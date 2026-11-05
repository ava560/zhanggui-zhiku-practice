"""从原文抽取实体和关系声明。同页共现不会变成管理或控股。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge.contracts import RULE_VERSION
from knowledge.registry import EntityRegistry


_BANK = re.compile(r"[\u4e00-\u9fff]{2,12}银行")
_FUND = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{2,40}?证券投资基金")
_SHARE_TITLE = re.compile(
    r"(?P<series>[\u4e00-\u9fffA-Za-z0-9]{2,40}?证券投资基金)（(?P<share>[^）]{2,30})）"
)
_ORG_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司")
_ORG_STOP = set("系为是的与和及、，,。；;：:（）() \n\t")
_MANAGER_MARKERS = ("理财产品管理人", "基金管理人", "产品管理人")
_ISSUER_MARKERS = ("产品发行人", "发行人")
_CONTROL = (
    re.compile(r"(?P<child>[\u4e00-\u9fffA-Za-z0-9]{2,40})(?:系|为|是)(?P<parent>[\u4e00-\u9fffA-Za-z0-9]{2,40})的(?:控股子公司|全资子公司)"),
    re.compile(r"(?P<parent>[\u4e00-\u9fffA-Za-z0-9]{2,40})控股(?P<child>[\u4e00-\u9fffA-Za-z0-9]{2,40})"),
)
_REPORT_KIND = re.compile(r"第一季度报告|第三季度报告|半年度报告|中期报告|年度报告|季度报告")
_DEFINES = re.compile(r"所称[“\"](?P<name>[^”\"]{2,12})[”\"][，,]是指(?P<body>[^。]{4,80})")
_APPLIES = re.compile(r"本(?:办法|规定|通知)适用于(?P<scope>[^。；;]{2,40})")
_INDUSTRY = re.compile(
    r"所属(?P<taxonomy>申万|中信|证监会)(?P<version>20\d{2})?(?:行业)?(?:分类)?[:：]\s*"
    r"(?P<name>[\u4e00-\u9fffA-Za-z0-9]{2,20})(?:[（(](?P<code>[A-Za-z0-9]+)[）)])?"
)
_VAGUE = ("各类", "相关", "其他", "有关", "等")
_PLANNED = ("拟由", "拟担任", "拟聘")
_NEGATED = ("不再担任", "已不再", "不再由", "曾由", "曾任", "曾担任")


@dataclass(frozen=True)
class NamedEntity:
    name: str
    entity_type: str
    share_class: str | None = None


@dataclass(frozen=True)
class RelationCandidate:
    subject: NamedEntity
    predicate: str
    object: NamedEntity
    polarity: str
    quote: str
    status: str
    rule_id: str = RULE_VERSION


def extract_chunk(content: str, registry: EntityRegistry) -> tuple[list[NamedEntity], list[RelationCandidate]]:
    text = content or ""
    entities = _entities(text)
    candidates = _unique_candidates([
        *_share_classes(text),
        *_management(text, entities),
        *_issued(text, entities),
        *_control(text),
        *_reports(text),
        *_defines(text),
        *_applies(text),
        *_industry(text, entities),
    ])
    for entity in entities:
        _register(registry, entity)
    for candidate in candidates:
        _register(registry, candidate.subject)
        _register(registry, candidate.object)
    return entities, candidates


def _entities(text: str) -> list[NamedEntity]:
    found: list[NamedEntity] = []
    seen: set[str] = set()

    def add(entity: NamedEntity) -> None:
        if entity.name and entity.name not in seen:
            seen.add(entity.name)
            found.append(entity)

    for match in _SHARE_TITLE.finditer(text):
        share = match.group("share").strip()
        series = match.group("series").strip()
        add(NamedEntity(series, "fund_series"))
        if _is_share_name(share):
            add(NamedEntity(share, "fund_share", share_class=_share_letter(share)))
    for match in _FUND.finditer(text):
        add(NamedEntity(match.group().strip(), "fund_series"))
    for name in _organization_names(text):
        add(NamedEntity(name, "organization"))
    for match in _BANK.finditer(text):
        name = match.group().strip()
        if text[match.end():match.end() + 2] == "行业":
            continue
        if any(name in item.name and name != item.name for item in found):
            continue
        add(NamedEntity(name, "organization"))
    return found


def _share_classes(text: str) -> list[RelationCandidate]:
    rows = []
    for match in _SHARE_TITLE.finditer(text):
        share = match.group("share").strip()
        series = match.group("series").strip()
        if not _is_share_name(share):
            continue
        rows.append(_candidate(
            NamedEntity(share, "fund_share", _share_letter(share)),
            "SHARE_CLASS_OF",
            NamedEntity(series, "fund_series"),
            "affirmed",
            match.group(),
            text,
        ))
    return rows


def _management(text: str, entities: list[NamedEntity]) -> list[RelationCandidate]:
    return _role_relations(text, entities, _MANAGER_MARKERS, "MANAGED_BY")


def _issued(text: str, entities: list[NamedEntity]) -> list[RelationCandidate]:
    return _role_relations(text, entities, _ISSUER_MARKERS, "ISSUED_BY")


def _role_relations(
    text: str,
    entities: list[NamedEntity],
    markers: tuple[str, ...],
    predicate: str,
) -> list[RelationCandidate]:
    products = [item for item in entities if item.entity_type in {"fund_series", "fund_share", "product"}]
    rows = []
    for sentence in _sentences(text):
        if not any(marker in sentence for marker in markers):
            continue
        organization = _organization_near_role(sentence, markers)
        if not organization:
            continue
        subject = _role_subject(sentence, products)
        if subject is None:
            continue
        rows.append(_candidate(
            subject,
            predicate,
            NamedEntity(organization, "organization"),
            _polarity(sentence),
            sentence.strip(),
            text,
        ))
    return rows


def _control(text: str) -> list[RelationCandidate]:
    rows = []
    for sentence in _sentences(text):
        if "持股" in sentence and "控股" not in sentence and "子公司" not in sentence:
            continue
        names = _organization_names(sentence)
        for pattern in _CONTROL:
            match = pattern.search(sentence)
            if not match:
                continue
            child = match.group("child").strip()
            parent = match.group("parent").strip()
            if child not in names or parent not in names or child == parent:
                continue
            rows.append(_candidate(
                NamedEntity(child, "organization"),
                "SUBSIDIARY_OF",
                NamedEntity(parent, "organization"),
                _polarity(sentence),
                sentence.strip(),
                text,
            ))
    return rows


def _organization_names(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    taken_ends: set[int] = set()
    for suffix in _ORG_SUFFIXES:
        start = 0
        while True:
            index = text.find(suffix, start)
            if index < 0:
                break
            end = index + len(suffix)
            start = end
            if end in taken_ends:
                continue
            cursor = index - 1
            while cursor >= 0 and text[cursor] not in _ORG_STOP and _name_char(text[cursor]) and index - cursor <= 30:
                cursor -= 1
            name_start = cursor + 1
            if index - name_start < 2:
                continue
            taken_ends.add(end)
            found.append((name_start, text[name_start:end]))
    found.sort(key=lambda item: item[0])
    names = []
    for _start, name in found:
        if name not in names:
            names.append(name)
    for match in _BANK.finditer(text):
        if text[match.end():match.end() + 2] == "行业":
            continue
        name = match.group().strip()
        if any(name in longer and name != longer for longer in names):
            continue
        if name not in names:
            names.append(name)
    return names


def _name_char(value: str) -> bool:
    return bool(re.match(r"[\u4e00-\u9fffA-Za-z0-9]", value))


def _unique_candidates(candidates: list[RelationCandidate]) -> list[RelationCandidate]:
    unique = []
    seen: set[tuple[str, str, str, str]] = set()
    for candidate in candidates:
        key = (candidate.subject.name, candidate.predicate, candidate.object.name, candidate.polarity)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _reports(text: str) -> list[RelationCandidate]:
    rows = []
    for name in _organization_names(text):
        start = 0
        while True:
            index = text.find(name, start)
            if index < 0:
                break
            window = text[index + len(name): index + len(name) + 16]
            kind = _REPORT_KIND.search(window)
            if kind:
                quote = text[index: index + len(name) + kind.end()]
                rows.append(_candidate(
                    NamedEntity("document", "document"),
                    "REPORTS_ON",
                    NamedEntity(name, "organization"),
                    "affirmed",
                    quote,
                    text,
                ))
            start = index + len(name)
    return rows


def _defines(text: str) -> list[RelationCandidate]:
    rows = []
    for match in _DEFINES.finditer(text):
        rows.append(_candidate(
            NamedEntity("document", "document"),
            "DEFINES",
            NamedEntity(match.group("name").strip(), "concept"),
            "affirmed",
            match.group().strip(),
            text,
        ))
    return rows


def _applies(text: str) -> list[RelationCandidate]:
    rows = []
    for match in _APPLIES.finditer(text):
        scope = match.group("scope").strip()
        status = "pending" if any(token in scope for token in _VAGUE) else "accepted"
        candidate = _candidate(
            NamedEntity("document", "document"),
            "APPLIES_TO",
            NamedEntity(scope, "organization"),
            "affirmed",
            match.group().strip(),
            text,
        )
        if status == "pending":
            candidate = RelationCandidate(
                candidate.subject, candidate.predicate, candidate.object,
                candidate.polarity, candidate.quote, "pending", candidate.rule_id,
            )
        rows.append(candidate)
    return rows


def _industry(text: str, entities: list[NamedEntity]) -> list[RelationCandidate]:
    companies = [item for item in entities if item.entity_type == "organization"]
    rows = []
    for sentence in _sentences(text):
        match = _INDUSTRY.search(sentence)
        if not match:
            continue
        code = (match.group("code") or "").strip()
        version = match.group("version") or ""
        taxonomy = match.group("taxonomy")
        name = match.group("name").strip()
        identity = f"{taxonomy}{version}:{code}:{name}" if code else f"{taxonomy}{version}:{name}"
        mentioned = [item for item in companies if item.name in sentence]
        if len(mentioned) != 1:
            continue
        status = "accepted" if code else "pending"
        candidate = _candidate(
            mentioned[0],
            "BELONGS_TO",
            NamedEntity(identity, "industry"),
            "affirmed",
            sentence.strip(),
            text,
        )
        if status == "pending":
            candidate = RelationCandidate(
                candidate.subject, candidate.predicate, candidate.object,
                candidate.polarity, candidate.quote, "pending", candidate.rule_id,
            )
        rows.append(candidate)
    return rows


def _candidate(
    subject: NamedEntity,
    predicate: str,
    obj: NamedEntity,
    polarity: str,
    quote: str,
    content: str,
) -> RelationCandidate:
    subject_bound = subject.entity_type == "document" or subject.name in quote
    object_bound = obj.entity_type == "industry" or obj.name in quote
    status = "accepted" if quote in content and subject_bound and object_bound else "rejected"
    return RelationCandidate(subject, predicate, obj, polarity, quote, status)


def _role_subject(sentence: str, products: list[NamedEntity]) -> NamedEntity | None:
    present = [item for item in products if item.name in sentence]
    shares = [item for item in present if item.entity_type == "fund_share"]
    if len(shares) == 1:
        return shares[0]
    series = [item for item in present if item.entity_type == "fund_series"]
    if len(series) == 1 and not shares:
        return series[0]
    if len(present) == 1:
        return present[0]
    return None


def _organization_near_role(sentence: str, markers: tuple[str, ...]) -> str:
    marker = next((item for item in markers if item in sentence), "")
    if not marker:
        return ""
    before, after = sentence.split(marker, 1)
    for region in (after, before):
        names = _organization_names(region)
        if names:
            return names[0]
    return ""


def _polarity(sentence: str) -> str:
    if any(token in sentence for token in _PLANNED):
        return "planned"
    if any(token in sentence for token in _NEGATED):
        return "negated"
    return "affirmed"


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。；;\n])", text)
    return [part.strip() for part in parts if part.strip()]


def _is_share_name(name: str) -> bool:
    return bool(re.search(r"[A-Z]$", name)) or "类份额" in name


def _share_letter(name: str) -> str | None:
    match = re.search(r"([A-Z])$", name)
    return match.group(1) if match else None


def _register(registry: EntityRegistry, entity: NamedEntity) -> None:
    if entity.entity_type == "document" or entity.name == "document":
        return
    registry.provisional(entity.entity_type, entity.name, share_class=entity.share_class)
