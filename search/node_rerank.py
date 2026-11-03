"""使用 BGE-reranker-large 对 RRF 知识块和网页摘要统一重排。"""

from __future__ import annotations

import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from state import ImportGraphState

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TOP_K = 10
SCORE_GAP = 0.3


@lru_cache(maxsize=2)
def _load_reranker(model_name: str, device: str, use_fp16: bool):
    try:
        from FlagEmbedding import FlagReranker
    except ImportError as exc:
        raise RuntimeError("未安装 FlagEmbedding，无法运行 BGE 重排序模型") from exc
    return FlagReranker(model_name, devices=device, use_fp16=use_fp16)


def get_reranker():
    """按环境变量中的模型目录加载一次模型。"""
    model_name = (
        os.getenv("BGE_RERANKER_PATH", "").strip()
        or os.getenv("BGE_RERANKER_LARGE", "").strip()
        or "BAAI/bge-reranker-large"
    )
    device = os.getenv("BGE_RERANKER_DEVICE", "").strip() or os.getenv("BGE_DEVICE", "cpu").strip() or "cpu"
    fp16_setting = os.getenv("BGE_RERANKER_FP16", "").strip() or os.getenv("BGE_FP16", "false").strip()
    use_fp16 = fp16_setting.lower() in {"1", "true", "yes", "on"} and not device.startswith("cpu")
    return _load_reranker(model_name, device, use_fp16)


def _candidates(
    rrf_chunks: list[dict[str, Any]], web_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = []
    for chunk in rrf_chunks:
        content = chunk.get("content")
        if isinstance(content, str) and content.strip():
            candidates.append({**chunk, "source": "rrf", "content": content.strip()})
    for result in web_results:
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            candidates.append({
                "source": "web",
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "content": content.strip(),
            })
    return candidates


def _comparison_web_results(
    results: list[dict[str, Any]], item_names: list[str],
) -> list[dict[str, Any]]:
    """有两个明确型号时，排除完全没提及这些型号的网页摘要。"""
    if len(item_names) < 2:
        return results
    model_ids = []
    for name in item_names:
        tokens = [
            re.sub(r"[^a-z0-9]", "", token.lower())
            for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", name)
        ]
        tokens = [token for token in tokens if len(token) >= 4 and any(c.isdigit() for c in token)]
        if not tokens:
            return results
        model_ids.append(max(tokens, key=len))
    return [
        result for result in results
        if any(
            model_id in re.sub(
                r"[^a-z0-9]", "",
                " ".join(str(result.get(key) or "") for key in ("title", "content", "url")).lower(),
            )
            for model_id in model_ids
        )
    ]


def rerank_candidates(
    question: str,
    rrf_chunks: list[dict[str, Any]],
    web_results: list[dict[str, Any]],
    model: Any,
    required_item_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """批量评分、降序排序，并在相邻归一化分数差超过 0.3 时截断。"""
    candidates = _candidates(rrf_chunks, web_results)
    if not candidates:
        return []

    pairs = [[question, candidate["content"]] for candidate in candidates]
    scores = model.compute_score(pairs, normalize=True)
    if isinstance(scores, (int, float)):
        scores = [scores]
    scores = list(scores)
    if len(scores) != len(candidates):
        raise ValueError("BGE 重排序返回的分数数量与候选块数量不一致")

    for candidate, raw_score in zip(candidates, scores):
        score = float(raw_score)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("BGE 重排序分数必须是 0 到 1 之间的有限数值")
        candidate["rerank_score"] = score
    candidates.sort(key=lambda candidate: candidate["rerank_score"], reverse=True)

    selected = []
    for candidate in candidates:
        if len(selected) == TOP_K:
            break
        if selected:
            gap = selected[-1]["rerank_score"] - candidate["rerank_score"]
            if gap > SCORE_GAP and not math.isclose(gap, SCORE_GAP, rel_tol=0, abs_tol=1e-9):
                break
        selected.append(candidate)
    if required_item_names and len(required_item_names) > 1:
        evidence = [
            next(
                (candidate for candidate in candidates
                 if candidate["source"] == "rrf" and candidate.get("item_name") == name),
                None,
            )
            for name in dict.fromkeys(required_item_names)
        ]
        selected_with_evidence = [candidate for candidate in evidence if candidate is not None][:TOP_K]
        seen = {id(candidate) for candidate in selected_with_evidence}
        for candidate in selected:
            if len(selected_with_evidence) == TOP_K:
                break
            if id(candidate) not in seen:
                selected_with_evidence.append(candidate)
                seen.add(id(candidate))
        selected = sorted(selected_with_evidence, key=lambda candidate: candidate["rerank_score"], reverse=True)
    return selected


def _read_results(path: Path, field: str) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(results, list) or any(not isinstance(result, dict) for result in results):
        raise ValueError(f"输入 JSON 的 {field} 必须是对象列表：{path}")
    return results


def node_rerank(state: ImportGraphState) -> ImportGraphState:
    """读取两路节点的 JSON，重排后写入新 JSON 和 state。"""
    question = str(state.get("question_rewritten") or "").strip()
    if not question:
        raise ValueError("question_rewritten 不能为空")
    rrf_value = str(state.get("rrf_json_path") or "").strip()
    web_value = str(state.get("web_search_json_path") or "").strip()
    if not rrf_value or (not web_value and not state.get("web_search_error")):
        raise ValueError("重排序需要 RRF 和网页搜索各自的 JSON 路径")
    rrf_path = Path(rrf_value).expanduser()
    web_path = Path(web_value).expanduser() if web_value else None
    if web_path is not None and rrf_path.resolve() == web_path.resolve():
        raise ValueError("RRF 和网页搜索不能使用同一个 JSON 文件")

    explicit = str(state.get("rerank_output_path") or "").strip()
    output_path = Path(explicit).expanduser() if explicit else rrf_path.with_name(
        rrf_path.stem.removesuffix("_rrf") + "_rerank.json"
    )
    input_paths = {rrf_path.resolve()}
    if web_path is not None:
        input_paths.add(web_path.resolve())
    if output_path.resolve() in input_paths:
        raise ValueError("重排序输出路径不能覆盖任一路输入 JSON")

    rrf_chunks = _read_results(rrf_path, "chunks")
    web_results = _read_results(web_path, "top5") if web_path is not None else []
    item_names = state.get("search_matched_item_names") or []
    web_results = _comparison_web_results(web_results, item_names)
    model = get_reranker() if _candidates(rrf_chunks, web_results) else None
    chunks = rerank_candidates(question, rrf_chunks, web_results, model, item_names)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"question_rewritten": question, "chunks": chunks,
                    "web_search_error": state.get("web_search_error") or ""}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    state["rerank_chunks"] = chunks
    state["rerank_json_path"] = str(output_path)
    return state
