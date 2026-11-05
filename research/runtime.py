"""进程内研究执行、幂等受理和租约代次。持久恢复仍需要具备事务的 MongoDB。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from research.facts import extract_verified_facts
from research.planner import ResearchTask, build_tasks
from research.report import render_comparison, render_industry
from search.evidence import build_citation
from search.query_plan import QueryPlan


@dataclass
class ResearchResult:
    status: str
    answer: str
    missing: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    task_status: dict[str, str] = field(default_factory=dict)


@dataclass
class RunDecision:
    run_id: str | None
    conflict: bool


class RunRegistry:
    """相同幂等键和相同请求复用 run；请求体不同则冲突。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], tuple[str, str]] = {}

    def accept(self, owner: str, key: str, body_hash: str) -> RunDecision:
        slot = (owner, key)
        existing = self._rows.get(slot)
        if existing is None:
            run_id = uuid.uuid4().hex
            self._rows[slot] = (body_hash, run_id)
            return RunDecision(run_id, False)
        if existing[0] == body_hash:
            return RunDecision(existing[1], False)
        return RunDecision(None, True)


class ExecutionFence:
    """旧执行代次不能提交状态。接管会提高代次。"""

    def __init__(self) -> None:
        self.epoch = 0
        self.owner = ""
        self.committed: dict[str, Any] | None = None

    def acquire(self, owner: str) -> int:
        self.epoch += 1
        self.owner = owner
        return self.epoch

    def submit(self, epoch: int, payload: dict[str, Any]) -> bool:
        if epoch != self.epoch:
            return False
        self.committed = dict(payload)
        return True


def converge(tasks: list[ResearchTask], results: dict[str, dict[str, Any]]) -> ResearchResult:
    status: dict[str, str] = {}
    for task in tasks:
        if task.task_id in results:
            status[task.task_id] = str(results[task.task_id].get("status") or "failed")
    for task in tasks:
        if task.task_id in status:
            continue
        dependency_status = [status.get(dependency) for dependency in task.depends_on]
        blocked = any(item in {None, "failed", "unsupported", "conflict"} for item in dependency_status)
        if task.depends_on and blocked and not task.allow_partial:
            status[task.task_id] = "blocked_by_dependency"
        else:
            status[task.task_id] = "unsupported"
    answer = "下游任务因依赖不足被阻断，不能编造缺失输入。"
    return ResearchResult("partial", answer, ["依赖未满足"], [], status)


def run_research(plan: QueryPlan, chunks: list[dict[str, Any]]) -> ResearchResult:
    """只用片段里已经写明的数值。没有事实时不生成财务数字。"""
    tasks = build_tasks(plan)
    facts = extract_verified_facts(chunks)
    if plan.task_type == "industry":
        names = [entity.canonical_name for entity in plan.entities]
        return ResearchResult(
            "partial",
            render_industry(names, universe_known=False),
            ["行业完整名单"],
            [],
            {task.task_id: "partial" for task in tasks},
        )
    if not facts:
        missing = [f"{entity.canonical_name}缺少可核实指标" for entity in plan.entities] or ["缺少可核实证据"]
        return ResearchResult(
            "unsupported",
            "没有足够的已核实证据，不能补写财务数字。",
            missing,
            [],
            {task.task_id: "unsupported" for task in tasks},
        )
    requested_year = plan.period.get("year") if plan.period else None
    rows = [
        {
            "entity": fact["entity"],
            "metric": fact["metric"],
            "year": fact["year"],
            "value": fact["value"],
            "unit": fact["unit"],
        }
        for fact in facts
    ]
    if requested_year and len(plan.entities) >= 2:
        answer = render_comparison(rows, requested_year=int(requested_year))
        missing = [
            entity.canonical_name for entity in plan.entities
            if not any(row["entity"] == entity.canonical_name and row["year"] == requested_year for row in rows)
        ]
    else:
        answer = "\n".join(
            f"{fact['entity']} {fact['year']}年 {fact['metric']} {fact['value']}{fact['unit']}"
            for fact in facts
        )
        missing = [
            entity.canonical_name for entity in plan.entities
            if not any(fact["entity"] == entity.canonical_name for fact in facts)
        ]
    status = "sufficient" if not missing else "partial"
    citations = [build_citation(fact["chunk"]) for fact in facts]
    return ResearchResult(status, answer, missing, citations, {task.task_id: status for task in tasks})
