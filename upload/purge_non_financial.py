"""删除知识库里与金融无关的记录。只按文件名和商品名判断，不扫描正文臆测。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from upload.corpus_scope import is_non_financial
from upload.node_import_milvus import escape_milvus_string

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def dropped_file_names(records: list[dict]) -> list[str]:
    names = []
    seen: set[str] = set()
    for record in records:
        file_name = str(record.get("file_name") or "").strip()
        item_name = str(record.get("item_name") or "").strip()
        if not file_name or file_name in seen:
            continue
        if is_non_financial(file_name, item_name):
            seen.add(file_name)
            names.append(file_name)
    return names


def milvus_file_filters(file_names: list[str], fields: set[str] | None = None) -> list[str]:
    filters = []
    available = fields or {"file_name", "file_title"}
    for file_name in file_names:
        escaped = escape_milvus_string(file_name)
        if "file_name" in available:
            filters.append(f'file_name == "{escaped}"')
        if "file_title" in available:
            filters.append(f'file_title == "{escaped}"')
    return filters


def minio_object_matches(object_name: str, file_names: list[str]) -> bool:
    compact = object_name.replace(" ", "")
    for file_name in file_names:
        stem = Path(file_name).stem.replace(" ", "")
        if stem.endswith("_new"):
            stem = stem[: -len("_new")]
        if len(stem) >= 8 and stem in compact:
            return True
    return False


def delete_milvus_files(client, collection: str, file_names: list[str], fields: set[str] | None = None) -> int:
    deleted = 0
    client.load_collection(collection_name=collection)
    for expr in milvus_file_filters(file_names, fields):
        client.delete(collection_name=collection, filter=expr)
        deleted += 1
        logger.info("已按文件删除 Milvus 记录: %s", expr)
    if hasattr(client, "flush"):
        try:
            client.flush(collection_name=collection)
        except Exception as exc:
            logger.warning("flush 失败，删除已提交: %s", type(exc).__name__)
    return deleted


def delete_graph_files(session, file_names: list[str], item_names: list[str]) -> None:
    """删掉设备手册对应的文件节点，以及不再指向任何文件的商品名。"""
    session.run(
        """
        MATCH (evidence:Evidence)
        WHERE evidence.file_name IN $names
        MATCH (relation:BusinessRelation)-[:SUPPORTED_BY]->(evidence)
        DETACH DELETE relation
        """,
        names=file_names,
    )
    session.run(
        """
        MATCH (evidence:Evidence)
        WHERE evidence.file_name IN $names
        WITH collect(evidence.chunk_id) AS chunk_ids
        MATCH ()-[rel]->()
        WHERE rel.chunk_id IN chunk_ids AND rel.chunk_id <> ''
        DELETE rel
        """,
        names=file_names,
    )
    session.run(
        """
        MATCH (node)
        WHERE (node:Evidence OR node:Document) AND node.file_name IN $names
        DETACH DELETE node
        """,
        names=file_names,
    )
    session.run(
        """
        MATCH (file:SourceFile)
        WHERE file.name IN $names
        DETACH DELETE file
        """,
        names=file_names,
    )
    session.run(
        """
        MATCH (item:Item)
        WHERE item.name IN $item_names
          AND NOT (item)-[:DESCRIBED_IN]->(:SourceFile)
        DETACH DELETE item
        """,
        item_names=item_names,
    )


def delete_minio_objects(client, bucket: str, file_names: list[str]) -> int:
    from minio.deleteobjects import DeleteObject

    matched = []
    for obj in client.list_objects(bucket, recursive=True):
        name = str(getattr(obj, "object_name", "") or "")
        if name and minio_object_matches(name, file_names):
            matched.append(DeleteObject(name))
    if not matched:
        return 0
    errors = list(client.remove_objects(bucket, matched))
    for error in errors:
        logger.error("MinIO 删除失败: %s", type(error).__name__)
    return len(matched) - len(errors)


def _query_pairs(client, collection: str) -> list[dict]:
    client.load_collection(collection_name=collection)
    rows = client.query(
        collection_name=collection,
        filter='file_name != ""',
        output_fields=["file_name", "item_name"],
        limit=16384,
    )
    return list(rows)


def purge_non_financial(*, include_minio: bool = True) -> dict[str, list[str]]:
    """删除 Milvus、Neo4j 和 MinIO 中的非金融知识库记录。不处理聊天记录。"""
    from graph_store import _database_name, _driver
    from upload.node_import_milvus import get_milvus_client, load_settings
    from upload.node_item_name_recognition import load_settings as load_item_settings

    chunk_settings = load_settings()
    item_settings = load_item_settings()
    client = get_milvus_client(chunk_settings)
    chunk_rows = _query_pairs(client, chunk_settings.chunks_collection)
    item_rows = _query_pairs(client, item_settings.item_name_collection)
    file_names = dropped_file_names([*chunk_rows, *item_rows])
    item_names = []
    seen_items: set[str] = set()
    for row in [*chunk_rows, *item_rows]:
        file_name = str(row.get("file_name") or "")
        item_name = str(row.get("item_name") or "").strip()
        if file_name in file_names and item_name and item_name not in seen_items:
            seen_items.add(item_name)
            item_names.append(item_name)

    if not file_names:
        logger.info("没有需要删除的非金融知识库文件")
        return {"files": [], "items": []}

    delete_milvus_files(client, chunk_settings.chunks_collection, file_names)
    delete_milvus_files(client, item_settings.item_name_collection, file_names)

    driver = _driver()
    try:
        driver.verify_connectivity()
        with driver.session(database=_database_name()) as session:
            delete_graph_files(session, file_names, item_names)
    finally:
        driver.close()

    if include_minio:
        _purge_minio(file_names)
    logger.info("已删除非金融知识库文件 %d 个", len(file_names))
    return {"files": file_names, "items": item_names}


def _purge_minio(file_names: list[str]) -> None:
    endpoint = os.getenv("MINIO_ENDPOINT", "").strip()
    access_key = os.getenv("MINIO_ACCESS_KEY", "").strip()
    secret_key = os.getenv("MINIO_SECRET_KEY", "").strip()
    bucket = os.getenv("MINIO_BUCKET_NAME", "").strip()
    if not all((endpoint, access_key, secret_key, bucket)):
        logger.warning("MinIO 配置不完整，跳过对象清理")
        return
    from minio import Minio

    secure = os.getenv("MINIO_SECURE", "false").strip().lower() in {"1", "true", "yes"}
    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    removed = delete_minio_objects(client, bucket, file_names)
    logger.info("已删除 MinIO 对象 %d 个", removed)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = purge_non_financial()
    print("DELETED_FILES")
    for name in result["files"]:
        print(name)
    print("DELETED_ITEMS")
    for name in result["items"]:
        print(name)


if __name__ == "__main__":
    main()
