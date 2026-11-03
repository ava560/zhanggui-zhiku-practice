"""HyDE：生成假设性回答扩展问题，再按已确认商品检索知识块。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import requests

from search.node_item_name_confirm import get_milvus_client
from search.node_search_embedding import (
    _input_names,
    _output_path,
    load_search_settings,
    retrieve_top5,
)
from state import ImportGraphState

logger = logging.getLogger(__name__)

HYDE_SYSTEM_PROMPT = """你是产品知识库检索的 HyDE 文本生成器。
请根据用户的问题和已确认的产品名，写一段约 300 字的中文假设性参考回答，供向量检索使用，不是给用户的最终答复。
保留产品名称、型号及问题中的关键术语，描述可能相关的现象、排查方向或操作要点。
没有资料依据时不要编造具体数值、按键名称、接口、操作顺序或产品文档结论。
只输出自然语言正文，不要 Markdown、标题、免责声明或对用户的反问。"""


def generate_hyde_answer(question: str, names: list[str]) -> str:
    """复用项目主/备用 OpenAI 兼容接口，只生成用于检索的参考文本。"""
    endpoints = [
        (
            os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            os.getenv("OPENAI_API_KEY", "").strip(),
            os.getenv("LLM_DEFAULT_MODEL", "qwen-flash").strip() or "qwen-flash",
        ),
        (
            os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            os.getenv("DEEPSEEK_API_KEY", "").strip(),
            os.getenv("LLM_FALLBACK_MODEL", "deepseek-chat").strip() or "deepseek-chat",
        ),
    ]
    configured = list(dict.fromkeys(endpoint for endpoint in endpoints if endpoint[1]))
    if not configured:
        raise ValueError("缺少 OPENAI_API_KEY 或 DEEPSEEK_API_KEY，无法生成 HyDE 回答")

    messages = [
        {"role": "system", "content": HYDE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "已确认产品名：" + "、".join(names) + "\n改写后的问题：" + question,
        },
    ]
    last_error: Exception | None = None
    for base_url, api_key, model in configured:
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "temperature": float(os.getenv("LLM_DEFAULT_TEMPERATURE", "0.1")),
                    "max_tokens": 700,
                    "messages": messages,
                },
                timeout=120,
            )
            response.raise_for_status()
            answer = str(response.json()["choices"][0]["message"]["content"] or "").strip()
            if answer:
                logger.info("HyDE 参考回答生成完成：model=%s chars=%d", model, len(answer))
                return answer
            last_error = ValueError("模型返回了空的 HyDE 回答")
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            last_error = exc
            logger.warning("HyDE 模型调用失败：model=%s error=%s", model, type(exc).__name__)
    raise RuntimeError("所有已配置的模型接口均未生成 HyDE 回答") from last_error


def _hyde_output_path(state: ImportGraphState, question: str) -> Path:
    explicit = str(state.get("search_hyde_top5_output_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    # 只借用普通检索节点的安全文件名生成逻辑，不复用上次检索的输出路径。
    baseline = _output_path({**state, "search_top5_output_path": ""}, question)
    return baseline.with_name(baseline.name.replace("_search_top5.json", "_search_hyde_top5.json"))


def node_search_embedding_hyde(state: ImportGraphState) -> ImportGraphState:
    """生成 question_answer，并用它在商品过滤后的知识块中做双索引 Top 5 检索。"""
    names = _input_names(state)
    question = str(state.get("question_rewritten") or "").strip()
    if not question:
        raise ValueError("question_rewritten 不能为空")

    hyde_answer = generate_hyde_answer(question, names)
    question_answer = f"{question}\n{hyde_answer}"
    settings = load_search_settings()
    client = get_milvus_client(settings)
    try:
        matched, missing, top5 = retrieve_top5(client, settings, names, question_answer, state)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    payload = {
        "itemname_question": names,
        "question_rewritten": question,
        "hyde_answer": hyde_answer,
        "question_answer": question_answer,
        "matched_item_names": matched,
        "missing_item_names": missing,
        "top5": top5,
    }
    output_path = _hyde_output_path(state, question)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    state["hyde_answer"] = hyde_answer
    state["question_answer"] = question_answer
    state["search_matched_item_names"] = matched
    state["search_missing_item_names"] = missing
    state["search_top5"] = top5
    state["search_top5_json_path"] = str(output_path)
    state["search_hyde_top5_json_path"] = str(output_path)
    logger.info("HyDE 检索完成：matched=%d top5=%d json=%s", len(matched), len(top5), output_path)
    return state
