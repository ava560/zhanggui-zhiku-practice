"""融合普通检索与 HyDE 检索的知识块，按 RRF 分数重排。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from state import ImportGraphState

RRF_K = 60


def _chunk_key(chunk: dict[str, Any]) -> tuple[str, ...]:
    chunk_id = chunk.get("chunk_id")
    if chunk_id is not None and str(chunk_id).strip():
        return ("id", str(chunk_id))
    return (
        "content",
        str(chunk.get("item_name") or ""),
        str(chunk.get("file_name") or ""),
        str(chunk.get("content") or ""),
    )


def _ranked_unique(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(chunks, key=lambda chunk: float(chunk["score"]), reverse=True)
    seen: set[tuple[str, ...]] = set()
    unique = []
    for chunk in ranked:
        key = _chunk_key(chunk)
        if key not in seen:
            seen.add(key)
            unique.append(chunk)
    return unique


def fuse_chunks(
    embedding_chunks: list[dict[str, Any]],
    hyde_chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """每路先按相似度排名；同一块每路最多贡献一次 1/(60+排名)。"""
    fused: dict[tuple[str, ...], dict[str, Any]] = {}
    for source, chunks in (("embedding", embedding_chunks), ("hyde", hyde_chunks)):
        for rank, chunk in enumerate(_ranked_unique(chunks), start=1):
            key = _chunk_key(chunk)
            if key not in fused:
                fused[key] = {
                    "chunk_id": chunk.get("chunk_id"),
                    "item_name": chunk.get("item_name", ""),
                    "file_name": chunk.get("file_name", ""),
                    "content": chunk.get("content", ""),
                    "rrf_score": 0.0,
                    "source_ranks": {},
                }
            fused[key]["rrf_score"] += 1 / (RRF_K + rank)
            fused[key]["source_ranks"][source] = rank
    return sorted(fused.values(), key=lambda chunk: chunk["rrf_score"], reverse=True)


def _read_chunks(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    chunks = payload.get("top5") if isinstance(payload, dict) else None
    if not isinstance(chunks, list) or any(not isinstance(chunk, dict) for chunk in chunks):
        raise ValueError(f"检索 JSON 的 top5 必须是对象列表：{path}")
    for chunk in chunks:
        try:
            if not math.isfinite(float(chunk["score"])):
                raise ValueError("score 不是有限数值")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"检索 JSON 的 chunk 缺少有效 score：{path}") from exc
    return chunks


def node_rrf(state: ImportGraphState) -> ImportGraphState:
    """读取两路检索 JSON，保存去重后的 RRF 排序并回写 state。"""
    hyde_value = str(state.get("search_hyde_top5_json_path") or "").strip()
    embedding_value = str(state.get("search_embedding_top5_json_path") or "").strip()
    if not embedding_value:
        legacy_value = str(state.get("search_top5_json_path") or "").strip()
        if legacy_value != hyde_value:
            embedding_value = legacy_value
    if not embedding_value or not hyde_value:
        raise ValueError("RRF 需要普通检索和 HyDE 检索各自的 JSON 路径")
    embedding_path = Path(embedding_value).expanduser()
    hyde_path = Path(hyde_value).expanduser()
    if embedding_path.resolve() == hyde_path.resolve():
        raise ValueError("普通检索和 HyDE 检索不能使用同一个 JSON 文件")

    explicit = str(state.get("rrf_output_path") or "").strip()
    output_path = Path(explicit).expanduser() if explicit else embedding_path.with_name(
        embedding_path.stem.removesuffix("_search_top5") + "_rrf.json"
    )
    if output_path.resolve() in {embedding_path.resolve(), hyde_path.resolve()}:
        raise ValueError("RRF 输出路径不能覆盖任一路检索 JSON")

    chunks = fuse_chunks(_read_chunks(embedding_path), _read_chunks(hyde_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"k": RRF_K, "chunks": chunks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    state["rrf_chunks"] = chunks
    state["rrf_json_path"] = str(output_path)
    return state
