"""把抽取结果投影成可写入 Neo4j 的文档、证据和声明。"""

from __future__ import annotations

from knowledge.contracts import (
    PREDICATES,
    AssertionRecord,
    ChunkRecord,
    DocumentRecord,
    EvidenceRecord,
    GraphProjection,
)
from knowledge.extract import NamedEntity, RelationCandidate, extract_chunk
from knowledge.ids import digest, gid, short_id
from knowledge.registry import EntityRegistry
from upload.corpus_scope import is_non_financial


def build_projection(
    chunks: list[dict],
    *,
    registry: EntityRegistry | None = None,
    kb_id: str = "default",
    build_id: str = "staging",
) -> GraphProjection:
    registry = registry or EntityRegistry(seeded=False)
    grouped: dict[str, list[dict]] = {}
    for chunk in chunks:
        grouped.setdefault(str(chunk.get("file_name") or ""), []).append(chunk)

    projection = GraphProjection(kb_id=kb_id, build_id=build_id)
    entity_ids: dict[tuple[str, str], str] = {}

    for file_name, group in grouped.items():
        item_name = str(group[0].get("item_name") or "")
        if not file_name or is_non_financial(file_name, item_name):
            continue
        body = "\n".join(str(chunk.get("content") or "") for chunk in group)
        content_hash = digest(body)
        doc_id = short_id("doc", file_name)
        doc_version = short_id("ver", doc_id, content_hash)
        projection.documents.append(DocumentRecord(
            doc_id=doc_id,
            doc_version=doc_version,
            file_name=file_name,
            content_hash=content_hash,
            source_uri=str(group[0].get("source_uri") or ""),
        ))
        for index, chunk in enumerate(group):
            content = str(chunk.get("content") or "")
            if not content:
                continue
            chunk_hash = digest(content)
            chunk_uid = short_id("chunk", doc_version, str(index), chunk_hash)
            vector_ref = str(chunk.get("chunk_id") or "")
            projection.chunks.append(ChunkRecord(
                chunk_uid=chunk_uid,
                doc_version=doc_version,
                content_hash=chunk_hash,
                vector_record_ref=vector_ref,
                section=str(chunk.get("section") or ""),
                content_type=str(chunk.get("content_type") or ""),
            ))
            mention_evidence = _evidence(projection, doc_version, chunk_uid, content, vector_ref)
            entities, candidates = extract_chunk(content, registry)
            for entity in entities:
                entity_id = _entity_id(projection, registry, entity_ids, entity)
                projection.mentions.append((entity_id, mention_evidence.evidence_id))
            for candidate in candidates:
                _add_assertion(
                    projection, registry, entity_ids, candidate, doc_version, chunk_uid, vector_ref,
                )
    _drop_incomplete(projection)
    return projection


def projection_conflicts(projection: GraphProjection, existing_hashes: dict[str, str]) -> list[str]:
    conflicts = []
    for assertion in projection.assertions:
        key = gid(projection.kb_id, projection.build_id, "KGAssertion", assertion.assertion_id)
        previous = existing_hashes.get(key)
        if previous and previous != assertion.payload_hash:
            conflicts.append(assertion.assertion_id)
    return conflicts


def _add_assertion(
    projection: GraphProjection,
    registry: EntityRegistry,
    entity_ids: dict[tuple[str, str], str],
    candidate: RelationCandidate,
    doc_version: str,
    chunk_uid: str,
    vector_ref: str,
) -> None:
    if candidate.predicate not in PREDICATES or candidate.status == "rejected":
        return
    subject_id = "document" if candidate.subject.entity_type == "document" else _entity_id(
        projection, registry, entity_ids, candidate.subject,
    )
    object_id = _entity_id(projection, registry, entity_ids, candidate.object)
    if candidate.predicate == "REPORTS_ON" or candidate.predicate in {"DEFINES", "APPLIES_TO"}:
        subject_id = projection.documents[-1].doc_version
    evidence = _evidence(projection, doc_version, chunk_uid, candidate.quote, vector_ref)
    evidence_ids = (evidence.evidence_id,)
    proposition = short_id(
        "prop", candidate.predicate, subject_id, object_id, candidate.polarity,
    )
    assertion_id = short_id(
        "assert", subject_id, candidate.predicate, object_id, candidate.polarity,
        ",".join(evidence_ids), candidate.rule_id,
    )
    payload = digest(
        assertion_id, subject_id, candidate.predicate, object_id, candidate.polarity,
        candidate.status, ",".join(evidence_ids), candidate.quote,
    )
    projection.assertions.append(AssertionRecord(
        assertion_id=assertion_id,
        proposition_key=proposition,
        subject_id=subject_id,
        predicate=candidate.predicate,
        object_id=object_id,
        polarity=candidate.polarity,
        status=candidate.status,
        evidence_ids=evidence_ids,
        quote=candidate.quote,
        payload_hash=payload,
    ))
    if candidate.subject.entity_type != "document":
        projection.mentions.append((subject_id, evidence.evidence_id))
    projection.mentions.append((object_id, evidence.evidence_id))


def _entity_id(
    projection: GraphProjection,
    registry: EntityRegistry,
    entity_ids: dict[tuple[str, str], str],
    entity: NamedEntity,
) -> str:
    key = (entity.entity_type, entity.name)
    if key in entity_ids:
        return entity_ids[key]
    record = registry.provisional(entity.entity_type, entity.name, share_class=entity.share_class)
    entity_ids[key] = record.entity_id
    if not any(item.entity_id == record.entity_id for item in projection.entities):
        projection.entities.append(record)
    return record.entity_id


def _evidence(
    projection: GraphProjection,
    doc_version: str,
    chunk_uid: str,
    quote: str,
    vector_ref: str,
) -> EvidenceRecord:
    quote_hash = digest(quote)
    evidence_id = short_id("ev", doc_version, quote_hash)
    for existing in projection.evidences:
        if existing.evidence_id == evidence_id:
            return existing
    record = EvidenceRecord(
        evidence_id=evidence_id,
        doc_version=doc_version,
        chunk_uid=chunk_uid,
        quote=quote,
        quote_hash=quote_hash,
        vector_record_ref=vector_ref,
    )
    projection.evidences.append(record)
    return record


def _drop_incomplete(projection: GraphProjection) -> None:
    entity_ids = {item.entity_id for item in projection.entities}
    document_ids = {item.doc_version for item in projection.documents}
    evidence_ids = {item.evidence_id for item in projection.evidences}
    kept = []
    for assertion in projection.assertions:
        subject_ok = assertion.subject_id in document_ids or assertion.subject_id in entity_ids
        object_ok = assertion.object_id in entity_ids
        evidence_ok = all(item in evidence_ids for item in assertion.evidence_ids)
        if subject_ok and object_ok and evidence_ok:
            kept.append(assertion)
    projection.assertions = kept
