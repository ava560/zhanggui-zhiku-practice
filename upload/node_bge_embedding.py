"""将 output 中的 chunk 用 BGE-M3 批量向量化，回写 dense/sparse 字段。"""

from __future__ import annotations

import json
import logging
import math
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
BATCH_SIZE = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv(BASE_DIR / ".env")

_bge_model = None


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bge_model_name: str
    bge_device: str
    bge_fp16: bool
    embedding_dim: int
    batch_size: int


def load_settings() -> Settings:
    bge_path = os.getenv("BGE_M3_PATH", "").strip()
    bge_name = os.getenv("BGE_M3", "BAAI/bge-m3").strip() or "BAAI/bge-m3"
    if bge_path and Path(bge_path).exists():
        bge_model_name = bge_path
    else:
        bge_model_name = bge_name
        if bge_path:
            logger.warning("本地 BGE-M3 路径不存在，改用 %s", bge_name)

    try:
        embedding_dim = int(os.getenv("EMBEDDING_DIM", "1024"))
    except ValueError:
        embedding_dim = 1024

    try:
        batch_size = max(1, int(os.getenv("BGE_BATCH_SIZE", str(BATCH_SIZE))))
    except ValueError:
        batch_size = BATCH_SIZE

    return Settings(
        bge_model_name=bge_model_name,
        bge_device=os.getenv("BGE_DEVICE", "cpu").strip() or "cpu",
        bge_fp16=env_bool("BGE_FP16", False),
        embedding_dim=embedding_dim,
        batch_size=batch_size,
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


def build_embedding_text(chunk: dict) -> str:
    item_name = str(chunk.get("item_name") or "").strip()
    content = str(chunk.get("content") or "").strip()
    if item_name:
        return f"商品：{item_name}，介绍：{content}"
    return content


def get_bge_model(settings: Settings):
    global _bge_model
    if _bge_model is not None:
        return _bge_model
    try:
        from milvus_model.hybrid import BGEM3EmbeddingFunction
    except ImportError:
        try:
            from pymilvus.model.hybrid import BGEM3EmbeddingFunction
        except ImportError as exc:
            raise SystemExit(
                "启动失败：未安装 milvus-model。请先执行 pip install pymilvus milvus-model"
            ) from exc

    logger.info(
        "初始化 BGE-M3：model=%s device=%s fp16=%s batch_size=%d",
        settings.bge_model_name,
        settings.bge_device,
        settings.bge_fp16,
        settings.batch_size,
    )
    _bge_model = BGEM3EmbeddingFunction(
        model_name=settings.bge_model_name,
        device=settings.bge_device,
        use_fp16=settings.bge_fp16 and not settings.bge_device.startswith("cpu"),
        normalize_embeddings=True,
    )
    logger.info("BGE-M3 初始化完成")
    return _bge_model


def to_dense_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return [float(value) for value in vector]


def sparse_to_dict(sparse: Any, index: int = 0) -> dict[int, float]:
    if sparse is None:
        return {}
    if isinstance(sparse, dict):
        return {int(key): float(value) for key, value in sparse.items() if float(value) != 0.0}
    if isinstance(sparse, list):
        if not sparse:
            return {}
        return sparse_to_dict(sparse[index], 0)
    if hasattr(sparse, "getrow"):
        row = sparse.getrow(index)
        return {
            int(idx): float(value)
            for idx, value in zip(row.indices.tolist(), row.data.tolist())
            if float(value) != 0.0
        }
    if hasattr(sparse, "indptr") and hasattr(sparse, "indices") and hasattr(sparse, "data"):
        start = int(sparse.indptr[index])
        end = int(sparse.indptr[index + 1])
        return {
            int(idx): float(value)
            for idx, value in zip(sparse.indices[start:end], sparse.data[start:end])
            if float(value) != 0.0
        }
    raise TypeError(f"无法解析稀疏向量类型: {type(sparse)!r}")


def normalize_sparse_vector(sparse_vec: dict[int, float]) -> dict[int, float]:
    if not sparse_vec:
        return {}
    values = list(sparse_vec.values())
    norm = math.sqrt(sum(value * value for value in values))
    if norm < 1e-9:
        return {int(key): float(value) for key, value in sparse_vec.items()}
    return {int(key): float(value) / norm for key, value in sparse_vec.items()}


def encode_batch(model: Any, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
    embeddings = model.encode_documents(texts)
    dense_vectors = [to_dense_list(vector) for vector in embeddings["dense"]]
    sparse_vectors = [
        normalize_sparse_vector(sparse_to_dict(embeddings["sparse"], index))
        for index in range(len(texts))
    ]
    return dense_vectors, sparse_vectors


def attach_vectors(chunk: dict, dense_vector: list[float], sparse_vector: dict[int, float]) -> dict:
    item = dict(chunk)
    item["dense_vector"] = dense_vector
    item["sparse_vector"] = {str(key): value for key, value in sparse_vector.items()}
    return item


def embed_chunks(settings: Settings, chunks: list[dict], source_name: str) -> tuple[list[dict], int]:
    model = get_bge_model(settings)
    output: list[dict] = []
    failed = 0
    total = len(chunks)
    for start in range(0, total, settings.batch_size):
        batch = chunks[start : start + settings.batch_size]
        begin, end = start + 1, start + len(batch)
        texts = [build_embedding_text(chunk) for chunk in batch]
        empty_indexes = [index for index, text in enumerate(texts) if not text.strip()]
        try:
            dense_vectors, sparse_vectors = encode_batch(model, texts if texts else [""])
            if len(dense_vectors) != len(batch) or len(sparse_vectors) != len(batch):
                raise ValueError(
                    f"向量条数不匹配: dense={len(dense_vectors)} sparse={len(sparse_vectors)} batch={len(batch)}"
                )
            for index, chunk in enumerate(batch):
                if index in empty_indexes:
                    logger.warning("%s 第 %d 条内容为空，跳过向量化", source_name, start + index + 1)
                    output.append(dict(chunk))
                    failed += 1
                    continue
                dense = dense_vectors[index]
                if settings.embedding_dim and len(dense) != settings.embedding_dim:
                    logger.warning(
                        "%s 第 %d 条 dense 维度为 %d，期望 %d",
                        source_name,
                        start + index + 1,
                        len(dense),
                        settings.embedding_dim,
                    )
                output.append(attach_vectors(chunk, dense, sparse_vectors[index]))
            logger.info(
                "%s 第 %d-%d/%d 条向量化完成",
                source_name,
                begin,
                end,
                total,
            )
        except Exception:
            logger.error(
                "%s 第 %d-%d 条向量化失败，保留原字段\n%s",
                source_name,
                begin,
                end,
                traceback.format_exc(),
            )
            output.extend(dict(chunk) for chunk in batch)
            failed += len(batch)
    return output, failed


def persist_chunks(state: ImportGraphState, chunks: list[dict]) -> None:
    split_path = str(state.get("split_path") or "").strip()
    if split_path:
        write_json(Path(split_path), chunks)
        logger.info("已回写: %s", Path(split_path).name)
    write_json(CHUNKS_JSON, chunks)
    logger.info("已写入汇总 JSON: %s ，共 %d 个 chunk", CHUNKS_JSON, len(chunks))


def node_bge_embedding(state: ImportGraphState) -> ImportGraphState:
    """为当前 chunks 生成稠密/稀疏向量，并写回状态。"""
    logger.info(">>> 开始执行节点：node_bge_embedding，当前状态：%s", format_state(state))
    chunks = list(state.get("chunks") or [])
    if not chunks:
        raise ValueError("chunks 为空，无法向量化")

    try:
        settings = load_settings()
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc

    source_name = str(state.get("file_title") or "chunks")
    embedded, failed = embed_chunks(settings, chunks, source_name)
    persist_chunks(state, embedded)
    state["chunks"] = embedded
    state["embeddings_content"] = embedded
    embedded_count = sum(
        1 for chunk in embedded if "dense_vector" in chunk and "sparse_vector" in chunk
    )
    logger.info("向量化完成：成功 %d 条，失败/跳过 %d 条", embedded_count, failed)
    if failed:
        raise ValueError(f"有 {failed} 条 chunk 未能生成向量")
    logger.info("更新后状态：%s", format_state(state))
    return state


def main() -> None:
    settings = load_settings()
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
    failed_total = 0
    for path, chunks in sources:
        if not chunks:
            logger.warning("%s 没有可处理的 chunk", path.name)
            continue
        logger.info("开始向量化: %s ，共 %d 条", path.name, len(chunks))
        embedded, failed = embed_chunks(settings, chunks, path.name)
        failed_total += failed
        write_json(path, embedded)
        logger.info("已回写: %s", path.name)
        all_chunks.extend(embedded)

    if all_chunks and (len(sources) > 1 or sources[0][0] != CHUNKS_JSON):
        write_json(CHUNKS_JSON, all_chunks)
        logger.info("已写入汇总 JSON: %s ，共 %d 个 chunk", CHUNKS_JSON, len(all_chunks))

    embedded_count = sum(1 for chunk in all_chunks if "dense_vector" in chunk and "sparse_vector" in chunk)
    logger.info("向量化完成：成功 %d 条，失败/跳过 %d 条", embedded_count, failed_total)
    if failed_total:
        raise SystemExit(f"有 {failed_total} 条 chunk 未能生成向量")


if __name__ == "__main__":
    main()
