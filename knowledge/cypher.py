"""固定 Cypher 模板。标签和边类型写在模板里，名称只作为参数。"""

from __future__ import annotations

SCHEMA_STATEMENTS = (
    """
    CREATE CONSTRAINT kg_gid_unique IF NOT EXISTS
    FOR (n:KGNode) REQUIRE n.gid IS UNIQUE
    """,
    """
    CREATE INDEX kg_entity_lookup IF NOT EXISTS
    FOR (n:KGEntity) ON (n.kb_id, n.build_id, n.entity_id)
    """,
    """
    CREATE INDEX kg_assertion_lookup IF NOT EXISTS
    FOR (a:KGAssertion) ON (a.kb_id, a.build_id, a.predicate)
    """,
    """
    CREATE INDEX kg_document_lookup IF NOT EXISTS
    FOR (d:KGDocument) ON (d.kb_id, d.build_id, d.doc_version)
    """,
    """
    CREATE INDEX kg_evidence_lookup IF NOT EXISTS
    FOR (e:KGEvidence) ON (e.kb_id, e.build_id, e.evidence_id)
    """,
)

MERGE_ENTITY = """
UNWIND $rows AS row
MERGE (n:KGNode:KGEntity {gid: row.gid})
SET n.kb_id = $kb_id,
    n.build_id = $build_id,
    n.schema_version = $schema_version,
    n.entity_id = row.entity_id,
    n.entity_type = row.entity_type,
    n.canonical_name = row.canonical_name,
    n.aliases = row.aliases,
    n.registry_status = row.registry_status
"""

MERGE_DOCUMENT = """
UNWIND $rows AS row
MERGE (n:KGNode:KGDocument {gid: row.gid})
SET n.kb_id = $kb_id,
    n.build_id = $build_id,
    n.schema_version = $schema_version,
    n.doc_id = row.doc_id,
    n.doc_version = row.doc_version,
    n.file_name = row.file_name,
    n.content_hash = row.content_hash
"""

MERGE_CHUNK = """
UNWIND $rows AS row
MERGE (n:KGNode:KGChunk {gid: row.gid})
SET n.kb_id = $kb_id,
    n.build_id = $build_id,
    n.schema_version = $schema_version,
    n.chunk_uid = row.chunk_uid,
    n.doc_version = row.doc_version,
    n.vector_record_ref = row.vector_record_ref
"""

MERGE_EVIDENCE = """
UNWIND $rows AS row
MERGE (n:KGNode:KGEvidence {gid: row.gid})
SET n.kb_id = $kb_id,
    n.build_id = $build_id,
    n.schema_version = $schema_version,
    n.evidence_id = row.evidence_id,
    n.doc_version = row.doc_version,
    n.chunk_uid = row.chunk_uid,
    n.quote_hash = row.quote_hash,
    n.vector_record_ref = row.vector_record_ref
"""

LINK_CHUNK = """
UNWIND $rows AS row
MATCH (doc:KGDocument {gid: row.doc_gid, kb_id: $kb_id, build_id: $build_id})
MATCH (chunk:KGChunk {gid: row.chunk_gid, kb_id: $kb_id, build_id: $build_id})
MERGE (doc)-[:HAS_CHUNK]->(chunk)
"""

LINK_EVIDENCE = """
UNWIND $rows AS row
MATCH (evidence:KGEvidence {gid: row.evidence_gid, kb_id: $kb_id, build_id: $build_id})
MATCH (doc:KGDocument {gid: row.doc_gid, kb_id: $kb_id, build_id: $build_id})
MATCH (chunk:KGChunk {gid: row.chunk_gid, kb_id: $kb_id, build_id: $build_id})
MERGE (evidence)-[:LOCATED_IN]->(doc)
MERGE (evidence)-[:RETRIEVABLE_IN]->(chunk)
"""

LINK_MENTION = """
UNWIND $rows AS row
MATCH (entity:KGEntity {gid: row.entity_gid, kb_id: $kb_id, build_id: $build_id})
MATCH (evidence:KGEvidence {gid: row.evidence_gid, kb_id: $kb_id, build_id: $build_id})
MERGE (entity)-[:MENTIONED_IN]->(evidence)
"""

MERGE_ASSERTION = """
UNWIND $rows AS row
MATCH (subject:KGNode {gid: row.subject_gid, kb_id: $kb_id, build_id: $build_id})
MATCH (object:KGNode {gid: row.object_gid, kb_id: $kb_id, build_id: $build_id})
MERGE (assertion:KGNode:KGAssertion {gid: row.gid})
ON CREATE SET
  assertion.kb_id = $kb_id,
  assertion.build_id = $build_id,
  assertion.schema_version = $schema_version,
  assertion.assertion_id = row.assertion_id,
  assertion.predicate = row.predicate,
  assertion.polarity = row.polarity,
  assertion.proposition_key = row.proposition_key,
  assertion.status = row.status,
  assertion.validity_mode = row.validity_mode,
  assertion.valid_from = row.valid_from,
  assertion.valid_to = row.valid_to,
  assertion.available_at = row.available_at,
  assertion.evidence_count = row.evidence_count,
  assertion.payload_hash = row.payload_hash
MERGE (assertion)-[:SUBJECT]->(subject)
MERGE (assertion)-[:OBJECT]->(object)
WITH assertion, row
UNWIND row.evidence_gids AS evidence_gid
MATCH (evidence:KGNode:KGEvidence {gid: evidence_gid, kb_id: $kb_id, build_id: $build_id})
MERGE (assertion)-[:SUPPORTED_BY]->(evidence)
RETURN count(DISTINCT assertion) AS assertion_count
"""

PRODUCT_MANAGER = """
MATCH (product:KGEntity {kb_id: $kb_id, build_id: $build_id, entity_id: $product_id})
MATCH (assertion:KGAssertion {
  kb_id: $kb_id, build_id: $build_id,
  predicate: 'MANAGED_BY', status: 'accepted', polarity: 'affirmed'
})-[:SUBJECT]->(product)
MATCH (assertion)-[:OBJECT]->(manager:KGEntity {kb_id: $kb_id, build_id: $build_id})
MATCH (assertion)-[:SUPPORTED_BY]->(evidence:KGEvidence)-[:LOCATED_IN]->(doc:KGDocument)
RETURN manager.entity_id AS manager_id,
       manager.canonical_name AS manager_name,
       assertion.assertion_id AS assertion_id,
       evidence.evidence_id AS evidence_id,
       doc.doc_version AS doc_version
"""
