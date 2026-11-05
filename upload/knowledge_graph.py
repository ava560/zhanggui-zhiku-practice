"""把审核后的声明投影到 Neo4j。实体来自原文，不依赖固定公司名单。"""

from __future__ import annotations

import logging

from knowledge.contracts import GraphProjection
from knowledge.cypher import (
    LINK_CHUNK,
    LINK_EVIDENCE,
    LINK_MENTION,
    MERGE_ASSERTION,
    MERGE_CHUNK,
    MERGE_DOCUMENT,
    MERGE_ENTITY,
    MERGE_EVIDENCE,
    SCHEMA_STATEMENTS,
)
from knowledge.ids import gid
from knowledge.project import build_projection, projection_conflicts
from knowledge.registry import EntityRegistry, default_registry

logger = logging.getLogger(__name__)


class ProjectionConflict(RuntimeError):
    """同一 gid 已存在不同内容，必须换新的声明或构建版本。"""


def build_financial_graph(chunks: list[dict]) -> GraphProjection:
    return build_projection(chunks, registry=EntityRegistry(seeded=False))


def write_financial_graph(
    graph: GraphProjection,
    session=None,
    existing_hashes: dict[str, str] | None = None,
) -> int:
    conflicts = projection_conflicts(graph, existing_hashes or {})
    if conflicts:
        raise ProjectionConflict("声明内容与已有 gid 不一致")
    if not graph.documents and not graph.entities and not graph.assertions:
        return 0

    owns_driver = session is None
    driver = None
    if owns_driver:
        from graph_store import _database_name, _driver

        driver = _driver()
        driver.verify_connectivity()
        session = driver.session(database=_database_name())
    try:
        for statement in SCHEMA_STATEMENTS:
            session.run(statement)
        scope = {
            "kb_id": graph.kb_id,
            "build_id": graph.build_id,
            "schema_version": graph.schema_version,
        }
        _run(session, MERGE_ENTITY, {**scope, "rows": _entity_rows(graph)})
        _run(session, MERGE_DOCUMENT, {**scope, "rows": _document_rows(graph)})
        _run(session, MERGE_CHUNK, {**scope, "rows": _chunk_rows(graph)})
        _run(session, MERGE_EVIDENCE, {**scope, "rows": _evidence_rows(graph)})
        _run(session, LINK_CHUNK, {**scope, "rows": _chunk_links(graph)})
        _run(session, LINK_EVIDENCE, {**scope, "rows": _evidence_links(graph)})
        _run(session, LINK_MENTION, {**scope, "rows": _mention_rows(graph)})
        _run(session, MERGE_ASSERTION, {**scope, "rows": _assertion_rows(graph)})
        return len(graph.assertions)
    finally:
        if owns_driver and session is not None:
            session.close()
        if driver is not None:
            driver.close()


def node_build_knowledge_graph(state: dict) -> dict:
    """Milvus 回填之后写入当前构建版本。图不可用时保留已入库文本。"""
    chunks = list(state.get("chunks") or [])
    graph = build_projection(chunks, registry=default_registry(), build_id=str(state.get("task_id") or "staging"))
    state["graph_build_id"] = graph.build_id
    state["accepted_count"] = len(graph.accepted())
    state["pending_count"] = len([item for item in graph.assertions if item.status == "pending"])
    if not graph.documents:
        state["graph_status"] = "empty"
        return state
    try:
        write_financial_graph(graph)
    except Exception as exc:
        logger.warning("知识图谱未写入，文本检索仍可使用：%s", type(exc).__name__)
        state["graph_status"] = "unavailable"
        return state
    state["graph_status"] = "ready"
    logger.info(
        "知识图谱已写入实体 %d 个、声明 %d 条",
        len(graph.entities),
        len(graph.assertions),
    )
    return state


def _run(session, statement: str, params: dict) -> None:
    if "rows" in params and not params["rows"]:
        return
    session.run(statement, **params)


def _entity_rows(graph: GraphProjection) -> list[dict]:
    return [
        {
            "gid": gid(graph.kb_id, graph.build_id, "KGEntity", item.entity_id),
            "entity_id": item.entity_id,
            "entity_type": item.entity_type,
            "canonical_name": item.canonical_name,
            "aliases": list(item.aliases),
            "registry_status": item.status,
        }
        for item in graph.entities
    ]


def _document_rows(graph: GraphProjection) -> list[dict]:
    return [
        {
            "gid": gid(graph.kb_id, graph.build_id, "KGDocument", item.doc_version),
            "doc_id": item.doc_id,
            "doc_version": item.doc_version,
            "file_name": item.file_name,
            "content_hash": item.content_hash,
        }
        for item in graph.documents
    ]


def _chunk_rows(graph: GraphProjection) -> list[dict]:
    return [
        {
            "gid": gid(graph.kb_id, graph.build_id, "KGChunk", item.chunk_uid),
            "chunk_uid": item.chunk_uid,
            "doc_version": item.doc_version,
            "vector_record_ref": item.vector_record_ref,
        }
        for item in graph.chunks
    ]


def _evidence_rows(graph: GraphProjection) -> list[dict]:
    return [
        {
            "gid": gid(graph.kb_id, graph.build_id, "KGEvidence", item.evidence_id),
            "evidence_id": item.evidence_id,
            "doc_version": item.doc_version,
            "chunk_uid": item.chunk_uid,
            "quote_hash": item.quote_hash,
            "vector_record_ref": item.vector_record_ref,
        }
        for item in graph.evidences
    ]


def _chunk_links(graph: GraphProjection) -> list[dict]:
    documents = {item.doc_version: item for item in graph.documents}
    rows = []
    for chunk in graph.chunks:
        if chunk.doc_version not in documents:
            continue
        rows.append({
            "doc_gid": gid(graph.kb_id, graph.build_id, "KGDocument", chunk.doc_version),
            "chunk_gid": gid(graph.kb_id, graph.build_id, "KGChunk", chunk.chunk_uid),
        })
    return rows


def _evidence_links(graph: GraphProjection) -> list[dict]:
    chunks = {item.chunk_uid: item for item in graph.chunks}
    rows = []
    for evidence in graph.evidences:
        chunk = chunks.get(evidence.chunk_uid)
        if chunk is None:
            continue
        rows.append({
            "evidence_gid": gid(graph.kb_id, graph.build_id, "KGEvidence", evidence.evidence_id),
            "doc_gid": gid(graph.kb_id, graph.build_id, "KGDocument", evidence.doc_version),
            "chunk_gid": gid(graph.kb_id, graph.build_id, "KGChunk", evidence.chunk_uid),
        })
    return rows


def _mention_rows(graph: GraphProjection) -> list[dict]:
    seen = set()
    rows = []
    for entity_id, evidence_id in graph.mentions:
        key = (entity_id, evidence_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "entity_gid": gid(graph.kb_id, graph.build_id, "KGEntity", entity_id),
            "evidence_gid": gid(graph.kb_id, graph.build_id, "KGEvidence", evidence_id),
        })
    return rows


def _assertion_rows(graph: GraphProjection) -> list[dict]:
    documents = {item.doc_version for item in graph.documents}
    rows = []
    for item in graph.assertions:
        if item.status != "accepted":
            continue
        subject_kind = "KGDocument" if item.subject_id in documents else "KGEntity"
        rows.append({
            "gid": gid(graph.kb_id, graph.build_id, "KGAssertion", item.assertion_id),
            "subject_gid": gid(graph.kb_id, graph.build_id, subject_kind, item.subject_id),
            "object_gid": gid(graph.kb_id, graph.build_id, "KGEntity", item.object_id),
            "assertion_id": item.assertion_id,
            "predicate": item.predicate,
            "polarity": item.polarity,
            "proposition_key": item.proposition_key,
            "status": item.status,
            "validity_mode": item.validity_mode,
            "valid_from": item.valid_from,
            "valid_to": item.valid_to,
            "available_at": item.available_at,
            "evidence_count": len(item.evidence_ids),
            "payload_hash": item.payload_hash,
            "evidence_gids": [
                gid(graph.kb_id, graph.build_id, "KGEvidence", evidence_id)
                for evidence_id in item.evidence_ids
            ],
        })
    return rows
