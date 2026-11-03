"""对话页的真实检索链：确认商品 -> 双路知识库/联网检索 -> 重排 -> 答案。"""

from __future__ import annotations

import os
import logging
from urllib.parse import urlsplit

from state import ImportGraphState

logger = logging.getLogger(__name__)


def _use_direct_service_connections() -> None:
    """本机环境的 HTTP 代理不可用时，仅让已配置的服务地址直连。"""
    if os.getenv("SEARCH_USE_PROXY", "").strip().lower() in {"1", "true", "yes"}:
        return
    hosts = {
        urlsplit(os.getenv(key, "")).hostname
        for key in ("OPENAI_BASE_URL", "DEEPSEEK_BASE_URL", "MCP_DASHSCOPE_BASE_URL")
    }
    current = [part.strip() for part in os.getenv("NO_PROXY", "").split(",") if part.strip()]
    os.environ["NO_PROXY"] = ",".join(dict.fromkeys(current + sorted(host for host in hosts if host)))


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
