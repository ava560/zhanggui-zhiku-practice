"""按实体编号合并候选，只在主体真正接近并列时才追问。"""

from __future__ import annotations

from typing import Any


NEAR_TIE = 0.01


def resolve_catalog_hits(
    extracted_names: list[str],
    hits_by_query: dict[str, list[dict[str, Any]]],
    task_type: str = "fact",
) -> dict[str, Any]:
    """精确名称或唯一别名直接采用；同一实体的多份文档合并为一个候选。"""
    if not extracted_names:
        return {
            "status": "unscoped",
            "entity_ids": [],
            "candidates": [],
            "needs_clarification": False,
            "task_type": task_type,
        }

    selected: list[dict[str, Any]] = []
    for name in extracted_names:
        grouped = _group_by_entity(hits_by_query.get(name) or [])
        exact = [
            item for item in grouped.values()
            if name == item.get("canonical_name") or name in set(item.get("aliases") or [])
        ]
        if len(exact) == 1:
            selected.append(exact[0])
            continue
        ranked = sorted(grouped.values(), key=lambda item: float(item.get("score") or 0), reverse=True)
        if len(ranked) >= 2 and abs(float(ranked[0]["score"]) - float(ranked[1]["score"])) <= NEAR_TIE:
            return {
                "status": "clarify",
                "entity_ids": [],
                "candidates": ranked[:2],
                "needs_clarification": True,
            }
        if ranked:
            selected.append(ranked[0])

    unique: dict[str, dict[str, Any]] = {}
    for item in selected:
        unique.setdefault(str(item.get("entity_id")), item)
    return {
        "status": "resolved" if unique else "unscoped",
        "entity_ids": list(unique),
        "candidates": list(unique.values()),
        "needs_clarification": False,
    }


def _group_by_entity(hits: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for hit in hits:
        entity_id = str(hit.get("entity_id") or "")
        if not entity_id:
            continue
        current = grouped.get(entity_id)
        titles = list(hit.get("doc_titles") or [])
        if current is None or float(hit.get("score") or 0) > float(current.get("score") or 0):
            kept = list((current or {}).get("doc_titles") or [])
            grouped[entity_id] = {
                **hit,
                "doc_titles": list(dict.fromkeys([*kept, *titles])),
            }
        else:
            current["doc_titles"] = list(dict.fromkeys([*(current.get("doc_titles") or []), *titles]))
    return grouped
