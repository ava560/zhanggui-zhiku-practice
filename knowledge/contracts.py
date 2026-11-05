"""图谱数据契约。业务关系是声明，不是实体上的可覆盖属性。"""

from __future__ import annotations

from dataclasses import dataclass, field


SCHEMA_VERSION = "kg-2026-11-05"
RULE_VERSION = "rules-2026-11-05"

ENTITY_TYPES = {
    "organization",
    "fund_series",
    "fund_share",
    "product",
    "industry",
    "concept",
    "company",
    "fund",
    "institution",
}

PREDICATES = {
    "MANAGED_BY": {"subject": {"fund_series", "fund_share", "product", "fund"}, "object": {"organization", "institution", "company"}},
    "ISSUED_BY": {"subject": {"fund_series", "fund_share", "product", "fund"}, "object": {"organization", "institution", "company"}},
    "SUBSIDIARY_OF": {"subject": {"organization", "company"}, "object": {"organization", "company"}},
    "BELONGS_TO": {"subject": {"organization", "company"}, "object": {"industry"}},
    "SHARE_CLASS_OF": {"subject": {"fund_share"}, "object": {"fund_series", "fund"}},
    "REPORTS_ON": {"subject": {"document"}, "object": {"organization", "company"}},
    "DEFINES": {"subject": {"document"}, "object": {"concept"}},
    "APPLIES_TO": {"subject": {"document"}, "object": {"organization", "industry", "product", "company"}},
}

STRUCTURAL_RELATIONS = {
    "HAS_CHUNK",
    "LOCATED_IN",
    "RETRIEVABLE_IN",
    "MENTIONED_IN",
    "SUBJECT",
    "OBJECT",
    "SUPPORTED_BY",
}


@dataclass(frozen=True)
class EntityRecord:
    entity_id: str
    entity_type: str
    canonical_name: str
    aliases: tuple[str, ...] = ()
    status: str = "provisional"
    share_class: str | None = None
    code: str = ""


@dataclass(frozen=True)
class DocumentRecord:
    doc_id: str
    doc_version: str
    file_name: str
    content_hash: str
    source_uri: str = ""


@dataclass(frozen=True)
class ChunkRecord:
    chunk_uid: str
    doc_version: str
    content_hash: str
    vector_record_ref: str = ""
    section: str = ""
    content_type: str = ""


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    doc_version: str
    chunk_uid: str
    quote: str
    quote_hash: str
    vector_record_ref: str = ""


@dataclass(frozen=True)
class AssertionRecord:
    assertion_id: str
    proposition_key: str
    subject_id: str
    predicate: str
    object_id: str
    polarity: str
    status: str
    evidence_ids: tuple[str, ...]
    quote: str
    validity_mode: str = "unknown"
    valid_from: str = ""
    valid_to: str = ""
    available_at: str = ""
    rule_version: str = RULE_VERSION
    payload_hash: str = ""


@dataclass
class GraphProjection:
    kb_id: str = "default"
    build_id: str = "staging"
    schema_version: str = SCHEMA_VERSION
    entities: list[EntityRecord] = field(default_factory=list)
    documents: list[DocumentRecord] = field(default_factory=list)
    chunks: list[ChunkRecord] = field(default_factory=list)
    evidences: list[EvidenceRecord] = field(default_factory=list)
    assertions: list[AssertionRecord] = field(default_factory=list)
    mentions: list[tuple[str, str]] = field(default_factory=list)

    def accepted(self, predicate: str | None = None, polarity: str = "affirmed") -> list[AssertionRecord]:
        rows = [
            item for item in self.assertions
            if item.status == "accepted" and item.polarity == polarity
        ]
        if predicate is None:
            return rows
        return [item for item in rows if item.predicate == predicate]
