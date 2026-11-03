"""按已确认商品名过滤知识库，并用 BGE-M3 检索最相关的五个内容块。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from search.node_item_name_confirm import (
    DENSE_WEIGHT,
    SPARSE_WEIGHT,
    generate_query_vectors,
    get_milvus_client,
)
from state import ImportGraphState, OUTPUT_DIR

logger = logging.getLogger(__name__)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class SearchSettings:
    milvus_url: str
    milvus_token: str
    item_name_collection: str
    chunks_collection: str
    bge_model_name: str
    bge_device: str
    bge_fp16: bool
    embedding_dim: int
    search_top_k: int = 5


def load_search_settings() -> SearchSettings:
    """只读取本节点需要的配置，不依赖 MongoDB 或 LLM 凭据。"""
    required = {
        "MILVUS_URL": os.getenv("MILVUS_URL", "").strip(),
        "ITEM_NAME_COLLECTION": os.getenv("ITEM_NAME_COLLECTION", "").strip(),
        "CHUNKS_COLLECTION": os.getenv("CHUNKS_COLLECTION", "").strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError(f"缺少配置：{', '.join(missing)}")
    model_name = (
        os.getenv("BGE_M3_PATH", "").strip()
        or os.getenv("BGE_M3", "BAAI/bge-m3").strip()
    )
    if not model_name:
        raise ValueError("缺少 BGE_M3_PATH 或 BGE_M3 配置")
    return SearchSettings(
        milvus_url=required["MILVUS_URL"],
        milvus_token=os.getenv("MILVUS_TOKEN", "").strip(),
        item_name_collection=required["ITEM_NAME_COLLECTION"],
        chunks_collection=required["CHUNKS_COLLECTION"],
        bge_model_name=model_name,
        bge_device=os.getenv("BGE_DEVICE", "cpu").strip() or "cpu",
        bge_fp16=os.getenv("BGE_FP16", "false").strip().lower() in {"1", "true", "yes", "on"},
        embedding_dim=int(os.getenv("EMBEDDING_DIM", "1024")),
    )


def _query_vectors(
    text: str, settings: SearchSettings, cache: dict[str, Any] | None,
) -> tuple[list[float], dict[int, float]]:
    if cache is None:
        return generate_query_vectors(settings, text)
    vectors = cache["vectors"]
    if text not in vectors:
        dense, sparse = generate_query_vectors(settings, text)
        vectors[text] = {"dense": dense, "sparse": sparse}
    saved = vectors[text]
    return saved["dense"], {int(key): float(value) for key, value in saved["sparse"].items()}


def _search_requests(
    text: str, settings: SearchSettings, expression: str, limit: int,
    cache: dict[str, Any] | None = None,
):
    try:
        from pymilvus import AnnSearchRequest
    except ImportError as exc:
        raise RuntimeError("未安装 pymilvus，无法执行双索引检索") from exc

    dense, sparse = _query_vectors(text, settings, cache)
    return [
        AnnSearchRequest(
            data=[dense],
            anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=limit,
            expr=expression,
        ),
        AnnSearchRequest(
            data=[sparse],
            anns_field="sparse_vector",
            param={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
            limit=limit,
            expr=expression,
        ),
    ]


def _hybrid_search(
    client: Any,
    settings: SearchSettings,
    collection: str,
    text: str,
    expression: str,
    limit: int,
    output_fields: list[str],
    cache: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    try:
        from pymilvus import WeightedRanker
    except ImportError as exc:
        raise RuntimeError("当前 pymilvus 不支持 WeightedRanker") from exc

    result = client.hybrid_search(
        collection_name=collection,
        reqs=_search_requests(text, settings, expression, limit, cache),
        ranker=WeightedRanker(DENSE_WEIGHT, SPARSE_WEIGHT, norm_score=False),
        limit=limit,
        output_fields=output_fields,
    )
    return result[0] if result else []


def _entity(hit: dict[str, Any]) -> dict[str, Any]:
    entity = hit.get("entity")
    return entity if isinstance(entity, dict) else hit


def _input_names(state: ImportGraphState) -> list[str]:
    if state.get("awaiting_user_confirmation"):
        raise ValueError("商品名尚待用户确认，不能检索知识库")
    names = state.get("itemname_question")
    if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
        raise ValueError("itemname_question 必须是已确认的非空商品名列表")
    cleaned = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not cleaned:
        raise ValueError("itemname_question 不能为空，不能无过滤地检索 kb_chunks")
    return cleaned


def _output_path(state: ImportGraphState, question: str) -> Path:
    explicit = str(state.get("search_top5_output_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    identity = str(
        state.get("task_id") or state.get("conversation_id") or state.get("session_id")
        or "conversation"
    )
    safe_identity = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", identity).strip("._")[:80]
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
    directory = Path(str(state.get("local_dir") or OUTPUT_DIR)).expanduser()
    return directory / f"{safe_identity or 'conversation'}_{digest}_search_top5.json"


def retrieve_top5(
    client: Any,
    settings: SearchSettings,
    names: list[str],
    query_text: str,
    state: ImportGraphState | None = None,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """先校验商品名，再用给定查询文本检索所有匹配商品的全局 Top 5。"""
    cache: dict[str, Any] | None = None
    if state is not None:
        model_signature = [
            getattr(settings, "bge_model_name", None),
            getattr(settings, "bge_device", None),
            getattr(settings, "bge_fp16", None),
            getattr(settings, "embedding_dim", None),
        ]
        cache = state.get("search_query_vector_cache")
        if not isinstance(cache, dict) or cache.get("model_signature") != model_signature:
            cache = {"model_signature": model_signature, "vectors": {}}
            state["search_query_vector_cache"] = cache
    matched: list[str] = []
    missing: list[str] = []
    top5: list[dict[str, Any]] = []
    client.load_collection(collection_name=settings.item_name_collection)
    for name in names:
        # 上一节点已完成模糊确认；此处精确校验名称是否仍在商品集合中。
        expression = "item_name == " + json.dumps(name, ensure_ascii=False)
        rows = client.query(
            collection_name=settings.item_name_collection,
            filter=expression,
            output_fields=["item_name"],
            limit=1,
        ) or []
        verified_name = str(rows[0].get("item_name") or "").strip() if rows else ""
        if verified_name == name:
            matched.append(verified_name)
        else:
            missing.append(name)

    matched = list(dict.fromkeys(matched))
    if matched:
        client.load_collection(collection_name=settings.chunks_collection)
        expression = "item_name in " + json.dumps(matched, ensure_ascii=False)
        hits = _hybrid_search(
            client, settings, settings.chunks_collection, query_text,
            expression, settings.search_top_k, ["item_name", "content", "file_name"], cache,
        )
        for hit in hits:
            entity = _entity(hit)
            if entity.get("item_name") not in matched:
                continue
            top5.append({
                "chunk_id": hit.get("id", entity.get("chunk_id")),
                "item_name": entity.get("item_name"),
                "file_name": entity.get("file_name", ""),
                "content": entity.get("content", ""),
                "score": float(hit.get("distance", hit.get("score", 0))),
            })
            if len(top5) == settings.search_top_k:
                break
    return matched, missing, top5


def node_search_embedding(state: ImportGraphState) -> ImportGraphState:
    """精确校验商品名，用知识块双索引检索，保存 JSON 并回写 state。"""
    names = _input_names(state)
    question = str(state.get("question_rewritten") or "").strip()
    if not question:
        raise ValueError("question_rewritten 不能为空")

    settings = load_search_settings()
    client = get_milvus_client(settings)
    try:
        matched, missing, top5 = retrieve_top5(client, settings, names, question, state)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    payload = {
        "itemname_question": names,
        "question_rewritten": question,
        "matched_item_names": matched,
        "missing_item_names": missing,
        "top5": top5,
    }
    output_path = _output_path(state, question)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    state["search_matched_item_names"] = matched
    state["search_missing_item_names"] = missing
    state["search_top5"] = top5
    state["search_top5_json_path"] = str(output_path)
    state["search_embedding_top5_json_path"] = str(output_path)
    logger.info("知识块检索完成：matched=%d top5=%d json=%s", len(matched), len(top5), output_path)
    return state
