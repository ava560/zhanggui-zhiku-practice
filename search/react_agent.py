"""主 Agent 的有界 ReAct 循环。动作只有四种。"""

from __future__ import annotations

from typing import Any, Callable


ALLOWED_ACTIONS = {"private_kb", "web_search", "ask_user", "finish"}


def run_react(
    goal: str,
    *,
    planner: Callable[[dict[str, Any]], list[dict[str, Any]]],
    private_runner: Callable[[dict[str, Any]], dict[str, Any]],
    web_runner: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """初始拆分后再补查最多两轮，私有库和联网派发合计少于 5 次。"""
    supplements_done = 0
    dispatches = 0
    evidences: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    stopped_for_limit = False
    while supplements_done <= 2:
        actions = list(planner({
            "goal": goal,
            "dispatches": dispatches,
            "supplements_done": supplements_done,
            "evidences": evidences,
        }) or [])
        for action in actions:
            name = action.get("action")
            if name not in ALLOWED_ACTIONS:
                raise PermissionError(f"动作不在允许范围内：{name}")
        if not actions or all(action.get("action") == "finish" for action in actions):
            break
        asks = [action for action in actions if action.get("action") == "ask_user"]
        if asks:
            trace.append({"thought": asks[0].get("thought") or "", "action": "ask_user", "observation": {}})
            return _result(goal, trace, evidences, stopped_for_limit, awaiting=asks[0])
        jobs = [action for action in actions if action.get("action") in {"private_kb", "web_search"}]
        room = 4 - dispatches
        if room <= 0:
            stopped_for_limit = True
            break
        if len(jobs) > room:
            stopped_for_limit = True
            jobs = jobs[:room]
        for job in jobs:
            observation = private_runner(job) if job["action"] == "private_kb" else web_runner(job)
            dispatches += 1
            evidences.append(observation or {})
            trace.append({
                "thought": job.get("thought") or "",
                "action": job["action"],
                "observation": observation or {},
            })
            if (observation or {}).get("awaiting_user_confirmation") or (observation or {}).get("status") == "awaiting_user":
                return _result(goal, trace, evidences, stopped_for_limit, awaiting=observation or {})
        if _satisfied(evidences):
            break
        if supplements_done == 2:
            break
        supplements_done += 1
    return _result(goal, trace, evidences, stopped_for_limit, awaiting=None)


def _satisfied(evidences: list[dict[str, Any]]) -> bool:
    if not evidences:
        return False
    return all(item.get("status") == "supported" and not item.get("gaps") for item in evidences)


def _result(goal, trace, evidences, stopped_for_limit, awaiting):
    chunks = []
    gaps = []
    seen_urls = set()
    for item in evidences:
        for chunk in item.get("chunks") or []:
            url = str(chunk.get("url") or "").strip()
            if url:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
            chunks.append(chunk)
        gaps.extend(item.get("gaps") or [])
    status = "awaiting_user" if awaiting else ("supported" if _satisfied(evidences) else "unsupported")
    return {
        "goal": goal,
        "trace": trace,
        "evidences": evidences,
        "chunks": chunks,
        "gaps": gaps,
        "stopped_for_limit": stopped_for_limit,
        "status": status,
        "awaiting_user_confirmation": bool(awaiting),
        "awaiting": awaiting or {},
    }
