"""读取各文件前几个 chunk，识别商品名，回填 JSON，并将商品向量写入 Milvus。"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from product_name import canonical_product_name
from state import ImportGraphState, format_state

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
CHUNKS_JSON = OUTPUT_DIR / "chunks.json"

FIRST_CHUNK_COUNT = 5
PREVIEW_MIN_CHARS = 800
PREVIEW_MAX_CHARS = 1000

SYSTEM_PROMPT = "你是商品识别专家，只输出识别的字符串即可！"
HUMAN_PROMPT = """请从以下信息中识别出商品名称与型号：
文件名：{file_title}

正文切片（用于辅助识别）：
{context}

要求：
1. 返回内容为字符串形式，最好是带品牌、型号和名称的完整商品名称。比如：苏伯尓5000W大功率电磁炉；
2. 返回结果应该只包含商品名称，不要添加任何解释或其他内容；
3. “用户指南”“使用手册”等是文档类型，文件名中的版本号和语言标识不是商品名称，不要输出；
4. 如果无法识别商品名称,请返回空字符串。"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv(BASE_DIR / ".env")

_SIGNED_URL_RE = re.compile(r"(https?://[^\s'\"<>]+)\?[^\s'\"<>]*", re.IGNORECASE)
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_QUOTE_RE = re.compile(r'^[\'"`“”‘’]+|[\'"`“”‘’]+$')

_bge_model = None


def sanitize_log_text(text: str) -> str:
    text = _BEARER_RE.sub(r"\1[redacted]", text)
    return _SIGNED_URL_RE.sub(r"\1?[redacted]", text)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_base_url: str
    llm_model: str
    llm_temperature: float
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    bge_model_name: str
    bge_device: str
    bge_fp16: bool
    embedding_dim: int
    milvus_url: str
    milvus_token: str
    item_name_collection: str
    item_name_diag: bool


def load_settings() -> Settings:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("启动失败：未找到 OPENAI_API_KEY。请在项目根目录的 .env 中设置后再运行。")

    milvus_url = os.getenv("MILVUS_URL", "").strip()
    collection = os.getenv("ITEM_NAME_COLLECTION", "").strip()
    missing = [
        name
        for name, value in (("MILVUS_URL", milvus_url), ("ITEM_NAME_COLLECTION", collection))
        if not value
    ]
    if missing:
        raise SystemExit(f"启动失败：缺少 Milvus 配置 {', '.join(missing)}。")

    try:
        temperature = float(os.getenv("LLM_DEFAULT_TEMPERATURE", "0.1"))
    except ValueError:
        temperature = 0.1

    try:
        embedding_dim = int(os.getenv("EMBEDDING_DIM", "1024"))
    except ValueError:
        embedding_dim = 1024

    bge_path = os.getenv("BGE_M3_PATH", "").strip()
    bge_name = os.getenv("BGE_M3", "BAAI/bge-m3").strip() or "BAAI/bge-m3"
    if bge_path and Path(bge_path).exists():
        bge_model_name = bge_path
    else:
        bge_model_name = bge_name
        if bge_path:
            logger.warning("本地 BGE-M3 路径不存在，改用 %s", bge_name)

    return Settings(
        openai_api_key=api_key,
        openai_base_url=os.getenv(
            "OPENAI_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).rstrip("/"),
        llm_model=os.getenv("LLM_DEFAULT_MODEL", "qwen-flash").strip() or "qwen-flash",
        llm_temperature=temperature,
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        deepseek_model=os.getenv("LLM_FALLBACK_MODEL", "deepseek-chat").strip() or "deepseek-chat",
        bge_model_name=bge_model_name,
        bge_device=os.getenv("BGE_DEVICE", "cpu").strip() or "cpu",
        bge_fp16=env_bool("BGE_FP16", False),
        embedding_dim=embedding_dim,
        milvus_url=milvus_url,
        milvus_token=os.getenv("MILVUS_TOKEN", "").strip(),
        item_name_collection=collection,
        item_name_diag=env_bool("ITEM_NAME_DIAG", False),
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


def group_chunks(chunks: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for chunk in chunks:
        file_name = str(chunk.get("file_name") or "").strip()
        grouped[file_name or "_unknown"].append(chunk)
    for items in grouped.values():
        items.sort(key=lambda item: int(item.get("part") or 0))
    return grouped


def clip_chunk_preview(text: str) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= PREVIEW_MAX_CHARS:
        return cleaned
    return cleaned[:PREVIEW_MAX_CHARS]


def build_context(chunks: list[dict], settings: Settings) -> str:
    parts: list[str] = []
    total = 0
    for index, chunk in enumerate(chunks[:FIRST_CHUNK_COUNT], start=1):
        title = str(chunk.get("title") or "").strip()
        part_title = str(chunk.get("part_title") or "").strip()
        content = clip_chunk_preview(str(chunk.get("content") or ""))
        if not (title or part_title or content):
            continue
        piece = (
            f"【切片{index}】\n"
            f"标题：{title}\n"
            f"章节：{part_title}\n"
            f"内容：{content}"
        )
        parts.append(piece)
        total += len(piece)
        if settings.item_name_diag:
            logger.info(
                "切片%d预览 %d 字（目标 %d-%d）",
                index,
                len(content),
                PREVIEW_MIN_CHARS,
                PREVIEW_MAX_CHARS,
            )
    return "\n\n".join(parts).strip()


def clean_item_name(text: str) -> str:
    name = (text or "").strip()
    name = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", name)
    name = name.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    name = _QUOTE_RE.sub("", name).strip()
    name = re.sub(r"^(商品名称|产品名称|item_name)\s*[：:]\s*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip(" 。.;；,，")
    if len(name) > 200:
        name = name[:200].rstrip()
    return canonical_product_name(name)


def fallback_item_name(chunks: list[dict], file_name: str) -> str:
    for chunk in chunks:
        title = str(chunk.get("title") or "").strip()
        if title:
            return canonical_product_name(title)
    stem = file_name
    if stem.endswith("_new.md"):
        stem = stem[: -len("_new.md")]
    elif stem.endswith(".md"):
        stem = stem[: -len(".md")]
    return canonical_product_name(stem) or "未知商品"


def llm_endpoints(settings: Settings) -> list[tuple[str, str, str]]:
    endpoints: list[tuple[str, str, str]] = [
        (settings.openai_base_url, settings.openai_api_key, settings.llm_model),
    ]
    if settings.deepseek_api_key:
        deepseek = (
            settings.deepseek_base_url,
            settings.deepseek_api_key,
            settings.deepseek_model,
        )
        if deepseek not in endpoints:
            endpoints.append(deepseek)
    return endpoints


def call_chat_completion(settings: Settings, prompt: str) -> str:
    payload_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    last_error: Exception | None = None
    for base_url, api_key, model in llm_endpoints(settings):
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "temperature": settings.llm_temperature,
                    "max_tokens": 128,
                    "messages": payload_messages,
                },
                timeout=120,
            )
            if response.status_code >= 400:
                logger.warning(
                    "大模型调用失败 model=%s status=%s body=%s",
                    model,
                    response.status_code,
                    sanitize_log_text(response.text[:300]),
                )
                last_error = RuntimeError(f"{model} HTTP {response.status_code}")
                continue
            data = response.json()
            content = str(data["choices"][0]["message"]["content"] or "")
            logger.info("大模型调用成功: model=%s", model)
            return content
        except Exception as exc:
            last_error = exc
            logger.warning("大模型请求异常 model=%s: %s", model, type(exc).__name__)
    if last_error:
        raise last_error
    raise RuntimeError("没有可用的大模型接口")


def recognize_item_name(settings: Settings, file_name: str, chunks: list[dict]) -> str:
    context = build_context(chunks, settings)
    fallback = fallback_item_name(chunks, file_name)
    if not context:
        logger.warning("%s 无可用正文，使用标题兜底", file_name)
        return fallback

    prompt = HUMAN_PROMPT.format(file_title=file_name, context=context)
    if settings.item_name_diag:
        logger.info("商品识别上下文长度=%d，文件=%s", len(context), file_name)

    try:
        raw = call_chat_completion(settings, prompt)
        if settings.item_name_diag:
            logger.info("大模型原始返回: %s", sanitize_log_text(raw))
        item_name = clean_item_name(raw)
        if not item_name:
            logger.warning("%s 大模型未识别出商品名，使用标题兜底", file_name)
            return fallback
        logger.info("%s 识别商品名: %s", file_name, item_name)
        return item_name
    except Exception:
        logger.error(
            "大模型识别失败，使用标题兜底: %s\n%s",
            file_name,
            sanitize_log_text(traceback.format_exc()),
        )
        return fallback


def backfill_item_name(chunks: list[dict], item_name: str) -> list[dict]:
    updated: list[dict] = []
    for chunk in chunks:
        item = dict(chunk)
        item["item_name"] = item_name
        updated.append(item)
    return updated


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
        "初始化 BGE-M3：model=%s device=%s fp16=%s",
        settings.bge_model_name,
        settings.bge_device,
        settings.bge_fp16,
    )
    _bge_model = BGEM3EmbeddingFunction(
        model_name=settings.bge_model_name,
        device=settings.bge_device,
        use_fp16=settings.bge_fp16 and not settings.bge_device.startswith("cpu"),
        normalize_embeddings=True,
    )
    logger.info("BGE-M3 初始化完成")
    return _bge_model


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


def generate_item_vectors(settings: Settings, item_name: str) -> tuple[list[float], dict[int, float]]:
    model = get_bge_model(settings)
    embeddings = model.encode_documents([item_name])
    dense = embeddings["dense"][0]
    if hasattr(dense, "tolist"):
        dense_vector = [float(value) for value in dense.tolist()]
    else:
        dense_vector = [float(value) for value in dense]
    sparse_vector = normalize_sparse_vector(sparse_to_dict(embeddings["sparse"], 0))
    if len(dense_vector) != settings.embedding_dim:
        logger.warning(
            "稠密向量维度为 %d，与配置 EMBEDDING_DIM=%d 不一致",
            len(dense_vector),
            settings.embedding_dim,
        )
    logger.info(
        "已生成向量: dense_dim=%d sparse_nnz=%d",
        len(dense_vector),
        len(sparse_vector),
    )
    return dense_vector, sparse_vector


def escape_milvus_string(value: str) -> str:
    text = str(value or "")
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


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


def ensure_item_name_collection(client: Any, settings: Settings) -> None:
    name = settings.item_name_collection
    if client.has_collection(collection_name=name):
        logger.info("Milvus 集合已存在: %s", name)
        return

    from pymilvus import DataType

    logger.info("创建 Milvus 集合: %s", name)
    schema = client.create_schema(auto_id=True, enable_dynamic_field=True)
    schema.add_field(field_name="pk", datatype=DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field(field_name="file_name", datatype=DataType.VARCHAR, max_length=65535)
    schema.add_field(field_name="item_name", datatype=DataType.VARCHAR, max_length=65535)
    schema.add_field(
        field_name="dense_vector",
        datatype=DataType.FLOAT_VECTOR,
        dim=settings.embedding_dim,
    )
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
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    logger.info("Milvus 集合创建完成: %s", name)


def save_item_to_milvus(
    client: Any,
    settings: Settings,
    file_name: str,
    item_name: str,
    dense_vector: list[float],
    sparse_vector: dict[int, float],
) -> None:
    collection = settings.item_name_collection
    ensure_item_name_collection(client, settings)
    fields = collection_fields(client, collection)
    client.load_collection(collection_name=collection)

    filters: list[str] = []
    if item_name:
        filters.append(f'item_name=="{escape_milvus_string(item_name)}"')
    if file_name and "file_name" in fields:
        filters.append(f'file_name=="{escape_milvus_string(file_name)}"')
    if file_name and "file_title" in fields:
        filters.append(f'file_title=="{escape_milvus_string(file_name)}"')
    if filters:
        expr = " or ".join(filters)
        client.delete(collection_name=collection, filter=expr)
        logger.info("已清理集合中的旧记录: %s", item_name)

    data: dict[str, Any] = {
        "item_name": item_name,
        "file_name": file_name,
        "dense_vector": dense_vector,
        "sparse_vector": sparse_vector,
    }
    if not fields or "file_title" in fields:
        data["file_title"] = file_name
    client.insert(collection_name=collection, data=[data])
    client.load_collection(collection_name=collection)
    logger.info(
        "已写入 Milvus: collection=%s file_name=%s item_name=%s",
        collection,
        file_name,
        item_name,
    )


def get_milvus_client(settings: Settings):
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:
        raise SystemExit("启动失败：未安装 pymilvus。请先执行 pip install pymilvus") from exc
    logger.info("连接 Milvus: %s", settings.milvus_url)
    kwargs: dict[str, Any] = {"uri": settings.milvus_url, "timeout": 30}
    if settings.milvus_token:
        kwargs["token"] = settings.milvus_token
    if os.getenv("MILVUS_USE_PROXY", "").strip().lower() not in {"1", "true", "yes"}:
        kwargs["grpc_options"] = {"grpc.enable_http_proxy": 0}
    return MilvusClient(**kwargs)


def process_file_chunks(
    settings: Settings,
    milvus_client: Any | None,
    file_name: str,
    chunks: list[dict],
) -> list[dict]:
    logger.info("识别商品: %s ，共 %d 个 chunk", file_name, len(chunks))
    item_name = recognize_item_name(settings, file_name, chunks)
    updated = backfill_item_name(chunks, item_name)
    if milvus_client is None:
        logger.warning("Milvus 不可用，已跳过向量入库: %s -> %s", file_name, item_name)
        return updated
    dense_vector, sparse_vector = generate_item_vectors(settings, item_name)
    save_item_to_milvus(
        milvus_client,
        settings,
        file_name,
        item_name,
        dense_vector,
        sparse_vector,
    )
    return updated


def persist_chunks(state: ImportGraphState, chunks: list[dict]) -> None:
    split_path = str(state.get("split_path") or "").strip()
    if split_path:
        write_json(Path(split_path), chunks)
        logger.info("已回填: %s", Path(split_path).name)
    write_json(CHUNKS_JSON, chunks)
    logger.info("已写入汇总 JSON: %s ，共 %d 个 chunk", CHUNKS_JSON, len(chunks))


def node_item_name_recognition(state: ImportGraphState) -> ImportGraphState:
    """识别当前文档商品名，回填 chunks，并尽量写入商品名集合。"""
    logger.info(">>> 开始执行节点：node_item_name_recognition，当前状态：%s", format_state(state))
    chunks = list(state.get("chunks") or [])
    if not chunks:
        raise ValueError("chunks 为空，无法识别商品名")

    try:
        settings = load_settings()
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc

    file_name = str(chunks[0].get("file_name") or state.get("file_title") or "")
    milvus_client = None
    try:
        milvus_client = get_milvus_client(settings)
    except Exception as exc:
        logger.error("无法连接 Milvus，将先完成商品识别和 JSON 回填: %s", type(exc).__name__)

    updated = process_file_chunks(settings, milvus_client, file_name, chunks)
    item_name = str((updated[0].get("item_name") if updated else "") or "")
    if item_name:
        try:
            from graph_store import link_name_to_file

            link_name_to_file(item_name, file_name or str(state.get("file_title") or ""))
        except Exception as exc:
            logger.warning("Neo4j 未写入名称关系，入库继续：%s", type(exc).__name__)
    persist_chunks(state, updated)
    state["chunks"] = updated
    state["item_name"] = item_name
    logger.info("商品识别完成: %s", item_name)
    logger.info("更新后状态：%s", format_state(state))
    return state


def main() -> None:
    settings = load_settings()
    if not OUTPUT_DIR.exists():
        raise SystemExit(f"启动失败：未找到 output 目录: {OUTPUT_DIR}")

    milvus_client = None
    milvus_connect_error: str | None = None
    try:
        milvus_client = get_milvus_client(settings)
    except Exception as exc:
        milvus_connect_error = str(exc)
        logger.error("无法连接 Milvus，将先完成商品识别和 JSON 回填: %s", type(exc).__name__)

    all_chunks: list[dict] = []
    failed: list[str] = []

    chunk_files = list_chunk_files(OUTPUT_DIR)
    if chunk_files:
        sources = [(path.name, load_chunks(path), path) for path in chunk_files]
    elif CHUNKS_JSON.exists():
        grouped = group_chunks(load_chunks(CHUNKS_JSON))
        sources = [(file_name, chunks, None) for file_name, chunks in grouped.items()]
    else:
        logger.warning("output 中没有 *_new_chunks.json 或 chunks.json")
        return

    for label, chunks, path in sources:
        file_name = str(chunks[0].get("file_name") or label) if chunks else label
        try:
            updated = process_file_chunks(settings, milvus_client, file_name, chunks)
            if path is not None:
                write_json(path, updated)
                logger.info("已回填: %s", path.name)
            all_chunks.extend(updated)
        except Exception:
            logger.error("处理失败: %s\n%s", file_name, sanitize_log_text(traceback.format_exc()))
            failed.append(file_name)

    if all_chunks:
        write_json(CHUNKS_JSON, all_chunks)
        logger.info("已写入汇总 JSON: %s ，共 %d 个 chunk", CHUNKS_JSON, len(all_chunks))

    if failed:
        raise SystemExit(f"有 {len(failed)} 个文件处理失败: {failed}")
    if milvus_connect_error:
        raise SystemExit(
            "商品名已回填到 JSON，但 Milvus 不可达，未写入向量。"
            "请检查 .env 中的 MILVUS_URL 后重试。"
        )
    logger.info("商品识别完成，共 %d 个文件", len(sources))


if __name__ == "__main__":
    main()
