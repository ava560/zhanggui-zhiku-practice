"""区分证据充分、部分支持、无支持、冲突和检索故障。"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from search.query_plan import QueryPlan


_FACT = re.compile(
    r"(平安银行|招商银行|中国平安|华夏债券C|华夏债券)?"
    r"\s*(20\d{2})年?\s*"
    r"(营业收入|净利润|净值)\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*"
    r"(亿元|万元|元|%)?"
)
_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_LOCATOR = re.compile(r"^【定位】(.*?)\n", re.MULTILINE)


def hydrate_locator(chunk: dict[str, Any]) -> dict[str, Any]:
    """Milvus 现有字段没有页码时，从正文定位行恢复章节和解析质量。"""
    matched = _LOCATOR.search(str(chunk.get("content") or ""))
    if not matched:
        return chunk
    fields = {}
    for part in matched.group(1).split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key] = value
    if chunk.get("page") is None and str(fields.get("page") or "").isdigit():
        chunk["page"] = int(fields["page"])
    if not chunk.get("section") and fields.get("section"):
        chunk["section"] = fields["section"]
    if not chunk.get("parse_quality") and fields.get("parse_quality"):
        chunk["parse_quality"] = fields["parse_quality"]
    if fields.get("numeric_verified") == "false":
        chunk["numeric_verified"] = False
    return chunk


def build_citation(chunk: dict[str, Any]) -> dict[str, Any]:
    """用户可见引用只保留文档、页码、章节和原文，不带内部编号。"""
    chunk = hydrate_locator(chunk)
    span = _LOCATOR.sub("", str(chunk.get("content") or "")).strip()
    if len(span) > 180:
        span = span[:180]
    page = chunk.get("page")
    section = chunk.get("section") or None
    return {
        "document_title": chunk.get("document_title") or chunk.get("file_name") or "",
        "file_name": chunk.get("file_name") or "",
        "page": page if isinstance(page, int) else None,
        "section": section if section else None,
        "source_span": span,
        "publish_date": chunk.get("publish_date"),
    }


def assess_support(
    plan: QueryPlan,
    chunks: list[dict[str, Any]],
    *,
    retrieval_error: str | None,
) -> dict[str, Any]:
    if retrieval_error:
        return _result(
            "retrieval_error",
            "知识库检索失败，不能据此判断库里是否有资料。",
            [],
            [],
            False,
        )
    if not chunks:
        return _result(
            "unsupported",
            "当前知识库没有足够依据回答。",
            [],
            [],
            False,
        )
    chunks = [hydrate_locator(dict(chunk)) for chunk in chunks]

    degraded = any(
        chunk.get("parse_quality") == "degraded" or chunk.get("numeric_verified") is False
        for chunk in chunks
    )
    facts = []
    for chunk in chunks:
        if chunk.get("parse_quality") == "degraded" or chunk.get("numeric_verified") is False:
            continue
        facts.extend(_facts_in_chunk(chunk))
    citations = [build_citation(chunk) for chunk in chunks]
    verified_values = [fact["value"] for fact in facts]

    if _is_metric_question(plan) and degraded and not facts:
        return _result(
            "unsupported",
            "表格解析不足，不能把扁平文本当作已验证的财务数字。",
            citations,
            [],
            True,
        )

    conflicts = _conflicting_facts(facts)
    if conflicts:
        shown = "、".join(conflicts)
        return _result(
            "conflict",
            f"来源冲突：同一指标出现不同数值（{shown}），两边证据都保留，不能择一当作结论。",
            citations,
            verified_values,
            False,
        )

    required = [entity.canonical_name for entity in plan.entities]
    if len(required) >= 2:
        covered = [name for name in required if _mentions(chunks, name)]
        missing = [name for name in required if name not in covered]
        if missing:
            return _result(
                "partial",
                "部分支持：缺少" + "、".join(missing) + "的对应证据，不能用已有一侧填补。",
                citations,
                verified_values,
                False,
            )

    if plan.intent == "concept" or facts or chunks:
        return _result("sufficient", "", citations, verified_values, False)
    return _result("unsupported", "当前知识库没有足够依据回答。", citations, [], degraded)


def boundary_reply(
    plan: QueryPlan,
    chunks: list[dict[str, Any]],
    *,
    today: date,
) -> str | None:
    """收益承诺和当日净值在展示前由规则截住，不交给模型补数字。"""
    if plan.intent == "advice":
        return "不承诺收益，也不提供买入、卖出或持有决定。相关资料只说明风险、费用和已披露事实。"
    if plan.freshness_requirement != "realtime" and plan.intent != "freshness":
        return None
    dated = []
    for chunk in chunks:
        dated.extend(_DATE.findall(str(chunk.get("content") or "")))
    today_key = (f"{today.year:04d}", f"{today.month:02d}", f"{today.day:02d}")
    if today_key in dated:
        return None
    visible = "、".join(f"{year}-{month}-{day}" for year, month, day in dict.fromkeys(dated))
    if visible:
        return f"没有截至{today.isoformat()}的净值，不能把历史数值当作今天的最新净值。库内可见日期包括 {visible}。"
    return f"没有截至{today.isoformat()}的净值，不能把历史资料或模型记忆当作今天的最新净值。"


def _facts_in_chunk(chunk: dict[str, Any]) -> list[dict[str, str]]:
    text = str(chunk.get("content") or "")
    facts = []
    for match in _FACT.finditer(text):
        entity, year, metric, value, unit = match.groups()
        if not value or not metric:
            continue
        facts.append({
            "entity": entity or "",
            "year": year or "",
            "metric": metric,
            "value": value,
            "unit": unit or "",
            "chunk": chunk,
        })
    return facts


def _conflicting_facts(facts: list[dict[str, str]]) -> list[str]:
    grouped: dict[tuple[str, str, str], set[str]] = {}
    for fact in facts:
        if not fact["entity"] or not fact["metric"]:
            continue
        key = (fact["entity"], fact["metric"], fact["year"])
        grouped.setdefault(key, set()).add(fact["value"])
    conflicts = []
    for values in grouped.values():
        if len(values) > 1:
            conflicts.extend(sorted(values))
    return list(dict.fromkeys(conflicts))


def _mentions(chunks: list[dict[str, Any]], name: str) -> bool:
    for chunk in chunks:
        if chunk.get("parse_quality") == "degraded" or chunk.get("numeric_verified") is False:
            continue
        text = f"{chunk.get('content') or ''} {chunk.get('item_name') or ''}"
        if name in text:
            return True
    return False


def _is_metric_question(plan: QueryPlan) -> bool:
    return bool(plan.requested_metrics) and plan.intent in {"fact", "freshness", "statement"}


def _result(
    status: str,
    message: str,
    citations: list[dict[str, Any]],
    verified_values: list[str],
    numbers_unverified: bool,
) -> dict[str, Any]:
    return {
        "status": status,
        "user_message": message,
        "citations": citations,
        "verified_values": verified_values,
        "numbers_unverified": numbers_unverified,
    }
