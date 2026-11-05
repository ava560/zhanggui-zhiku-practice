"""简单查询和私有库子 Agent 共用的检索过程。"""

from __future__ import annotations

import logging
from typing import Any

from knowledge.registry import EntityRegistry, default_registry
from search.citations import NO_INFO, RETRIEVAL_ERROR, WEB_ERROR
from search.node_rrf import fuse_chunks

logger = logging.getLogger(__name__)


def resolve_with_registry(question: str, registry: EntityRegistry | None = None) -> dict[str, Any]:
    """向量对不上时做最长匹配；剩余简称歧义交给用户。"""
    source = registry or default_registry()
    ambiguous = source.ambiguous_aliases(question)
    if ambiguous:
        alias, records = ambiguous[0]
        return {
            "confirmed": [],
            "ambiguous": True,
            "message": f"请确认“{alias}”指哪一个主体",
            "candidates": [
                {
                    "itemname_question": record.canonical_name,
                    "display_name": record.canonical_name,
                    "score": 0.0,
                }
                for record in records
            ],
        }
    found = source.find_in_text(question)
    return {
        "confirmed": [record.canonical_name for record in found],
        "ambiguous": False,
        "candidates": [],
        "message": "",
    }


def report_status(state: dict[str, Any], text: str) -> None:
    """把当前步骤交给对话页，方便用户看到进展。"""
    state["status_text"] = text
    hook = state.get("on_status")
    if callable(hook):
        hook(text)


def run_shared_query(state: dict[str, Any], ports: dict[str, Any]) -> dict[str, Any]:
    """执行改写后的一次查询。不生成给用户的最终回答。"""
    report_status(state, "正在理解问题")
    prepared = ports["prepare"](state)
    question = str(prepared.get("question_rewritten") or state.get("current_question") or "").strip()
    names = [str(name) for name in prepared.get("extracted_names") or [] if str(name).strip()]
    base = {
        "question_rewritten": question,
        "confirmed_names": [],
        "chunks": [],
        "followups": [],
        "gaps": [],
        "web_search_error": "",
        "awaiting_user_confirmation": False,
        "item_name_candidates": [],
        "item_name_confirmation_message": "",
        "user_message": "",
    }
    if names:
        match = ports["match_names"](names, question)
        status = str(match.get("item_name_confirm_status") or "")
        if status in {"need_confirmation", "need_selection"}:
            return {
                **base,
                "status": "awaiting_user",
                "awaiting_user_confirmation": True,
                "item_name_candidates": list(match.get("item_name_candidates") or []),
                "item_name_confirmation_message": str(match.get("item_name_confirmation_message") or ""),
            }
        confirmed = [str(name) for name in match.get("itemname_question") or []]
        if status == "not_found" or not confirmed:
            resolved = ports["resolve_registry"](question, names)
            if resolved.get("ambiguous"):
                return {
                    **base,
                    "status": "awaiting_user",
                    "awaiting_user_confirmation": True,
                    "item_name_candidates": list(resolved.get("candidates") or []),
                    "item_name_confirmation_message": str(resolved.get("message") or ""),
                }
            confirmed = [str(name) for name in resolved.get("confirmed") or []]
        if not confirmed:
            return {**base, "status": "unsupported", "gaps": ["未确认到可检索的名称"], "user_message": NO_INFO}
        report_status(state, "正在检索知识库")
        try:
            private_chunks = list(ports["search_named"](question, confirmed) or [])
            hyde_chunks = list(ports["search_hyde"](question, confirmed) or [])
        except Exception as exc:
            return {**base, "status": "retrieval_error", "user_message": f"{RETRIEVAL_ERROR}（{type(exc).__name__}）"}
    else:
        confirmed = []
        report_status(state, "正在检索知识库")
        try:
            private_chunks = list(ports["search_direct"](question) or [])
            hyde_chunks = list(ports["search_hyde"](question, None) or [])
        except Exception as exc:
            return {**base, "status": "retrieval_error", "user_message": f"{RETRIEVAL_ERROR}（{type(exc).__name__}）"}

    web_error = ""
    web_chunks: list[dict[str, Any]] = []
    if state.get("use_web_search", True) is not False:
        report_status(state, "正在联网搜索")
        try:
            web_chunks = list(ports["search_web"](question) or [])
        except Exception as exc:
            logger.warning("百炼联网检索失败：%s", type(exc).__name__)
            web_error = WEB_ERROR
    fused = fuse_chunks(private_chunks, hyde_chunks, web_chunks)
    gaps = [] if fused else ["未检索到足够信息"]
    return {
        **base,
        "confirmed_names": confirmed,
        "chunks": fused,
        "status": "supported" if fused else "unsupported",
        "gaps": gaps,
        "web_search_error": web_error,
        "user_message": "" if fused else NO_INFO,
    }
