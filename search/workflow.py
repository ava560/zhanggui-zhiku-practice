"""对话页的真实检索链：确认商品 -> 双路知识库/联网检索 -> 重排 -> 答案。"""

from __future__ import annotations

import os
import logging
from urllib.parse import urlsplit

from typing import Any

from state import ImportGraphState

logger = logging.getLogger(__name__)


def _use_direct_service_connections() -> None:
    """本机环境的 HTTP 代理不可用时，仅让已配置的服务地址直连。"""
    if os.getenv("SEARCH_USE_PROXY", "").strip().lower() in {"1", "true", "yes"}:
        return
    hosts = {
        urlsplit(os.getenv(key, "")).hostname
        for key in ("OPENAI_BASE_URL", "DEEPSEEK_BASE_URL", "MCP_DASHSCOPE_BASE_URL", "MILVUS_URL")
    }
    current = [part.strip() for part in os.getenv("NO_PROXY", "").split(",") if part.strip()]
    os.environ["NO_PROXY"] = ",".join(dict.fromkeys(current + sorted(host for host in hosts if host)))


def _apply_financial_choice(state: ImportGraphState) -> ImportGraphState:
    """用户点选的是公司或产品主体，不是内部文件名。"""
    candidates = list(state.get("item_name_candidates") or [])
    chosen = None
    if state.get("item_name_user_confirmed") is True and len(candidates) == 1:
        chosen = candidates[0]
    else:
        reply = state.get("item_name_user_selection")
        labels = [reply] if isinstance(reply, str) else list(reply or [])
        for label in labels:
            for candidate in candidates:
                if label in {candidate.get("display_name"), candidate.get("itemname_question")}:
                    chosen = candidate
                    break
    if not isinstance(chosen, dict):
        return state
    memory = dict(state.get("session_memory") or {})
    name = str(chosen.get("display_name") or chosen.get("itemname_question") or "")
    memory["resolved_entities"] = [{
        "entity_id": chosen.get("entity_id") or f"company:{name}",
        "canonical_name": name,
        "entity_type": chosen.get("entity_type") or "company",
        "aliases": [name],
    }]
    state["session_memory"] = memory
    state["awaiting_user_confirmation"] = False
    state["item_name_user_selection"] = None
    state["item_name_user_confirmed"] = None
    return state


def _run_financial(state: ImportGraphState, plan: Any) -> ImportGraphState:
    """概念、财报和比较走证据检索；检索故障不能写成库里没有资料。"""
    from datetime import date

    from search.evidence import assess_support, boundary_reply

    state["query_plan"] = plan.to_dict()
    state["session_memory"] = plan.session_memory
    state["question_rewritten"] = plan.rewritten or str(state.get("current_question") or "")
    state["is_product_question"] = False
    state["itemname_question"] = [entity.canonical_name for entity in plan.entities]
    state["item_name_confirm_status"] = "knowledge"
    if plan.clarification:
        state["awaiting_user_confirmation"] = True
        state["item_name_confirm_status"] = "need_selection"
        state["item_name_confirmation_message"] = plan.clarification
        state["item_name_candidates"] = [
            {
                "itemname_question": item["canonical_name"],
                "display_name": item["display_name"],
                "entity_id": item["entity_id"],
                "entity_type": item["entity_type"],
            }
            for item in plan.clarification_options
        ]
        return state
    try:
        from search.financial_retrieve import search_financial_evidence

        state = search_financial_evidence(state, plan)
    except Exception as exc:
        logger.exception("金融知识检索失败：%s", type(exc).__name__)
        assessed = assess_support(plan, [], retrieval_error=type(exc).__name__)
        state["search_error"] = type(exc).__name__
        state["evidence_status"] = assessed["status"]
        state["final_answer"] = assessed["user_message"]
        state["citations"] = []
        return state

    chunks = list(state.get("financial_hits") or [])
    if plan.task_type in {
        "relation",
        "product_manager",
        "manager_products",
        "issued_by",
        "company_parent",
        "company_reports",
        "industry_companies",
    }:
        graph_error = None
        edges: list[Any] = []
        try:
            from graph_store import load_business_edges

            edges = load_business_edges()
        except Exception as exc:
            graph_error = type(exc).__name__
            state["graph_status"] = "degraded"
        from graph_relations import relation_evidence

        chunks, gap = relation_evidence(plan, chunks, edges, graph_error=graph_error)
        state["financial_hits"] = chunks
        if gap:
            state["evidence_status"] = "unsupported"
            state["final_answer"] = gap
            state["citations"] = []
            return state
    boundary = boundary_reply(plan, chunks, today=date.today())
    assessed = assess_support(plan, chunks, retrieval_error=None)
    state["evidence_status"] = assessed["status"]
    state["citations"] = assessed["citations"]
    if boundary is not None or assessed["status"] in {"unsupported", "retrieval_error", "conflict"} or assessed["numbers_unverified"]:
        state["final_answer"] = boundary or assessed["user_message"]
        return state
    if plan.route == "research":
        from research.runtime import run_research

        result = run_research(plan, chunks)
        state["final_answer"] = result.answer
        state["evidence_status"] = result.status
        state["citations"] = result.citations or assessed["citations"]
        state["status_text"] = "正在核对已收录资料中的可比事实。"
        return state
    state["rerank_chunks"] = chunks
    if state.get("defer_answer_generation"):
        state["status_text"] = "正在根据已检索资料组织回答。"
        return state
    from search.node_answer_output import node_answer_output

    return node_answer_output(state)


def run_search_workflow(state: ImportGraphState) -> ImportGraphState:
    """确认阶段可暂停；最终答案由 node_answer_output 归档到 MongoDB。"""
    _use_direct_service_connections()
    from search.node_answer_output import node_answer_output
    from search.node_item_name_confirm import node_item_name_confirm
    from search.node_rerank import node_rerank
    from search.node_rrf import node_rrf
    from search.node_search_embedding import node_search_embedding
    from search.node_search_embedding_hyde import node_search_embedding_hyde
    from search.node_web_search_mcp import node_web_search_mcp
    from search.query_plan import apply_mode, plan_question

    pending_plan = state.get("query_plan")
    financial_pending = (
        state.get("awaiting_user_confirmation")
        and isinstance(pending_plan, dict)
        and pending_plan.get("route") not in {None, "", "defer"}
    )
    if financial_pending:
        state = _apply_financial_choice(state)
        if state.get("awaiting_user_confirmation"):
            return state

    question = str(state.get("current_question") or "").strip()
    if question and not state.get("awaiting_user_confirmation"):
        plan = apply_mode(
            plan_question(question, state.get("session_memory") if isinstance(state.get("session_memory"), dict) else None),
            str(state.get("answer_mode") or "auto"),
        )
        if plan.route == "general_chat":
            state["query_plan"] = plan.to_dict()
            state["session_memory"] = plan.session_memory
            state["item_name_confirm_status"] = "general_chat"
            state["is_product_question"] = False
            state["question_rewritten"] = question
            return state
        if plan.route != "defer":
            return _run_financial(state, plan)

    state = node_item_name_confirm(state)
    if state.get("awaiting_user_confirmation"):
        return state
    if state.get("item_name_confirm_status") == "general_chat":
        return state
    if state.get("itemname_question"):
        from graph_store import expand_relation_scope

        expand_relation_scope(state)
        state = node_search_embedding(state)
        state = node_search_embedding_hyde(state)
        state = node_rrf(state)
        try:
            state = node_web_search_mcp(state)
        except Exception as exc:
            # 联网搜索中断不应丢弃已经检索到的本地知识库资料。
            logger.warning("MCP 联网搜索未完成，继续使用知识库结果：%s", type(exc).__name__)
            state["web_search_top5"] = []
            state["web_search_json_path"] = ""
            state["web_search_error"] = "本次联网搜索暂不可用，仅使用知识库资料。"
        state = node_rerank(state)
    return node_answer_output(state)
