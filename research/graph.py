"""用 LangGraph 把每家公司的取证分发到独立 worker，再交回同一个汇总节点。"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from research.facts import extract_verified_facts
from research.planner import build_tasks
from research.runtime import ResearchResult, run_research
from search.query_plan import QueryPlan


class ResearchState(TypedDict, total=False):
    results: Annotated[list[dict[str, Any]], operator.add]
    answer: str
    status: str
    missing: list[str]
    citations: list[dict[str, Any]]


def run_research_graph(plan: QueryPlan, chunks: list[dict[str, Any]]) -> ResearchResult:
    tasks = [task for task in build_tasks(plan) if task.task_type == "company_evidence"]
    if not tasks:
        return run_research(plan, chunks)

    def fanout(_state: ResearchState) -> list[Send]:
        return [
            Send("worker", {"entity_id": task.entity_ids[0], "task_id": task.task_id})
            for task in tasks[:3]
        ]

    def worker(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("entity_id") or "").split(":", 1)[-1]
        selected = [chunk for chunk in chunks if name and name in str(chunk.get("content") or "")]
        return {"results": [{"task_id": payload.get("task_id"), "facts": extract_verified_facts(selected)}]}

    def reduce(state: ResearchState) -> dict[str, Any]:
        seen: list[dict[str, Any]] = []
        identities: set[int] = set()
        for item in state.get("results") or []:
            for fact in item.get("facts") or []:
                chunk = fact.get("chunk")
                if isinstance(chunk, dict) and id(chunk) not in identities:
                    identities.add(id(chunk))
                    seen.append(chunk)
        result = run_research(plan, seen)
        return {
            "answer": result.answer,
            "status": result.status,
            "missing": result.missing,
            "citations": result.citations,
        }

    graph = StateGraph(ResearchState)
    graph.add_node("worker", worker)
    graph.add_node("reduce", reduce)
    graph.add_conditional_edges(START, fanout, ["worker"])
    graph.add_edge("worker", "reduce")
    graph.add_edge("reduce", END)
    output = graph.compile().invoke({"results": []})
    return ResearchResult(
        str(output.get("status") or "unsupported"),
        str(output.get("answer") or ""),
        list(output.get("missing") or []),
        list(output.get("citations") or []),
    )
