"""把问题规划成知识检索或研究任务，不再把金融问题当成闲聊。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any


@dataclass
class EntityMention:
    canonical_name: str
    entity_type: str
    entity_id: str
    mention: str
    share_class: str | None = None
    ambiguous: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QueryPlan:
    intent: str
    needs_retrieval: bool
    entities: list[EntityMention]
    topics: list[str]
    period: dict[str, Any] | None
    requested_metrics: list[str]
    task_type: str
    freshness_requirement: str
    clarification: str | None
    clarification_options: list[dict[str, str]]
    kb_scope: str
    route: str
    hyde_enabled: bool
    web_search_enabled: bool
    rewritten: str
    session_memory: dict[str, Any]
    topic: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["entities"] = [entity.as_dict() for entity in self.entities]
        return payload


_FINANCIAL = (
    "基金", "净值", "赎回", "申购", "理财", "财报", "业绩", "营收", "净利润",
    "利润", "现金流", "资产质量", "手续费", "费率", "管理人", "央行", "政策",
    "公告", "份额", "银行行业", "不良", "经营风险", "盈利能力", "年报",
)
_DEVICE = ("怎么开机", "怎么用", "如何配置", "用户指南", "说明书", "故障", "设备", "网桥", "平板")
_GREETING = re.compile(r"^\s*(?:你好|您好|嗨|hi|hello|在吗)[\s，,！!。]*", re.IGNORECASE)
_ARITHMETIC = re.compile(r"\d+\s*[\+\-\*/×÷]\s*\d+")
_YEAR = re.compile(r"(20\d{2})\s*年")
_PRONOUN = ("它", "其", "这个", "该")
_RELATION_TASKS = (
    ("manager_products", ("还管理",)),
    ("issued_by", ("发行人",)),
    ("company_parent", ("控股", "子公司", "属于哪个", "哪个集团")),
    ("industry_companies", ("所属行业",)),
    ("company_reports", ("哪些报告", "哪份报告")),
    ("product_manager", ("管理人",)),
)


def apply_mode(plan: QueryPlan, mode: str) -> QueryPlan:
    """快速模式不启动多 Agent；深入研究保留研究路由。"""
    if mode == "fast" and plan.route == "research":
        route = "program_parallel" if len(plan.entities) >= 2 else "knowledge"
        return replace(plan, route=route)
    return plan


def plan_question(question: str, session: dict[str, Any] | None = None, registry=None) -> QueryPlan:
    """依据问题文本和会话中已确认的主体生成查询计划。"""
    previous = session or {}
    original = (question or "").strip()
    remainder = _GREETING.sub("", original).strip()
    if not remainder:
        return _chat_plan(original, previous)

    if _defer_to_product_pipeline(remainder):
        return _base_plan(
            intent="product",
            route="defer",
            needs_retrieval=True,
            rewritten=remainder,
            session_memory=_carry_session(previous, [], None, previous.get("topic") or ""),
            topic=str(previous.get("topic") or ""),
        )

    entities, ambiguous = _find_entities(remainder, registry)
    entities, ambiguous = _inherit_session_entities(remainder, entities, ambiguous, previous)
    period = _parse_period(remainder) or _copy_period(previous.get("active_period"))
    intent = _intent(remainder)
    topic = _topic(remainder, intent)
    metrics = _metrics(remainder)
    route, task_type = _route(remainder, entities, intent)
    clarification = None
    options: list[dict[str, str]] = []
    if ambiguous and intent != "advice":
        alias, candidates = ambiguous[0]
        clarification, options = _clarification(alias, candidates)
        entities = []
    elif _needs_subject(remainder, intent) and not entities and not previous.get("resolved_entities"):
        clarification = "请说明要查询的公司或产品。"

    resolved_for_memory = entities
    memory = _carry_session(previous, resolved_for_memory, period, topic)
    rewritten = remainder
    if len(entities) == 1 and any(token in remainder for token in ("它", "其")):
        rewritten = remainder.replace("它", entities[0].canonical_name).replace("其", entities[0].canonical_name)

    return _base_plan(
        intent=intent,
        route=route,
        needs_retrieval=True,
        entities=entities,
        topics=[topic] if topic else [],
        period=period,
        requested_metrics=metrics,
        task_type=task_type,
        freshness_requirement="realtime" if intent == "freshness" else "historical" if period else "none",
        clarification=clarification,
        clarification_options=options,
        rewritten=rewritten,
        session_memory=memory,
        topic=topic,
        hyde_enabled=False,
        web_search_enabled="联网" in remainder or "网上搜索" in remainder,
    )


def _chat_plan(question: str, previous: dict[str, Any]) -> QueryPlan:
    return _base_plan(
        intent="greeting",
        route="general_chat",
        needs_retrieval=False,
        rewritten=question,
        session_memory=_carry_session(previous, [], _copy_period(previous.get("active_period")), str(previous.get("topic") or "")),
        topic=str(previous.get("topic") or ""),
    )


def _base_plan(**kwargs: Any) -> QueryPlan:
    defaults: dict[str, Any] = {
        "entities": [],
        "topics": [],
        "period": None,
        "requested_metrics": [],
        "task_type": "chat",
        "freshness_requirement": "none",
        "clarification": None,
        "clarification_options": [],
        "kb_scope": "current",
        "hyde_enabled": False,
        "web_search_enabled": False,
    }
    defaults.update(kwargs)
    return QueryPlan(**defaults)


def _defer_to_product_pipeline(text: str) -> bool:
    if _ARITHMETIC.search(text):
        return True
    financial = any(token in text for token in _FINANCIAL)
    if financial:
        return False
    return any(token in text for token in _DEVICE)


def _find_entities(text: str, registry=None) -> tuple[list[EntityMention], list[tuple[str, list]]]:
    from knowledge.registry import default_registry

    source = registry or default_registry()
    records = source.find_in_text(text)
    found = [
        EntityMention(
            canonical_name=record.canonical_name,
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            mention=record.canonical_name,
            share_class=record.share_class,
        )
        for record in records
    ]
    return _unique_entities(found), source.ambiguous_aliases(text)


def _inherit_session_entities(
    text: str,
    entities: list[EntityMention],
    ambiguous: list[tuple[str, list]],
    previous: dict[str, Any],
) -> tuple[list[EntityMention], list[tuple[str, list]]]:
    stored = _entities_from_session(previous)
    switched = any(token in text for token in ("换成", "改成", "切换到", "改为"))
    if entities:
        return entities, [] if switched else ambiguous
    candidate_ids = {
        record.entity_id
        for _alias, records in ambiguous
        for record in records
    }
    if ambiguous and len(stored) == 1 and stored[0].entity_id in candidate_ids:
        return stored, []
    pronoun = any(token in text for token in _PRONOUN)
    if pronoun and stored and not switched:
        return stored, []
    return entities, ambiguous


def _entities_from_session(previous: dict[str, Any]) -> list[EntityMention]:
    mentions: list[EntityMention] = []
    for item in previous.get("resolved_entities") or []:
        if not isinstance(item, dict) or not item.get("canonical_name"):
            continue
        mentions.append(EntityMention(
            canonical_name=str(item["canonical_name"]),
            entity_type=str(item.get("entity_type") or "company"),
            entity_id=str(item.get("entity_id") or f"{item.get('entity_type') or 'company'}:{item['canonical_name']}"),
            mention=str(item["canonical_name"]),
            share_class=item.get("share_class"),
        ))
    return mentions


def _parse_period(text: str) -> dict[str, Any] | None:
    year_match = _YEAR.search(text)
    year = int(year_match.group(1)) if year_match else None
    quarter = None
    if "一季度" in text or "第一季度" in text:
        quarter = 1
    elif "二季度" in text or "第二季度" in text or "半年度" in text:
        quarter = 2
    elif "三季度" in text or "第三季度" in text:
        quarter = 3
    elif "四季度" in text or "第四季度" in text:
        quarter = 4
    if year is None and quarter is None:
        return None
    label = f"{year}年" if year else ""
    if quarter:
        label = f"{label}第{quarter}季度" if year else f"第{quarter}季度"
    return {"year": year, "quarter": quarter, "label": label}


def _copy_period(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {"year": value.get("year"), "quarter": value.get("quarter"), "label": value.get("label") or ""}


def _intent(text: str) -> str:
    if any(token in text for token in ("保证赚钱", "稳赚", "保本", "能不能买", "该买吗")):
        return "advice"
    if any(token in text for token in ("今天", "今日", "最新")) and any(token in text for token in ("净值", "行情")):
        return "freshness"
    if any(token in text for token in ("什么是", "是什么", "是指什么", "含义", "定义", "多久到账")):
        return "concept"
    if "通常" in text and "赎回" in text:
        return "concept"
    if "背离" in text or ("利润" in text and "现金流" in text):
        return "statement"
    if _relation_task(text):
        return "relation"
    if "行业" in text:
        return "industry"
    return "fact"


def _topic(text: str, intent: str) -> str:
    if intent == "concept":
        return "concept"
    if intent == "advice":
        return "advice"
    if intent == "freshness":
        return "freshness"
    if "风险" in text:
        return "risk"
    if any(token in text for token in ("赎回费", "申购费", "手续费", "费率")):
        return "fee"
    if any(token in text for token in ("业绩", "营收", "利润", "营业收入")):
        return "performance"
    if intent == "industry":
        return "industry"
    return "fact"


def _metrics(text: str) -> list[str]:
    metrics = []
    if "营业收入" in text or "营收" in text:
        metrics.append("营业收入")
    if "净利润" in text or "利润" in text:
        metrics.append("净利润")
    if "现金流" in text:
        metrics.append("现金流")
    if "净值" in text:
        metrics.append("净值")
    return metrics


def _route(text: str, entities: list[EntityMention], intent: str) -> tuple[str, str]:
    if intent == "industry" or ("行业" in text and "分析" in text):
        return "research", "industry"
    if intent == "statement":
        return "research", "company_statement"
    dimensions = set()
    if "盈利" in text:
        dimensions.add("profit")
    if "资产质量" in text:
        dimensions.add("asset")
    if "风险" in text:
        dimensions.add("risk")
    if "现金流" in text:
        dimensions.add("cashflow")
    if len(entities) >= 2 and len(dimensions) >= 2:
        return "research", "company_comparison"
    if len(entities) >= 2 and any(token in text for token in ("比较", "对比", "分别")):
        return "program_parallel", "comparison"
    if intent == "concept":
        return "knowledge", "concept"
    if intent == "relation":
        return "knowledge", _relation_task(text) or "product_manager"
    if entities or any(token in text for token in _FINANCIAL):
        task = "company_fact" if entities and entities[0].entity_type == "company" else "fact"
        return "knowledge", task
    return "defer", "product"


def _relation_task(text: str) -> str:
    for task, tokens in _RELATION_TASKS:
        if any(token in text for token in tokens):
            return task
    return ""


def _clarification(alias: str, records: list) -> tuple[str, list[dict[str, str]]]:
    names = "、".join(record.canonical_name for record in records)
    options = [
        {
            "display_name": record.canonical_name,
            "canonical_name": record.canonical_name,
            "entity_id": record.entity_id,
            "entity_type": record.entity_type,
        }
        for record in records
    ]
    return f"“{alias}”对应多个主体：{names}。请说明要查询哪一个。", options


def _needs_subject(text: str, intent: str) -> bool:
    if intent in {"concept", "advice", "freshness", "industry"}:
        return False
    return any(token in text for token in _PRONOUN)


def _carry_session(
    previous: dict[str, Any],
    entities: list[EntityMention],
    period: dict[str, Any] | None,
    topic: str,
) -> dict[str, Any]:
    if entities:
        stored = [entity.as_dict() for entity in entities]
    else:
        stored = [dict(item) for item in previous.get("resolved_entities") or [] if isinstance(item, dict)]
    return {
        "resolved_entities": stored,
        "active_period": period if period is not None else _copy_period(previous.get("active_period")),
        "topic": topic or str(previous.get("topic") or ""),
        "last_evidence_refs": list(previous.get("last_evidence_refs") or []),
    }


def _unique_entities(entities: list[EntityMention]) -> list[EntityMention]:
    chosen: dict[str, EntityMention] = {}
    for entity in entities:
        chosen.setdefault(entity.entity_id, entity)
    return list(chosen.values())
