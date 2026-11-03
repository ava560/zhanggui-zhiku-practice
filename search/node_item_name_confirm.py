"""结合 MongoDB 对话上下文提取问题商品名，并用 Milvus 双索引确认商品。

节点不会在内部使用 ``input()`` 阻塞工作流。需要用户确认或选择时，它会把候选项、
提示语和 ``awaiting_user_confirmation=True`` 写入 state；上层应用收到用户回复后，
将 ``item_name_user_confirmed`` 或 ``item_name_user_selection`` 写回 state，再次调用本
节点即可完成确认。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from product_name import canonical_product_name
from state import ImportGraphState, OUTPUT_DIR, format_state

BASE_DIR = Path(__file__).resolve().parent.parent

DENSE_WEIGHT = 0.8
SPARSE_WEIGHT = 0.2
DIRECT_MATCH_THRESHOLD = 0.9
MIN_MATCH_THRESHOLD = 0.6

SYSTEM_PROMPT = """你是面向产品知识库的对话理解助手。
你的任务是结合历史对话理解当前问题中的指代，提取用户正在询问的产品名称，并改写问题。
先判断当前问题是否在咨询产品、产品文档、功能、使用或故障；普通闲聊、模型身份、算术等不是产品咨询。
只返回一个 JSON 对象，不要返回 Markdown、解释或其他文字。"""

USER_PROMPT = """历史对话：
{history}

当前问题：
{question}

请严格返回以下 JSON 结构：
{{
  "is_product_question": true,
  "itemname_question": ["产品名称或型号1", "产品名称或型号2"],
  "question_rewritten": "补全指代、语义独立且保留原意的问题"
}}

要求：
1. is_product_question 必须是布尔值，主要依据当前问题意图判断，不能因为历史里提过产品就把闲聊当作产品咨询；
2. 非产品咨询时返回 false 和空的 itemname_question，不检索产品库；
3. itemname_question 必须是字符串数组，可以有一个、多个或为空数组；
4. 产品名称尽量保留品牌、型号和品类，不要把故障现象、用户指南等文档类型混入产品名；
5. 当前问题中的“它”“这个设备”等指代，应根据历史对话补全；
6. question_rewritten 必须能脱离历史对话独立理解；
7. 不得虚构历史对话和当前问题中没有依据的产品。"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv(BASE_DIR / ".env")

_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_SIGNED_URL_RE = re.compile(r"(https?://[^\s'\"<>]+)\?[^\s'\"<>]*", re.IGNORECASE)
_FILENAME_RE = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")
_bge_model = None


@dataclass(frozen=True)
class Settings:
    mongo_uri: str
    mongo_database: str
    mongo_collection: str
    mongo_user_id_field: str
    mongo_conversation_id_field: str
    mongo_messages_field: str
    mongo_role_field: str
    mongo_content_field: str
    mongo_question_field: str
    mongo_answer_field: str
    mongo_created_at_field: str
    mongo_history_limit: int
    mongo_timeout_ms: int
    max_history_chars: int
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
    search_top_k: int


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def load_settings() -> Settings:
    """读取显式配置，不扫描目录，也不猜测远端数据库地址。"""
    mongo_uri = _first_env("MONGODB_URI", "MONGO_URL")
    mongo_database = _first_env("MONGODB_DATABASE", "MONGO_DB_NAME")
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    milvus_url = os.getenv("MILVUS_URL", "").strip()
    item_name_collection = os.getenv("ITEM_NAME_COLLECTION", "").strip()
    missing = [
        name
        for name, value in (
            ("MONGO_URL/MONGODB_URI", mongo_uri),
            ("MONGO_DB_NAME/MONGODB_DATABASE", mongo_database),
            ("OPENAI_API_KEY", openai_api_key),
            ("MILVUS_URL", milvus_url),
            ("ITEM_NAME_COLLECTION", item_name_collection),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"缺少配置：{', '.join(missing)}")

    bge_path = os.getenv("BGE_M3_PATH", "").strip()
    bge_name = os.getenv("BGE_M3", "BAAI/bge-m3").strip() or "BAAI/bge-m3"
    if bge_path and Path(bge_path).exists():
        bge_model_name = bge_path
    else:
        bge_model_name = bge_name
        if bge_path:
            logger.warning("配置的 BGE_M3_PATH 不存在，改用显式模型名 %s", bge_name)

    return Settings(
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        mongo_collection=_first_env(
            "MONGODB_CONVERSATION_COLLECTION",
            "MONGO_CONVERSATION_COLLECTION",
            default="conversation_history",
        ),
        mongo_user_id_field=os.getenv("MONGO_USER_ID_FIELD", "user_id").strip() or "user_id",
        mongo_conversation_id_field=(
            os.getenv("MONGO_CONVERSATION_ID_FIELD", "conversation_id").strip()
            or "conversation_id"
        ),
        mongo_messages_field=os.getenv("MONGO_MESSAGES_FIELD", "messages").strip() or "messages",
        mongo_role_field=os.getenv("MONGO_ROLE_FIELD", "role").strip() or "role",
        mongo_content_field=os.getenv("MONGO_CONTENT_FIELD", "content").strip() or "content",
        mongo_question_field=(
            os.getenv("MONGO_QUESTION_FIELD", "current_question").strip()
            or "current_question"
        ),
        mongo_answer_field=os.getenv("MONGO_ANSWER_FIELD", "answer").strip() or "answer",
        mongo_created_at_field=(
            os.getenv("MONGO_CREATED_AT_FIELD", "created_at").strip() or "created_at"
        ),
        mongo_history_limit=max(1, _env_int("MONGO_HISTORY_LIMIT", 50)),
        mongo_timeout_ms=max(1000, _env_int("MONGO_TIMEOUT_MS", 5000)),
        max_history_chars=max(1000, _env_int("LLM_HISTORY_MAX_CHARS", 12000)),
        openai_api_key=openai_api_key,
        openai_base_url=os.getenv(
            "OPENAI_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).rstrip("/"),
        llm_model=os.getenv("LLM_DEFAULT_MODEL", "qwen-flash").strip() or "qwen-flash",
        llm_temperature=_env_float("LLM_DEFAULT_TEMPERATURE", 0.1),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        deepseek_model=os.getenv("LLM_FALLBACK_MODEL", "deepseek-chat").strip() or "deepseek-chat",
        bge_model_name=bge_model_name,
        bge_device=os.getenv("BGE_DEVICE", "cpu").strip() or "cpu",
        bge_fp16=_env_bool("BGE_FP16", False),
        embedding_dim=_env_int("EMBEDDING_DIM", 1024),
        milvus_url=milvus_url,
        milvus_token=os.getenv("MILVUS_TOKEN", "").strip(),
        item_name_collection=item_name_collection,
        search_top_k=max(1, _env_int("ITEM_NAME_SEARCH_TOP_K", 10)),
    )


def sanitize_log_text(text: str) -> str:
    text = _BEARER_RE.sub(r"\1[redacted]", str(text or ""))
    return _SIGNED_URL_RE.sub(r"\1?[redacted]", text)


def _normalise_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role in {"user", "human", "customer", "客户", "用户"}:
        return "user"
    if role in {"assistant", "ai", "bot", "客服", "助手"}:
        return "assistant"
    return role or "user"


def _append_message(messages: list[dict[str, str]], role: Any, content: Any) -> None:
    text = str(content or "").strip()
    if not text:
        return
    message = {"role": _normalise_role(role), "content": text}
    if messages and messages[-1] == message:
        return
    messages.append(message)


def _messages_from_document(document: dict[str, Any], settings: Settings) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    nested = document.get(settings.mongo_messages_field)
    if not isinstance(nested, list):
        for fallback_field in ("messages", "history", "conversation"):
            candidate = document.get(fallback_field)
            if isinstance(candidate, list):
                nested = candidate
                break

    if isinstance(nested, list):
        for item in nested:
            if not isinstance(item, dict):
                continue
            content = item.get(settings.mongo_content_field)
            if content is None:
                content = item.get("content") or item.get("message") or item.get("text")
            if content is not None:
                _append_message(
                    messages,
                    item.get(settings.mongo_role_field) or item.get("role") or item.get("sender"),
                    content,
                )
                continue
            question = item.get(settings.mongo_question_field) or item.get("question")
            answer = item.get(settings.mongo_answer_field) or item.get("answer")
            _append_message(messages, "user", question)
            _append_message(messages, "assistant", answer)
    else:
        content = document.get(settings.mongo_content_field)
        if content is None:
            content = document.get("content") or document.get("message") or document.get("text")
        if content is not None:
            _append_message(
                messages,
                document.get(settings.mongo_role_field)
                or document.get("role")
                or document.get("sender"),
                content,
            )
        else:
            question = document.get(settings.mongo_question_field)
            if question is None:
                question = document.get("current_question") or document.get("question")
            answer = document.get(settings.mongo_answer_field) or document.get("answer")
            _append_message(messages, "user", question)
            _append_message(messages, "assistant", answer)

    explicit_current = document.get(settings.mongo_question_field)
    if explicit_current is None and settings.mongo_question_field != "current_question":
        explicit_current = document.get("current_question")
    if explicit_current:
        _append_message(messages, "user", explicit_current)
    return messages


def _coerce_mongo_filter(query: dict[str, Any]) -> dict[str, Any]:
    """仅对显式 ``_id`` 做 ObjectId 转换，其余字段保持调用方原值。"""
    if "_id" not in query or not isinstance(query["_id"], str):
        return query
    try:
        from bson import ObjectId

        if ObjectId.is_valid(query["_id"]):
            query = dict(query)
            query["_id"] = ObjectId(query["_id"])
    except ImportError:
        pass
    return query


def _build_mongo_filter(state: ImportGraphState, settings: Settings) -> dict[str, Any]:
    explicit_filter = state.get("mongodb_filter")
    if isinstance(explicit_filter, dict) and explicit_filter:
        query = _coerce_mongo_filter(dict(explicit_filter))
        if not query.get(settings.mongo_user_id_field) or not query.get(
            settings.mongo_conversation_id_field
        ):
            raise ValueError("mongodb_filter 必须同时包含 user_id 和 conversation_id")
        return query

    user_id = str(state.get("user_id") or "").strip()
    conversation_id = str(
        state.get("conversation_id") or state.get("session_id") or ""
    ).strip()
    if not user_id or not conversation_id:
        raise ValueError(
            "无法定位 MongoDB 对话：state 必须同时提供 user_id 和 conversation_id"
        )
    return {
        settings.mongo_user_id_field: user_id,
        settings.mongo_conversation_id_field: conversation_id,
    }


def load_conversation_from_mongodb(
    settings: Settings,
    state: ImportGraphState,
) -> tuple[list[dict[str, str]], str]:
    """按 chat_service 的 seq 顺序读取消息，最后一条用户消息是当前问题。"""
    try:
        from pymongo import DESCENDING, MongoClient
    except ImportError as exc:
        raise RuntimeError("未安装 pymongo，请先安装 requirements.txt 中的依赖。") from exc

    query = _build_mongo_filter(state, settings)
    collection_name = str(state.get("mongo_collection") or settings.mongo_collection).strip()
    if not collection_name:
        raise ValueError("MongoDB collection 不能为空")

    client = MongoClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=settings.mongo_timeout_ms,
        connectTimeoutMS=settings.mongo_timeout_ms,
    )
    try:
        collection = client[settings.mongo_database][collection_name]
        cursor = (
            collection.find(query)
            .sort("seq", DESCENDING)
            .limit(settings.mongo_history_limit)
        )
        documents = list(cursor)
    finally:
        client.close()

    if not documents:
        raise ValueError(
            f"MongoDB 未找到对话记录：database={settings.mongo_database}, "
            f"collection={collection_name}"
        )

    messages: list[dict[str, str]] = []
    for document in reversed(documents):
        messages.extend(_messages_from_document(document, settings))

    current_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index]["role"] == "user"),
        -1,
    )
    if current_index < 0:
        raise ValueError("MongoDB 对话记录中没有可识别的用户问题")

    current_question = messages[current_index]["content"].strip()
    history = messages[:current_index]
    logger.info(
        "MongoDB 对话读取完成：collection=%s history=%d current_question_chars=%d",
        collection_name,
        len(history),
        len(current_question),
    )
    return history, current_question


def _history_for_prompt(history: list[dict[str, str]], max_chars: int) -> str:
    if not history:
        return "（无历史对话）"
    selected: list[dict[str, str]] = []
    current_chars = 0
    for message in reversed(history):
        size = len(message.get("content", "")) + len(message.get("role", "")) + 16
        if selected and current_chars + size > max_chars:
            break
        selected.append(message)
        current_chars += size
    selected.reverse()
    return json.dumps(selected, ensure_ascii=False, indent=2)


def _llm_endpoints(settings: Settings) -> list[tuple[str, str, str]]:
    endpoints = [(settings.openai_base_url, settings.openai_api_key, settings.llm_model)]
    fallback = (
        settings.deepseek_base_url,
        settings.deepseek_api_key,
        settings.deepseek_model,
    )
    if settings.deepseek_api_key and fallback not in endpoints:
        endpoints.append(fallback)
    return endpoints


def call_llm_for_json(settings: Settings, history: list[dict[str, str]], question: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_PROMPT.format(
                history=_history_for_prompt(history, settings.max_history_chars),
                question=question,
            ),
        },
    ]
    last_error: Exception | None = None
    for base_url, api_key, model in _llm_endpoints(settings):
        payload: dict[str, Any] = {
            "model": model,
            "temperature": settings.llm_temperature,
            "max_tokens": 512,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
            if response.status_code >= 400:
                logger.warning(
                    "LLM 调用失败 model=%s status=%s body=%s",
                    model,
                    response.status_code,
                    sanitize_log_text(response.text[:300]),
                )
                last_error = RuntimeError(f"{model} HTTP {response.status_code}")
                continue
            content = str(response.json()["choices"][0]["message"]["content"] or "")
            logger.info("LLM 商品提取与问题改写成功：model=%s", model)
            return content
        except Exception as exc:
            last_error = exc
            logger.warning("LLM 请求异常 model=%s: %s", model, type(exc).__name__)
    raise RuntimeError("所有 LLM 接口均调用失败") from last_error


def _clean_product_name(value: Any) -> str:
    text = str(value or "").strip().strip("'\"`“”‘’")
    text = re.sub(r"\s+", " ", text).strip(" ,，;；。")
    return text[:200].rstrip()


def parse_llm_json(raw: str, current_question: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    begin, end = text.find("{"), text.rfind("}")
    if begin < 0 or end < begin:
        raise ValueError("LLM 返回内容不是 JSON 对象")
    try:
        data = json.loads(text[begin : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 返回 JSON 解析失败：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("LLM 返回 JSON 的顶层必须是对象")

    raw_names = data.get("itemname_question", [])
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    if not isinstance(raw_names, list):
        raise ValueError("itemname_question 必须是字符串数组")

    names: list[str] = []
    seen: set[str] = set()
    for value in raw_names:
        if not isinstance(value, str):
            raise ValueError("itemname_question 中的每一项都必须是字符串")
        name = _clean_product_name(value)
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    rewritten_value = data.get("question_rewritten")
    if rewritten_value is not None and not isinstance(rewritten_value, str):
        raise ValueError("question_rewritten 必须是字符串")
    rewritten = str(rewritten_value or "").strip()
    if not rewritten:
        rewritten = current_question.strip()
    is_product = data.get("is_product_question", bool(names))
    if not isinstance(is_product, bool):
        raise ValueError("is_product_question 必须是布尔值")
    if not is_product:
        names = []
    return {
        "is_product_question": is_product,
        "itemname_question": names,
        "question_rewritten": rewritten,
    }


def _safe_filename(value: str) -> str:
    cleaned = _FILENAME_RE.sub("_", str(value or "").strip()).strip("._")
    return (cleaned or "conversation")[:100]


def persist_llm_json(state: ImportGraphState, payload: dict[str, Any]) -> str:
    configured_path = str(state.get("item_name_question_json_path") or "").strip()
    if configured_path:
        path = Path(configured_path).expanduser()
    else:
        output_dir = Path(str(state.get("local_dir") or OUTPUT_DIR)).expanduser()
        identity = str(
            state.get("task_id")
            or state.get("conversation_id")
            or state.get("session_id")
            or "conversation"
        )
        path = output_dir / f"{_safe_filename(identity)}_item_name_question.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("LLM JSON 已写入: %s", path)
    return str(path)


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
            raise RuntimeError("未安装 milvus-model，无法生成稠密/稀疏查询向量。") from exc

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
    return _bge_model


def _sparse_to_dict(sparse: Any, index: int = 0) -> dict[int, float]:
    if sparse is None:
        return {}
    if isinstance(sparse, dict):
        return {int(key): float(value) for key, value in sparse.items() if float(value) != 0.0}
    if isinstance(sparse, list):
        return _sparse_to_dict(sparse[index], 0) if sparse else {}
    if hasattr(sparse, "getrow"):
        row = sparse.getrow(index)
        return {
            int(key): float(value)
            for key, value in zip(row.indices.tolist(), row.data.tolist())
            if float(value) != 0.0
        }
    if hasattr(sparse, "indptr") and hasattr(sparse, "indices") and hasattr(sparse, "data"):
        start = int(sparse.indptr[index])
        end = int(sparse.indptr[index + 1])
        return {
            int(key): float(value)
            for key, value in zip(sparse.indices[start:end], sparse.data[start:end])
            if float(value) != 0.0
        }
    raise TypeError(f"无法解析稀疏向量类型：{type(sparse)!r}")


def _normalise_sparse_vector(vector: dict[int, float]) -> dict[int, float]:
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm < 1e-9:
        return {int(key): float(value) for key, value in vector.items()}
    return {int(key): float(value) / norm for key, value in vector.items()}


def generate_query_vectors(settings: Settings, text: str) -> tuple[list[float], dict[int, float]]:
    model = get_bge_model(settings)
    encoder = getattr(model, "encode_queries", None) or model.encode_documents
    embeddings = encoder([text])
    dense = embeddings["dense"][0]
    dense_vector = [float(value) for value in (dense.tolist() if hasattr(dense, "tolist") else dense)]
    sparse_vector = _normalise_sparse_vector(_sparse_to_dict(embeddings["sparse"], 0))
    if settings.embedding_dim and len(dense_vector) != settings.embedding_dim:
        logger.warning(
            "查询稠密向量维度为 %d，与 EMBEDDING_DIM=%d 不一致",
            len(dense_vector),
            settings.embedding_dim,
        )
    return dense_vector, sparse_vector


def get_milvus_client(settings: Settings):
    try:
        from pymilvus import MilvusClient
    except ImportError as exc:
        raise RuntimeError("未安装 pymilvus，无法检索商品名集合。") from exc
    kwargs: dict[str, Any] = {"uri": settings.milvus_url, "timeout": 30}
    if settings.milvus_token:
        kwargs["token"] = settings.milvus_token
    if os.getenv("MILVUS_USE_PROXY", "").strip().lower() not in {"1", "true", "yes"}:
        kwargs["grpc_options"] = {"grpc.enable_http_proxy": 0}
    return MilvusClient(**kwargs)


def _hit_value(hit: Any, key: str) -> Any:
    if isinstance(hit, dict):
        entity = hit.get("entity")
        if isinstance(entity, dict) and key in entity:
            return entity[key]
        return hit.get(key)
    entity = getattr(hit, "entity", None)
    if entity is not None:
        try:
            value = entity.get(key)
            if value is not None:
                return value
        except (AttributeError, KeyError, TypeError):
            pass
    return getattr(hit, key, None)


def _parse_hybrid_hits(result: Any) -> list[dict[str, Any]]:
    hits = result[0] if isinstance(result, list) and result and isinstance(result[0], list) else result
    if not isinstance(hits, list):
        return []
    best_by_name: dict[str, dict[str, Any]] = {}
    for hit in hits:
        name = _clean_product_name(_hit_value(hit, "item_name"))
        score_value = _hit_value(hit, "distance")
        if score_value is None:
            score_value = _hit_value(hit, "score")
        try:
            score = float(score_value)
        except (TypeError, ValueError):
            continue
        if not name:
            continue
        candidate = {
            "itemname_question": name,
            "display_name": canonical_product_name(name),
            "score": score,
            "file_name": str(_hit_value(hit, "file_name") or ""),
        }
        current = best_by_name.get(name)
        if current is None or score > float(current["score"]):
            best_by_name[name] = candidate
    return sorted(best_by_name.values(), key=lambda item: float(item["score"]), reverse=True)


def hybrid_search_item_name(
    client: Any,
    settings: Settings,
    query_name: str,
) -> list[dict[str, Any]]:
    """按 dense=0.8、sparse=0.2 执行 Milvus hybrid search。"""
    try:
        from pymilvus import AnnSearchRequest, WeightedRanker
    except ImportError as exc:
        raise RuntimeError("当前 pymilvus 不支持 hybrid search。") from exc

    dense_vector, sparse_vector = generate_query_vectors(settings, query_name)
    search_requests = [
        AnnSearchRequest(
            data=[dense_vector],
            anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=settings.search_top_k,
        ),
        AnnSearchRequest(
            data=[sparse_vector],
            anns_field="sparse_vector",
            param={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
            limit=settings.search_top_k,
        ),
    ]
    result = client.hybrid_search(
        collection_name=settings.item_name_collection,
        reqs=search_requests,
        # 两路向量在写入和查询时都已归一化；保留原始相似度，使 0.6/0.9 阈值可解释。
        ranker=WeightedRanker(DENSE_WEIGHT, SPARSE_WEIGHT, norm_score=False),
        limit=settings.search_top_k,
        output_fields=["item_name", "file_name"],
    )
    hits = _parse_hybrid_hits(result)
    logger.info("商品名双索引检索完成：query=%s hits=%d", query_name, len(hits))
    return hits


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def classify_matches(
    extracted_names: list[str],
    hits_by_query: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    direct_matches: list[str] = []
    pending_candidates: list[dict[str, Any]] = []
    not_found: list[str] = []
    match_results: list[dict[str, Any]] = []

    for query_name in extracted_names:
        hits = hits_by_query.get(query_name, [])
        top = hits[0] if hits else None
        if top and float(top["score"]) >= DIRECT_MATCH_THRESHOLD:
            direct_matches.append(str(top["itemname_question"]))
            match_results.append(
                {"query_item_name": query_name, "decision": "direct", "candidates": [top]}
            )
            continue

        middle = [
            dict(hit, display_name=canonical_product_name(str(hit["itemname_question"])), query_item_name=query_name)
            for hit in hits
            if MIN_MATCH_THRESHOLD <= float(hit["score"]) < DIRECT_MATCH_THRESHOLD
        ]
        if middle:
            pending_candidates.extend(middle)
            match_results.append(
                {
                    "query_item_name": query_name,
                    "decision": "confirm" if len(middle) == 1 else "select",
                    "candidates": middle,
                }
            )
        else:
            not_found.append(query_name)
            match_results.append(
                {"query_item_name": query_name, "decision": "not_found", "candidates": []}
            )

    direct_matches = _deduplicate(direct_matches)
    pending_by_name: dict[str, dict[str, Any]] = {}
    for candidate in pending_candidates:
        name = str(candidate.get("itemname_question") or "")
        previous = pending_by_name.get(name)
        if previous is None or float(candidate["score"]) > float(previous["score"]):
            pending_by_name[name] = candidate
    unique_pending = list(pending_by_name.values())

    if len(unique_pending) > 1:
        status = "need_selection"
        options = "\n".join(
            f"{index}. {item['display_name']}（匹配 {item['query_item_name']}，分数 {item['score']:.9g}）"
            for index, item in enumerate(unique_pending, start=1)
        )
        message = f"找到多个可能的产品，请选择一个或多个：\n{options}"
    elif len(unique_pending) == 1:
        status = "need_confirmation"
        item = unique_pending[0]
        message = (
            f"请确认您询问的产品是否为“{item['display_name']}”"
            f"（匹配分数 {item['score']:.9g}）？"
        )
    elif direct_matches:
        status = "matched"
        message = ""
    else:
        status = "not_found"
        missing_text = "、".join(not_found or extracted_names) or "当前问题中的产品"
        message = f"未找到与“{missing_text}”匹配分数达到 {MIN_MATCH_THRESHOLD} 的产品。"

    return {
        "itemname_question": direct_matches,
        "item_name_direct_matches": direct_matches,
        "item_name_candidates": unique_pending,
        "item_name_not_found": not_found,
        "item_name_match_results": match_results,
        "item_name_confirm_status": status,
        "item_name_confirmation_message": message,
        "awaiting_user_confirmation": bool(unique_pending),
    }


def _selection_values(selection: Any, candidates: list[dict[str, Any]]) -> list[str]:
    if isinstance(selection, bool):
        raise ValueError("布尔确认请使用 item_name_user_confirmed 字段")
    if isinstance(selection, (str, int)):
        values = [selection]
    elif isinstance(selection, (list, tuple, set)):
        values = list(selection)
    else:
        raise ValueError("item_name_user_selection 必须是候选名称、序号或它们的列表")

    selected: list[str] = []
    allowed_names = {
        str(candidate.get("itemname_question") or ""): candidate for candidate in candidates
    }
    for value in values:
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            index = int(value) - 1
            if index < 0 or index >= len(candidates):
                raise ValueError(f"候选序号超出范围：{value}")
            selected.append(str(candidates[index]["itemname_question"]))
            continue
        name = str(value or "").strip()
        if name not in allowed_names:
            raise ValueError(f"用户选择不在候选项中：{name}")
        selected.append(name)
    return _deduplicate(selected)


def apply_user_decision(state: ImportGraphState) -> ImportGraphState:
    candidates = list(state.get("item_name_candidates") or [])
    direct_matches = [str(value) for value in state.get("item_name_direct_matches") or []]
    confirmed = state.get("item_name_user_confirmed")
    selection = state.get("item_name_user_selection")

    if confirmed is True:
        if len(candidates) != 1:
            raise ValueError("item_name_user_confirmed=True 仅适用于只有一个候选项的确认场景")
        selected = [str(candidates[0]["itemname_question"])]
    elif confirmed is False:
        selected = []
    elif selection is not None:
        selected = _selection_values(selection, candidates)
    else:
        return state

    final_names = _deduplicate(direct_matches + selected)
    state["itemname_question"] = final_names
    state["item_name_confirm_status"] = "confirmed" if selected else (
        "matched" if direct_matches else "not_found"
    )
    state["item_name_confirmation_message"] = ""
    state["awaiting_user_confirmation"] = False
    state["item_name_user_selection"] = None
    state["item_name_user_confirmed"] = None
    logger.info("用户确认处理完成：itemname_question=%s", final_names)
    return state


def node_item_name_confirm(state: ImportGraphState) -> ImportGraphState:
    """MongoDB 上下文 -> LLM JSON -> Milvus 双索引 -> state。"""
    logger.info(">>> 开始执行节点：node_item_name_confirm，当前状态：%s", format_state(state))

    if state.get("awaiting_user_confirmation"):
        has_decision = (
            state.get("item_name_user_confirmed") is not None
            or state.get("item_name_user_selection") is not None
        )
        if has_decision:
            return apply_user_decision(state)
        logger.info("节点正在等待用户确认或选择，不重复读取数据库和调用模型")
        return state

    settings = load_settings()
    history, current_question = load_conversation_from_mongodb(settings, state)
    raw = call_llm_for_json(settings, history, current_question)
    llm_payload = parse_llm_json(raw, current_question)
    json_path = persist_llm_json(state, llm_payload)

    state["conversation_history"] = history
    state["current_question"] = current_question
    state["question_rewritten"] = str(llm_payload["question_rewritten"])
    state["item_name_question_json_path"] = json_path
    state["is_product_question"] = bool(llm_payload["is_product_question"])
    if not state["is_product_question"]:
        state["itemname_question"] = []
        state["item_name_confirm_status"] = "general_chat"
        state["awaiting_user_confirmation"] = False
        logger.info("当前问题非产品咨询，跳过 Milvus 检索")
        return state

    extracted_names = list(llm_payload["itemname_question"])
    hits_by_query: dict[str, list[dict[str, Any]]] = {}
    if extracted_names:
        client = get_milvus_client(settings)
        try:
            client.load_collection(collection_name=settings.item_name_collection)
            for query_name in extracted_names:
                hits_by_query[query_name] = hybrid_search_item_name(client, settings, query_name)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    match_state = classify_matches(extracted_names, hits_by_query)
    state.update(match_state)
    state["itemname_question_extracted"] = extracted_names
    logger.info(
        "商品确认节点完成：status=%s extracted=%s final=%s",
        state.get("item_name_confirm_status"),
        extracted_names,
        state.get("itemname_question"),
    )
    logger.info("更新后状态：%s", format_state(state))
    return state


if __name__ == "__main__":
    raise SystemExit(
        "node_item_name_confirm 是工作流节点，请由 LangGraph 传入包含 user_id/"
        "conversation_id 的 state 调用。"
    )
