"""带证据的业务关系。同一文件里同时出现两个名称只算共现，不生成管理或控股边。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ALLOWED_RELATIONS = {
    "HAS_ALIAS",
    "MANAGED_BY",
    "ISSUED_BY",
    "SUBSIDIARY_OF",
    "BELONGS_TO",
    "REPORTS_ON",
    "MENTIONED_IN",
    "SUPPORTED_BY",
}


@dataclass(frozen=True)
class RelationEdge:
    source: str
    relation: str
    target: str
    evidence_ids: tuple[str, ...]
    chunk_id: str


def business_edges_from_mentions(records: list[dict[str, Any]]) -> list[RelationEdge]:
    edges: list[RelationEdge] = []
    for record in records:
        relation = str(record.get("relation") or "")
        source = str(record.get("source") or "")
        target = str(record.get("target") or "")
        evidence = tuple(str(item) for item in record.get("evidence_ids") or [] if str(item).strip())
        if relation not in ALLOWED_RELATIONS or not source or not target or not evidence:
            continue
        edges.append(RelationEdge(
            source,
            relation,
            target,
            evidence,
            str(record.get("chunk_id") or ""),
        ))
    return edges


def relation_evidence(
    plan: Any,
    chunks: list[dict[str, Any]],
    edges: list[RelationEdge],
    *,
    graph_error: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """管理关系必须回到带来源的片段。图故障或共现都不能列出产品。"""
    if graph_error:
        return [], "图关系暂不可用，不能据此列出管理关系。"
    found: list[dict[str, Any]] = []
    for entity in list(getattr(plan, "entities", []) or []):
        entity_id = str(getattr(entity, "entity_id", "") or "")
        if not entity_id:
            continue
        found.extend(query_relations(edges, start=entity_id, relation="MANAGED_BY", direction="in"))
        found.extend(query_relations(edges, start=entity_id, relation="MANAGED_BY", direction="out"))
    chunk_ids = {item["chunk_id"] for item in found if item.get("chunk_id")}
    matched = [chunk for chunk in chunks if str(chunk.get("chunk_id") or "") in chunk_ids]
    if not matched:
        return [], "没有带来源片段的管理关系，不能根据同页共现推断管理人。"
    return matched, None


def query_relations(
    edges: list[RelationEdge],
    *,
    start: str,
    relation: str,
    direction: str = "out",
    max_hops: int = 1,
) -> list[dict[str, Any]]:
    """只返回每一跳都有证据的路径终点。缺证据的边不能继续向外推断。"""
    hops = max(1, min(int(max_hops), 2))
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    frontier = [start]
    for hop in range(1, hops + 1):
        next_frontier = []
        for node in frontier:
            for edge in edges:
                if edge.relation != relation or not edge.evidence_ids:
                    continue
                if direction == "in" and edge.target == node:
                    arrived = edge.source
                    chunk_id = edge.chunk_id
                elif direction == "out" and edge.source == node:
                    arrived = edge.target
                    chunk_id = edge.chunk_id
                else:
                    continue
                if arrived in seen or arrived == start:
                    continue
                seen.add(arrived)
                next_frontier.append(arrived)
                found.append({
                    "entity_id": arrived,
                    "chunk_id": chunk_id,
                    "evidence_ids": list(edge.evidence_ids),
                    "complete": True,
                    "hops": hop,
                })
        frontier = next_frontier
    return found
