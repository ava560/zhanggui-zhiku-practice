"""将向量化后的 chunk 导入 Milvus，并回填自动生成的 chunk_id。"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from state import ImportGraphState, format_state

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
CHUNKS_JSON = OUTPUT_DIR / "chunks.json"
VARCHAR_MAX = 65535
INSERT_BATCH_SIZE = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    milvus_url: str
    milvus_token: str
    chunks_collection: str
    embedding_dim: int


def load_settings() -> Settings:
    milvus_url = os.getenv("MILVUS_URL", "").strip()
    collection = os.getenv("CHUNKS_COLLECTION", "").strip()
    missing = [
        name
        for name, value in (("MILVUS_URL", milvus_url), ("CHUNKS_COLLECTION", collection))
        if not value
    ]
    if missing:
        raise SystemExit(f"启动失败：缺少 Milvus 配置 {', '.join(missing)}。")

    try:
        embedding_dim = int(os.getenv("EMBEDDING_DIM", "1024"))
    except ValueError:
        embedding_dim = 1024

    return Settings(
        milvus_url=milvus_url,
        milvus_token=os.getenv("MILVUS_TOKEN", "").strip(),
        chunks_collection=collection,
        embedding_dim=embedding_dim,
    )


def list_chunk_files(output_dir: Path) -> list[Path]:
    files = sorted(
        path
        for path in output_dir.glob("*_new_chunks.json")
        if path.is_file()
    )
    logger.info("在 %s 中找到 %d 个 *_new_chunks.json 文件", output_dir, len(files))
    return files


def load_chunks(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("chunks"), list):
        data = data["chunks"]
    if not isinstance(data, list):
        raise ValueError(f"JSON 不是 chunk 列表: {path.name}")
    return [item for item in data if isinstance(item, dict)]


def write_json(path: Path, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    last_error: OSError | None = None
    for attempt in range(3):
        try:
            path.write_text(payload, encoding="utf-8")
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    if path == CHUNKS_JSON:
        logger.warning("汇总 JSON 暂不可写，单文件结果已保留: %s", last_error)
        return
    raise last_error if last_error is not None else OSError(f"无法写入 {path}")


def escape_milvus_string(value: str) -> str:
    text = str(value or "")
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def clip_varchar(value: Any, max_length: int = VARCHAR_MAX) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_length:
        return text
    return text[:max_length]


def resolve_file_title(chunk: dict) -> str:
    return clip_varchar(
        chunk.get("file_title")
        or chunk.get("file_name")
        or chunk.get("title")
        or ""
    ).strip()


def to_int_part(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def sparse_for_milvus(sparse: Any) -> dict[int, float]:
    if not isinstance(sparse, dict):
        return {}
    return {
        int(key): float(value)
        for key, value in sparse.items()
        if float(value) != 0.0
    }


def validate_chunks(chunks: list[dict], expected_dim: int) -> int:
    if not chunks:
        raise ValueError("没有可导入的 chunk")
    missing = [
        index
        for index, chunk in enumerate(chunks, start=1)
        if "dense_vector" not in chunk or "sparse_vector" not in chunk
    ]
    if missing:
        raise ValueError(f"以下 chunk 缺少向量字段: {missing[:10]}")
    dim = len(chunks[0]["dense_vector"])
    if expected_dim and dim != expected_dim:
        logger.warning("dense 维度为 %d，与配置 EMBEDDING_DIM=%d 不一致", dim, expected_dim)
    return dim


def get_milvus_client(settings: Settings):
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:
        raise SystemExit("启动失败：未安装 pymilvus。请先执行 pip install pymilvus") from exc
    logger.info("连接 Milvus: %s", settings.milvus_url)
    kwargs: dict[str, Any] = {"uri": settings.milvus_url, "timeout": 60}
    if settings.milvus_token:
        kwargs["token"] = settings.milvus_token
    if os.getenv("MILVUS_USE_PROXY", "").strip().lower() not in {"1", "true", "yes"}:
        kwargs["grpc_options"] = {"grpc.enable_http_proxy": 0}
    return MilvusClient(**kwargs)


def collection_fields(client: Any, collection_name: str) -> set[str]:
    try:
        description = client.describe_collection(collection_name=collection_name)
    except Exception:
        return set()
    fields = description.get("fields") if isinstance(description, dict) else None
    if not fields and hasattr(description, "fields"):
        fields = description.fields
    names: set[str] = set()
    for field in fields or []:
        if isinstance(field, dict):
            name = field.get("name") or field.get("field_name")
        else:
            name = getattr(field, "name", None)
        if name:
            names.add(str(name))
    return names


def create_chunks_collection(client: Any, collection_name: str, vector_dimension: int) -> None:
    from pymilvus import DataType

    logger.info("创建 Milvus 集合: %s", collection_name)
    schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
    schema.add_field(field_name="chunk_id", datatype=DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field(field_name="file_name", datatype=DataType.VARCHAR, max_length=VARCHAR_MAX)
    schema.add_field(field_name="file_title", datatype=DataType.VARCHAR, max_length=VARCHAR_MAX)
    schema.add_field(field_name="part", datatype=DataType.INT64)
    schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=VARCHAR_MAX)
    schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=VARCHAR_MAX)
    schema.add_field(field_name="part_title", datatype=DataType.VARCHAR, max_length=VARCHAR_MAX)
    schema.add_field(field_name="item_name", datatype=DataType.VARCHAR, max_length=VARCHAR_MAX)
    schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=vector_dimension)
    schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="dense_vector",
        index_name="dense_vector_index",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 16, "efConstruction": 200},
    )
    index_params.add_index(
        field_name="sparse_vector",
        index_name="sparse_vector_index",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="IP",
        params={"inverted_index_algo": "DAAT_MAXSCORE", "normalize": True, "quantization": "none"},
    )
    client.create_collection(
        collection_name=collection_name,
        schema=schema,
        index_params=index_params,
    )
    logger.info("Milvus 集合创建完成: %s，dense 维度=%d", collection_name, vector_dimension)


def ensure_collection(client: Any, settings: Settings, vector_dimension: int) -> None:
    name = settings.chunks_collection
    if client.has_collection(collection_name=name):
        logger.info("Milvus 集合已存在: %s", name)
        return
    create_chunks_collection(client, name, vector_dimension)


def flush_collection(client: Any, collection_name: str) -> None:
    if not hasattr(client, "flush"):
        return
    try:
        client.flush(collection_name=collection_name)
    except Exception as exc:
        logger.warning("flush 失败，继续执行: %s", type(exc).__name__)


def unique_identity_pairs(chunks: list[dict]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for chunk in chunks:
        file_title = resolve_file_title(chunk)
        item_name = clip_varchar(chunk.get("item_name") or "").strip()
        key = (file_title, item_name)
        if not file_title and not item_name:
            continue
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


def clean_old_data(client: Any, settings: Settings, chunks: list[dict], fields: set[str]) -> None:
    collection = settings.chunks_collection
    client.load_collection(collection_name=collection)
    pairs = unique_identity_pairs(chunks)
    if not pairs:
        logger.warning("没有可用的 file_title/item_name，跳过幂等清理")
        return

    has_file_title = not fields or "file_title" in fields
    has_file_name = "file_name" in fields
    has_item_name = not fields or "item_name" in fields
    for file_title, item_name in pairs:
        clauses: list[str] = []
        if has_item_name and item_name:
            clauses.append(f'item_name == "{escape_milvus_string(item_name)}"')
        if has_file_title and file_title:
            clauses.append(f'file_title == "{escape_milvus_string(file_title)}"')
        elif has_file_name and file_title:
            clauses.append(f'file_name == "{escape_milvus_string(file_title)}"')
        if not clauses:
            continue
        expr = " and ".join(clauses)
        logger.info("幂等清理: %s", expr)
        client.delete(collection_name=collection, filter=expr)
    flush_collection(client, collection)


def prepare_row(chunk: dict, fields: set[str]) -> dict[str, Any]:
    file_name = clip_varchar(chunk.get("file_name") or "")
    file_title = resolve_file_title(chunk)
    row = {
        "file_name": file_name,
        "file_title": file_title,
        "part": to_int_part(chunk.get("part")),
        "title": clip_varchar(chunk.get("title") or ""),
        "content": clip_varchar(chunk.get("content") or ""),
        "part_title": clip_varchar(chunk.get("part_title") or ""),
        "item_name": clip_varchar(chunk.get("item_name") or ""),
        "dense_vector": [float(value) for value in chunk["dense_vector"]],
        "sparse_vector": sparse_for_milvus(chunk.get("sparse_vector") or {}),
    }
    if not fields:
        return row
    return {key: value for key, value in row.items() if key in fields}


def extract_insert_ids(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, dict):
        ids = result.get("ids") or result.get("primary_keys") or []
        return list(ids)
    for attr in ("ids", "primary_keys"):
        value = getattr(result, attr, None)
        if value:
            return list(value)
    return []


def insert_chunks(client: Any, settings: Settings, chunks: list[dict], fields: set[str]) -> list[Any]:
    collection = settings.chunks_collection
    rows = [prepare_row(chunk, fields) for chunk in chunks]
    inserted_ids: list[Any] = []
    for start in range(0, len(rows), INSERT_BATCH_SIZE):
        batch = rows[start : start + INSERT_BATCH_SIZE]
        begin, end = start + 1, start + len(batch)
        logger.info("插入 chunk %d-%d/%d", begin, end, len(rows))
        result = client.insert(collection_name=collection, data=batch)
        batch_ids = extract_insert_ids(result)
        if len(batch_ids) != len(batch):
            raise RuntimeError(
                f"插入返回的 ID 数量不一致: got={len(batch_ids)} expected={len(batch)}"
            )
        inserted_ids.extend(batch_ids)
        logger.info("已插入 %d 条", len(batch_ids))
    flush_collection(client, collection)
    client.load_collection(collection_name=collection)
    try:
        stats = client.get_collection_stats(collection_name=collection)
        logger.info("集合统计: %s", stats)
    except Exception as exc:
        logger.warning("读取集合统计失败: %s", type(exc).__name__)
    return inserted_ids


def backfill_chunk_ids(chunks: list[dict], inserted_ids: list[Any]) -> list[dict]:
    updated: list[dict] = []
    for chunk, chunk_id in zip(chunks, inserted_ids):
        item = dict(chunk)
        item["file_title"] = resolve_file_title(chunk)
        try:
            item["chunk_id"] = int(chunk_id)
        except (TypeError, ValueError):
            item["chunk_id"] = chunk_id
        updated.append(item)
    return updated


def persist_chunks(state: ImportGraphState, chunks: list[dict]) -> None:
    split_path = str(state.get("split_path") or "").strip()
    if split_path:
        write_json(Path(split_path), chunks)
        logger.info("已回填 chunk_id: %s", Path(split_path).name)
    write_json(CHUNKS_JSON, chunks)
    logger.info("已写入汇总 JSON: %s", CHUNKS_JSON)


def import_chunks_to_milvus(chunks: list[dict]) -> list[dict]:
    settings = load_settings()
    vector_dimension = validate_chunks(chunks, settings.embedding_dim)
    logger.info(
        "准备导入 %d 条 chunk，dense 维度=%d，集合=%s",
        len(chunks),
        vector_dimension,
        settings.chunks_collection,
    )
    client = get_milvus_client(settings)
    ensure_collection(client, settings, vector_dimension)
    fields = collection_fields(client, settings.chunks_collection)
    clean_old_data(client, settings, chunks, fields)
    inserted_ids = insert_chunks(client, settings, chunks, fields)
    return backfill_chunk_ids(chunks, inserted_ids)


def node_import_milvus(state: ImportGraphState) -> ImportGraphState:
    """把当前 chunks 写入 Milvus，并把自动生成的 chunk_id 回填到状态。"""
    logger.info(">>> 开始执行节点：node_import_milvus，当前状态：%s", format_state(state))
    chunks = list(state.get("chunks") or state.get("embeddings_content") or [])
    if not chunks:
        raise ValueError("chunks 为空，无法导入 Milvus")

    try:
        updated = import_chunks_to_milvus(chunks)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    except Exception:
        logger.error("导入 Milvus 失败\n%s", traceback.format_exc())
        raise ValueError("导入 Milvus 失败，请检查连接、集合和向量字段。")

    persist_chunks(state, updated)
    state["chunks"] = updated
    state["embeddings_content"] = updated
    logger.info("导入完成，共 %d 条，chunk_id 示例=%s", len(updated), updated[0].get("chunk_id"))
    logger.info("更新后状态：%s", format_state(state))
    return state


def main() -> None:
    if not OUTPUT_DIR.exists():
        raise SystemExit(f"启动失败：未找到 output 目录: {OUTPUT_DIR}")

    chunk_files = list_chunk_files(OUTPUT_DIR)
    if chunk_files:
        sources = [(path, load_chunks(path)) for path in chunk_files]
    elif CHUNKS_JSON.exists():
        sources = [(CHUNKS_JSON, load_chunks(CHUNKS_JSON))]
    else:
        logger.warning("output 中没有 *_new_chunks.json 或 chunks.json")
        return

    all_chunks: list[dict] = []
    source_ranges: list[tuple[Path, int, int]] = []
    for path, chunks in sources:
        if not chunks:
            logger.warning("%s 没有可导入的 chunk", path.name)
            continue
        start = len(all_chunks)
        all_chunks.extend(chunks)
        source_ranges.append((path, start, len(all_chunks)))

    if not all_chunks:
        logger.warning("没有可导入的 chunk")
        return

    try:
        updated = import_chunks_to_milvus(all_chunks)
    except Exception:
        logger.error("导入 Milvus 失败\n%s", traceback.format_exc())
        raise SystemExit("导入 Milvus 失败，请检查连接、集合和向量字段。")

    for path, start, end in source_ranges:
        write_json(path, updated[start:end])
        logger.info("已回填 chunk_id: %s", path.name)

    if len(source_ranges) > 1 or source_ranges[0][0] != CHUNKS_JSON:
        write_json(CHUNKS_JSON, updated)
        logger.info("已写入汇总 JSON: %s", CHUNKS_JSON)

    logger.info("导入完成，共 %d 条，chunk_id 示例=%s", len(updated), updated[0].get("chunk_id"))


if __name__ == "__main__":
    main()
