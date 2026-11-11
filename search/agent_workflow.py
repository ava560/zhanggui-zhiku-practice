"""把意图识别、共用查询、ReAct 和 Self-Ask 接到一次用户问题上。"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from search.citations import NO_INFO, RETRIEVAL_ERROR, annotate_answer, citations_for
from search.intent_classify import IntentClassificationError, classify_intent, default_llm
from search.long_term_memory import extract_explicit_memory, get_memory_book
from search.private_evidence import run_private_evidence, table_display_chunks
from search.react_agent import run_react
from search.self_ask import run_private_self_ask, run_web_self_ask
from search.shared_query import report_status, resolve_with_registry


def run_agent_workflow(
    state: dict[str, Any],
    llm=None,
    ports=None,
    planner=None,
    memory=None,
    decide_followup=None,
    answerer=None,
) -> dict[str, Any]:
    """对话入口使用的查询流程。未传入替换函数时，连接项目已配置的模型、Milvus 和百炼。"""
    from search.workflow import _use_direct_service_connections

    _use_direct_service_connections()
    question = str(state.get("current_question") or "").strip()
    if not state.get("user_question"):
        state["user_question"] = question
    _bind_query_budget(state.get("deadline_monotonic"))
    if not question:
        state["final_answer"] = "问题不能为空。"
        state["evidence_status"] = "unsupported"
        return state

    book = memory if memory is not None else get_memory_book()
    user_id = str(state.get("user_id") or "")
    remembered = extract_explicit_memory(question)
    if remembered and user_id:
        book.add(user_id, remembered, "user_explicit")
    state["long_term_memory"] = book.texts(user_id) if user_id else []
    history = list(state.get("conversation_history") or [])[-10:]
    caller = llm or default_llm
    active_ports = ports or build_ports()
    skill_steps = str(state.get("skill_instructions") or "")
    decider = decide_followup or (lambda pack, followups: _decide_with_llm(pack, followups, caller, skill_steps))

    if _resuming_confirmation(state):
        return _continue_after_confirmation(state, active_ports, decider, answerer)

    report_status(state, "正在判断问题类型")
    try:
        intent = classify_intent(
            question,
            caller,
            history=history,
            session_memory=state.get("session_memory") or {},
            long_term=state.get("long_term_memory") or [],
            skill=str(state.get("skill_instructions") or ""),
        )
    except IntentClassificationError as exc:
        state["final_answer"] = str(exc)
        state["evidence_status"] = "retrieval_error"
        state["citations"] = []
        return state

    state["agent_intent"] = intent
    if intent == "chat":
        state["item_name_confirm_status"] = "general_chat"
        state["is_product_question"] = False
        state["question_rewritten"] = question
        return state
    if intent == "simple":
        return _finish_simple(state, run_private_evidence(state, active_ports), answerer)

    active_planner = planner or (lambda context: _plan_with_llm({**context, "skill": skill_steps}, caller))

    def private_runner(action: dict[str, Any]) -> dict[str, Any]:
        sub_state = dict(state)
        sub_state["current_question"] = action.get("question") or question
        sub_state.pop("question_rewritten", None)
        task_id = re.sub(r"\s+", "_", str(action.get("question") or "private"))[:40]
        if state.get("local_dir"):
            sub_state["local_dir"] = str(Path(str(state["local_dir"])) / task_id)
        return run_private_self_ask(
            {"task_id": task_id, "question": sub_state["current_question"]},
            sub_state,
            active_ports,
            decide_followup=decider,
        )

    def web_runner(action: dict[str, Any]) -> dict[str, Any]:
        if state.get("use_web_search") is False:
            return {
                "task_id": "web",
                "task_type": "web_search",
                "status": "unsupported",
                "chunks": [],
                "gaps": ["未开启联网搜索"],
                "followups": [],
            }
        return run_web_self_ask(
            {"task_id": "web", "question": action.get("question") or question},
            lambda text: active_ports["search_web"](text),
            decide_followup=decider,
        )

    try:
        result = run_react(question, planner=active_planner, private_runner=private_runner, web_runner=web_runner)
    except PermissionError as exc:
        state["final_answer"] = f"任务规划失败：{exc}"
        state["evidence_status"] = "retrieval_error"
        state["citations"] = []
        return state
    if result.get("awaiting_user_confirmation"):
        waiting = result.get("awaiting") or {}
        if result.get("trace"):
            observation = result["trace"][-1].get("observation") or {}
            if observation.get("item_name_candidates"):
                waiting = observation
        state["awaiting_user_confirmation"] = True
        state["item_name_candidates"] = list(waiting.get("item_name_candidates") or [])
        state["item_name_confirmation_message"] = waiting.get("item_name_confirmation_message") or "请确认要查询的名称"
        state["item_name_confirm_status"] = "need_selection" if len(state["item_name_candidates"]) > 1 else "need_confirmation"
        state["react_trace"] = result.get("trace") or []
        return state
    gaps = list(result.get("gaps") or [])
    if result.get("stopped_for_limit"):
        gaps.append("已达到子任务或补查上限")
    web_error = ""
    for item in result.get("evidences") or []:
        if item.get("web_search_error"):
            web_error = str(item["web_search_error"])
            break
    table_results = []
    for item in result.get("evidences") or []:
        table_results.extend(item.get("table_results") or [])
        if item.get("table_reason") == "query_failed":
            gaps.append("结构化数据查询失败，不能据此判断表里是否有记录。")
    gaps = _maybe_write_back(state, table_results, gaps)
    display = list(result.get("chunks") or []) + table_display_chunks(table_results)
    if any(item.get("status") == "retrieval_error" for item in result.get("evidences") or []) and not display:
        state["final_answer"] = RETRIEVAL_ERROR
        state["evidence_status"] = "retrieval_error"
        state["citations"] = []
        return state
    if not display and not _has_answer_background(state):
        state["final_answer"] = NO_INFO
        state["evidence_status"] = "unsupported"
        state["citations"] = []
        state["react_trace"] = result.get("trace") or []
        return state
    return _write_model_answer(state, display, gaps, web_error, answerer, trace=result.get("trace") or [])


def _resuming_confirmation(state: dict[str, Any]) -> bool:
    if not state.get("awaiting_user_confirmation"):
        return False
    return state.get("item_name_user_confirmed") is not None or state.get("item_name_user_selection") is not None


def _continue_after_confirmation(state: dict[str, Any], ports: dict[str, Any], decider, answerer) -> dict[str, Any]:
    from search.node_item_name_confirm import apply_user_decision

    state = apply_user_decision(state)
    names = [str(name) for name in state.get("itemname_question") or []]
    if not names:
        state["final_answer"] = NO_INFO
        state["evidence_status"] = "unsupported"
        state["citations"] = []
        state["awaiting_user_confirmation"] = False
        return state
    state["extracted_names"] = names
    narrowed = dict(ports)
    narrowed["match_names"] = lambda selected, _question: {
        "item_name_confirm_status": "matched",
        "itemname_question": list(selected),
        "item_name_candidates": [],
        "item_name_confirmation_message": "",
    }
    return _finish_simple(state, run_private_evidence(state, narrowed), answerer)


def _finish_simple(state: dict[str, Any], result: dict[str, Any], answerer) -> dict[str, Any]:
    state["question_rewritten"] = result.get("question_rewritten") or state.get("current_question")
    if result.get("awaiting_user_confirmation"):
        state["awaiting_user_confirmation"] = True
        state["item_name_candidates"] = list(result.get("item_name_candidates") or [])
        state["item_name_confirmation_message"] = result.get("item_name_confirmation_message") or "请确认要查询的名称"
        state["item_name_confirm_status"] = "need_selection" if len(state["item_name_candidates"]) > 1 else "need_confirmation"
        state["evidence_status"] = "awaiting_user"
        return state
    display = _display_chunks(result)
    reason = str(result.get("table_reason") or "")
    if not display and (result.get("status") == "retrieval_error" or reason == "query_failed"):
        state["final_answer"] = _table_message(reason) or result.get("user_message") or RETRIEVAL_ERROR
        state["evidence_status"] = "retrieval_error"
        state["citations"] = []
        return state
    if not display and (reason or not _has_answer_background(state)):
        state["final_answer"] = _table_message(reason) or NO_INFO
        state["evidence_status"] = "unsupported"
        state["citations"] = []
        return state
    web_error = str(result.get("web_search_error") or "")
    gaps = _maybe_write_back(state, list(result.get("table_results") or []), list(result.get("gaps") or []))
    note = _table_message(reason)
    if note and note not in gaps:
        gaps.append(note)
    return _write_model_answer(state, display, gaps, web_error, answerer)


def _display_chunks(result: dict[str, Any]) -> list[dict[str, Any]]:
    return list(result.get("chunks") or []) + table_display_chunks(list(result.get("table_results") or []))


def _has_answer_background(state: dict[str, Any]) -> bool:
    from search.node_answer_output import has_answer_background

    return has_answer_background(
        state.get("conversation_history"),
        state.get("session_memory"),
        state.get("long_term_memory"),
    )


def _table_message(reason: str) -> str:
    return {
        "query_failed": "结构化数据查询失败，不能据此判断表里是否有记录。",
        "no_records": "表中没有对应记录。",
        "metric_unconfirmed": "缺少已确认的指标口径，不能计算。",
    }.get(reason, "")


def _write_model_answer(state, chunks, gaps, web_error, answerer, trace=None):
    """有检索结果时调用已配置的回答模型，并附上本次检索到的文件名或网页链接。"""
    if trace is not None:
        state["react_trace"] = trace
    state["citations"] = citations_for(chunks)
    state["awaiting_user_confirmation"] = False
    if not chunks and not _has_answer_background(state):
        state["final_answer"] = NO_INFO
        state["evidence_status"] = "unsupported"
        return state
    question = str(state.get("question_rewritten") or state.get("current_question") or "")
    report_status(state, "正在组织回答")
    ranked = _rerank_with_bge(question, chunks) if answerer is None and chunks else chunks
    if answerer is None:
        from search.node_answer_output import generate_answer

        answer, _images = generate_answer(
            str(state.get("current_question") or question),
            question,
            ranked,
            history=list(state.get("conversation_history") or [])[-10:],
            session_memory=state.get("session_memory") or {},
            long_term=state.get("long_term_memory") or [],
            skill=str(state.get("skill_instructions") or ""),
        )
    else:
        answer = answerer(state, ranked, gaps, web_error)
    marked, citations = annotate_answer(answer, ranked, download_for=_download_link)
    notes = [web_error] if web_error else []
    notes.extend(f"未完成：{gap}" for gap in gaps or [] if gap)
    if notes:
        marked = marked.rstrip() + "\n" + "\n".join(notes)
    state["final_answer"] = marked
    state["rerank_chunks"] = ranked
    state["citations"] = citations
    state["evidence_status"] = "supported"
    return state


def _download_link(file_name: str) -> str | tuple[str, str]:
    from urllib.parse import quote

    from search.source_files import find_imported_file

    path = find_imported_file(file_name)
    if path is None:
        return ""
    return "/api/sources/download?name=" + quote(file_name), path.name


def _rerank_with_bge(question: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from search.node_rerank import get_reranker, rerank_candidates

    model = get_reranker()
    ranked = rerank_candidates(question, chunks, [], model)
    return ranked or chunks


def _bind_query_budget(deadline: Any) -> None:
    from structured.query_table import bind_statement_budget

    if not isinstance(deadline, (int, float)):
        bind_statement_budget(None)
        return
    bind_statement_budget(int((float(deadline) - time.monotonic()) * 1000))


def _maybe_write_back(state: dict[str, Any], table_results: list[dict[str, Any]], gaps: list[str]) -> list[str]:
    from structured.writeback import wants_writeback, write_back_rows

    question = str(state.get("user_question") or state.get("current_question") or "")
    if not wants_writeback(question):
        return gaps
    notes = list(gaps)
    if not table_results:
        notes.append("没有可写回的表结果")
        return notes
    try:
        from structured.indexes import live_index
        from structured.session import open_db

        try:
            indexes = live_index()
        except Exception:
            indexes = _RejectingIndex()
        conn = open_db()
        try:
            for table in table_results:
                outcome = write_back_rows(
                    conn,
                    indexes,
                    str(table.get("table_id") or ""),
                    list(table.get("rows") or []),
                    question=question,
                    skill_id=str(state.get("skill_id") or ""),
                )
                message = str(outcome.get("message") or "")
                if message and message not in notes:
                    notes.append(message)
        finally:
            conn.close()
    except Exception:
        notes.append("写回没有完成，当前可查询版本不变")
    return notes


class _RejectingIndex:
    def write_values(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("值索引不可用")

    def delete_values(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _decide_with_llm(pack: dict[str, Any], followups: list, llm, skill: str = "") -> str | None:
    """证据还不够时，最多再问一个可检索的追问。"""
    if len(followups) >= 2 or (pack.get("status") == "supported" and not pack.get("gaps")):
        return None
    prompt = (
        "你是子 Agent。只输出 JSON：{\"followup\":\"\"}。"
        "如果还缺一个可以继续检索的问题，把问题写进 followup；不需要则留空。"
        f"当前问题：{pack.get('question_rewritten')}。状态：{pack.get('status')}。"
        f"缺口：{json.dumps(pack.get('gaps') or [], ensure_ascii=False)}。"
    )
    if str(skill or "").strip():
        prompt += f"技能步骤：{str(skill).strip()}。步骤不改写用户原话。"
    try:
        raw = str(llm(prompt) or "")
    except Exception:
        return None
    begin, end = raw.find("{"), raw.rfind("}")
    if begin < 0 or end <= begin:
        return None
    try:
        data = json.loads(raw[begin : end + 1])
    except json.JSONDecodeError:
        return None
    followup = data.get("followup") if isinstance(data, dict) else None
    if isinstance(followup, str) and followup.strip():
        return followup.strip()
    return None


def _plan_with_llm(context: dict[str, Any], llm) -> list[dict[str, Any]]:
    if context.get("dispatches", 0) >= 4 or context.get("supplements_done", 0) > 2:
        return [{"action": "finish", "thought": "已达到上限"}]
    statuses = [item.get("status") for item in context.get("evidences") or []]
    prompt = (
        "你是主 Agent。只输出 JSON："
        '{"actions":[{"action":"private_kb","question":"","thought":""}]}。'
        "action 只能是 private_kb、web_search、ask_user、finish。"
        f"已派发 {context.get('dispatches', 0)} 次，已补查 {context.get('supplements_done', 0)} 轮。"
        f"目标：{context.get('goal')}。已有状态：{json.dumps(statuses, ensure_ascii=False)}。"
    )
    if str(context.get("skill") or "").strip():
        prompt += f"技能步骤：{str(context.get('skill')).strip()}。步骤不能增加动作。"
    try:
        raw = llm(prompt)
    except Exception:
        return [{"action": "finish", "thought": "规划失败"}]
    text = str(raw or "")
    begin, end = text.find("{"), text.rfind("}")
    if begin < 0 or end <= begin:
        return [{"action": "finish", "thought": "规划失败"}]
    try:
        data = json.loads(text[begin : end + 1])
    except json.JSONDecodeError:
        return [{"action": "finish", "thought": "规划失败"}]
    actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions, list):
        return [{"action": "finish", "thought": "规划失败"}]
    cleaned = []
    for item in actions:
        if not isinstance(item, dict) or item.get("action") not in {"private_kb", "web_search", "ask_user", "finish"}:
            return [{"action": "finish", "thought": "规划失败"}]
        cleaned.append({
            "action": item["action"],
            "question": str(item.get("question") or context.get("goal") or ""),
            "thought": str(item.get("thought") or ""),
        })
    return cleaned or [{"action": "finish", "thought": "没有动作"}]


def _milvus_once(operation):
    """Zilliz 偶发断连时换一条新连接再试一次，仍然连的是配置里的库。"""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            text = str(exc).lower()
            transient = type(exc).__name__ in {"MilvusException", "ConnectionNotExistException"} or any(
                token in text for token in ("unavailable", "deadline", "fail connecting", "server unavailable")
            )
            if attempt == 0 and transient:
                logger.warning("Milvus 连接中断，正在重连：%s", type(exc).__name__)
                continue
            raise
    if last_error is not None:
        raise last_error
    return None


def build_ports() -> dict[str, Any]:
    """把现有检索节点收成共用查询的端口。"""

    def prepare(state: dict[str, Any]) -> dict[str, Any]:
        from search.node_item_name_confirm import call_llm_for_json, load_settings, parse_llm_json

        history = list(state.get("conversation_history") or [])[-10:]
        notes = [str(item) for item in state.get("long_term_memory") or [] if str(item).strip()]
        if notes:
            history = [*history, {"role": "user", "content": "长期记忆：" + "；".join(notes)}]
        if state.get("extracted_names"):
            return {
                "question_rewritten": state.get("question_rewritten") or state.get("current_question") or "",
                "extracted_names": list(state.get("extracted_names") or []),
            }
        raw = call_llm_for_json(load_settings(), history, str(state.get("current_question") or ""))
        parsed = parse_llm_json(raw, str(state.get("current_question") or ""))
        return {
            "question_rewritten": parsed["question_rewritten"],
            "extracted_names": list(parsed["itemname_question"]),
        }

    def match_names(names, question):
        del question
        from search.node_item_name_confirm import (
            classify_matches,
            get_milvus_client,
            hybrid_search_item_name,
            load_settings,
        )

        settings = load_settings()
        client = get_milvus_client(settings)
        hits = {}
        try:
            client.load_collection(collection_name=settings.item_name_collection)
            for name in names:
                hits[name] = hybrid_search_item_name(client, settings, name)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        return classify_matches(list(names), hits)

    def search_named(question, names, state=None):
        from search.node_search_embedding import get_milvus_client, load_search_settings, retrieve_top5

        settings = load_search_settings()

        def once():
            client = get_milvus_client(settings)
            try:
                _matched, _missing, top5 = retrieve_top5(client, settings, list(names), question, state or {})
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            return top5

        return _milvus_once(once)

    def search_direct(question, state=None):
        from search.financial_retrieve import _vector_cache
        from search.node_search_embedding import _hybrid_search, get_milvus_client, load_search_settings

        settings = load_search_settings()

        def once():
            client = get_milvus_client(settings)
            try:
                client.load_collection(collection_name=settings.chunks_collection)
                hits = _hybrid_search(
                    client,
                    settings,
                    settings.chunks_collection,
                    question,
                    'file_name != ""',
                    max(5, int(getattr(settings, "search_top_k", 5))),
                    ["item_name", "content", "file_name"],
                    _vector_cache(state or {}, settings),
                )
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            chunks = []
            for hit in hits:
                entity = hit.get("entity") if isinstance(hit.get("entity"), dict) else hit
                chunks.append({
                    "chunk_id": hit.get("id", entity.get("chunk_id")),
                    "item_name": entity.get("item_name", ""),
                    "file_name": entity.get("file_name", ""),
                    "content": entity.get("content", ""),
                    "score": float(hit.get("distance", hit.get("score", 0)) or 0),
                })
            return chunks

        return _milvus_once(once)

    def search_hyde(question, names):
        from search.node_search_embedding_hyde import generate_hyde_answer

        selected = list(names or [])
        hyde_text = generate_hyde_answer(question, selected)
        query = f"{question}\n{hyde_text}"
        if selected:
            return search_named(query, selected)
        return search_direct(query)

    def search_web(question):
        from search.node_web_search_mcp import node_web_search_mcp

        last_error: Exception | None = None
        for _ in range(2):
            try:
                found = node_web_search_mcp({"question_rewritten": question, "current_question": question})
                rows = list(found.get("web_search_top5") or [])
                if rows:
                    return rows
                last_error = ValueError("百炼没有返回带链接的网页")
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return []

    return {
        "prepare": prepare,
        "match_names": match_names,
        "resolve_registry": lambda question, _unresolved: resolve_with_registry(question),
        "search_named": lambda question, names: search_named(question, names),
        "search_direct": search_direct,
        "search_hyde": search_hyde,
        "search_web": search_web,
        "query_table": _query_table,
    }


def _query_table(question: str) -> dict[str, Any]:
    from structured.query_table import query_table_data

    try:
        from structured.indexes import live_index

        indexes = live_index()
    except Exception:
        indexes = None
    return query_table_data(question, indexes=indexes)
