"""金融问题先重排召回候选，再按主体保留证据。"""

from __future__ import annotations

import math
import os
from typing import Any

from search.evidence import hydrate_locator
from search.query_plan import QueryPlan


def _selection_score(item: dict[str, Any]) -> float:
    if item.get("rerank_score") is not None:
        return float(item["rerank_score"])
    return float(item.get("score") or 0)


def assemble_evidence(
    hits: list[dict[str, Any]],
    plan: QueryPlan,
    per_subject_limit: int = 8,
) -> list[dict[str, Any]]:
    names = [entity.canonical_name for entity in plan.entities]
    ranked = sorted(hits, key=_selection_score, reverse=True)
    if len(names) < 2:
        return ranked[: max(per_subject_limit, 1)]
    buckets = {name: [] for name in names}
    for hit in ranked:
        text = f"{hit.get('content') or ''} {hit.get('item_name') or ''} {hit.get('file_name') or ''}"
        for name in names:
            if name in text and len(buckets[name]) < per_subject_limit:
                buckets[name].append(hit)
                break
    kept: list[dict[str, Any]] = []
    for name in names:
        kept.extend(buckets[name])
    return kept


def _vector_cache(state: dict[str, Any], settings: Any) -> dict[str, Any]:
    """空缓存不能直接交给查询向量函数，否则会在 vectors 键上失败。"""
    signature = [
        settings.bge_model_name,
        settings.bge_device,
        settings.bge_fp16,
        settings.embedding_dim,
    ]
    cache = state.get("search_query_vector_cache")
    if not isinstance(cache, dict) or cache.get("model_signature") != signature or "vectors" not in cache:
        cache = {"model_signature": signature, "vectors": {}}
        state["search_query_vector_cache"] = cache
    return cache


def rerank_financial_candidates(question: str, chunks: list[dict[str, Any]], model: Any) -> list[dict[str, Any]]:
    """用共享的 BGE 重排模型给召回候选打分。空候选不加载模型。"""
    usable = [chunk for chunk in chunks if str(chunk.get("content") or "").strip()]
    if not usable:
        return []
    pairs = [[question, str(chunk["content"]).strip()] for chunk in usable]
    scores = model.compute_score(pairs, normalize=True)
    if isinstance(scores, (int, float)):
        scores = [scores]
    scores = list(scores)
    if len(scores) != len(usable):
        raise ValueError("BGE 重排序返回的分数数量与候选块数量不一致")
    for chunk, raw_score in zip(usable, scores):
        score = float(raw_score)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("BGE 重排序分数必须是 0 到 1 之间的有限数值")
        chunk["rerank_score"] = score
    usable.sort(key=_selection_score, reverse=True)
    return usable


def search_financial_evidence(state: dict[str, Any], plan: QueryPlan) -> dict[str, Any]:
    """混合召回后重排，再按主体截断。连接或查询失败时把异常抛给上层。"""
    from search.node_search_embedding import (
        _hybrid_search,
        get_milvus_client,
        load_search_settings,
    )

    question = str(state.get("question_rewritten") or state.get("current_question") or "").strip()
    if not question:
        raise ValueError("question_rewritten 不能为空")
    settings = load_search_settings()
    limit = max(5, int(os.getenv("FINANCIAL_SEARCH_LIMIT", "20")))
    client = get_milvus_client(settings)
    try:
        client.load_collection(collection_name=settings.chunks_collection)
        expression = 'file_name != ""'
        hits = _hybrid_search(
            client,
            settings,
            settings.chunks_collection,
            question,
            expression,
            limit,
            ["item_name", "content", "file_name"],
            _vector_cache(state, settings),
        )
        chunks = []
        for hit in hits:
            entity = hit.get("entity") if isinstance(hit.get("entity"), dict) else hit
            chunks.append(hydrate_locator({
                "chunk_id": hit.get("id", entity.get("chunk_id")),
                "item_name": entity.get("item_name", ""),
                "file_name": entity.get("file_name", ""),
                "content": entity.get("content", ""),
                "score": float(hit.get("distance", hit.get("score", 0)) or 0),
            }))
        if chunks:
            from search.node_rerank import get_reranker

            chunks = rerank_financial_candidates(question, chunks, get_reranker())
        state["financial_hits"] = assemble_evidence(chunks, plan)
        state["rerank_chunks"] = list(state["financial_hits"])
        return state
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
