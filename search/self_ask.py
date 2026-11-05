"""子 Agent 用 Self-Ask 取证，不向用户发布最终回答。"""

from __future__ import annotations

from typing import Any, Callable

from search.citations import WEB_ERROR
from search.shared_query import run_shared_query


FollowupDecider = Callable[..., str | None]


def _pack_from_query(task_id: str, task_type: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_type": task_type,
        "question_rewritten": result.get("question_rewritten") or "",
        "followups": [],
        "confirmed_names": list(result.get("confirmed_names") or []),
        "chunks": list(result.get("chunks") or []),
        "status": result.get("status") or "unsupported",
        "gaps": list(result.get("gaps") or []),
        "awaiting_user_confirmation": bool(result.get("awaiting_user_confirmation")),
        "item_name_candidates": list(result.get("item_name_candidates") or []),
        "item_name_confirmation_message": result.get("item_name_confirmation_message") or "",
        "web_search_error": result.get("web_search_error") or "",
    }


def run_private_self_ask(
    task: dict[str, Any],
    state: dict[str, Any],
    ports: dict[str, Any],
    *,
    decide_followup: FollowupDecider,
) -> dict[str, Any]:
    """主问题和每个追问都走共用私有库查询。追问最多两个。"""
    base = dict(state)
    base["current_question"] = task["question"]
    base.pop("question_rewritten", None)
    pack = _pack_from_query(str(task.get("task_id") or ""), "private_kb", run_shared_query(base, ports))
    if pack["awaiting_user_confirmation"] or pack["status"] == "retrieval_error":
        return pack
    while len(pack["followups"]) < 2:
        nxt = decide_followup(pack, pack["followups"])
        if not nxt:
            break
        follow_state = dict(base)
        follow_state["current_question"] = nxt
        follow_state.pop("question_rewritten", None)
        follow = run_shared_query(follow_state, ports)
        pack["followups"].append({
            "question": nxt,
            "status": follow.get("status"),
            "chunks": list(follow.get("chunks") or []),
        })
        pack["chunks"].extend(follow.get("chunks") or [])
        if follow.get("awaiting_user_confirmation"):
            pack["awaiting_user_confirmation"] = True
            pack["status"] = "awaiting_user"
            pack["item_name_candidates"] = list(follow.get("item_name_candidates") or [])
            pack["item_name_confirmation_message"] = follow.get("item_name_confirmation_message") or ""
            break
    return pack


def _web_once(question: str, search_web: Callable[[str], list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], str]:
    try:
        rows = list(search_web(question) or [])
    except Exception:
        return [], WEB_ERROR
    kept = []
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        kept.append({
            "title": row.get("title") or "",
            "url": url,
            "content": row.get("content") or "",
            "source": "web",
        })
    return kept, ""


def run_web_self_ask(
    task: dict[str, Any],
    search_web: Callable[[str], list[dict[str, Any]]],
    *,
    decide_followup: FollowupDecider,
) -> dict[str, Any]:
    """只调用网页检索。没有链接的摘要会被丢掉。"""
    chunks, error = _web_once(str(task.get("question") or ""), search_web)
    pack = {
        "task_id": str(task.get("task_id") or ""),
        "task_type": "web_search",
        "question_rewritten": str(task.get("question") or ""),
        "followups": [],
        "confirmed_names": [],
        "chunks": chunks,
        "status": "supported" if chunks else ("retrieval_error" if error else "unsupported"),
        "gaps": [] if chunks else ["未检索到带链接的网页"],
        "awaiting_user_confirmation": False,
        "item_name_candidates": [],
        "item_name_confirmation_message": "",
        "web_search_error": error,
    }
    while len(pack["followups"]) < 2:
        nxt = decide_followup(pack, pack["followups"])
        if not nxt:
            break
        more, follow_error = _web_once(nxt, search_web)
        pack["followups"].append({"question": nxt, "chunks": more, "web_search_error": follow_error})
        pack["chunks"].extend(more)
        if follow_error and not pack["web_search_error"]:
            pack["web_search_error"] = follow_error
    if pack["chunks"]:
        pack["status"] = "supported"
        pack["gaps"] = []
    return pack
