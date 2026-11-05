"""意图识别必须调用 LLM，不能用关键词表代替。"""

from __future__ import annotations

import json
import os
import re

import requests


class IntentClassificationError(RuntimeError):
    """两次都无法解析模型输出。"""


_LABELS = {
    "chat": "chat",
    "simple": "simple",
    "complex": "complex",
    "闲聊": "chat",
    "简单查询": "simple",
    "复杂任务": "complex",
}

_PROMPT = """你是查询分流器。只输出一个 JSON 对象，不要输出其他文字。
格式：{{"intent":"chat"}}、{{"intent":"simple"}} 或 {{"intent":"complex"}}。
执行标准：
- 闲聊：寒暄、感谢，或回答不需要资料的交谈。intent 用 chat。
- 简单查询：一个问题用一次资料查询就能回答。intent 用 simple。
- 复杂任务：需要拆成多个子问题、比较多个主体，或后一步依赖前一步的结果。intent 用 complex。
不要按问题长短或名称数量机械分类。
最近对话：{history}
会话记忆：{session_memory}
长期记忆：{long_term}
用户问题：{question}
"""


def build_intent_prompt(question: str, history=None, session_memory=None, long_term=None) -> str:
    return _PROMPT.format(
        history=json.dumps(list(history or [])[-10:], ensure_ascii=False),
        session_memory=json.dumps(session_memory or {}, ensure_ascii=False),
        long_term=json.dumps(list(long_term or []), ensure_ascii=False),
        question=question,
    )


def parse_intent(raw: str) -> str | None:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    begin, end = text.find("{"), text.rfind("}")
    if begin < 0 or end <= begin:
        return None
    try:
        data = json.loads(text[begin : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    label = data.get("intent")
    if not isinstance(label, str):
        return None
    return _LABELS.get(label.strip())


def classify_intent(question: str, llm, history=None, session_memory=None, long_term=None) -> str:
    """调用两次以内。两次都无法解析就失败，不猜测路径。"""
    prompt = build_intent_prompt(question, history, session_memory, long_term)
    for _ in range(2):
        parsed = parse_intent(llm(prompt))
        if parsed:
            return parsed
    raise IntentClassificationError("意图分类失败，请换一种说法后再试。")


def default_llm(prompt: str) -> str:
    """复用项目已配置的 OpenAI 兼容接口。"""
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
    configured = [item for item in endpoints if item[1]]
    if not configured:
        raise IntentClassificationError("意图分类失败，请换一种说法后再试。")
    last_error: Exception | None = None
    for base_url, api_key, model in configured:
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 80,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"] or "")
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            last_error = exc
    raise IntentClassificationError("意图分类失败，请换一种说法后再试。") from last_error
