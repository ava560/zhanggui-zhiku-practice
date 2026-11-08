"""代码报错后只生成待确认的修改，不替换正在使用的版本，也不再运行。"""

from __future__ import annotations

import os


def suggest_code_fix(source: str, error: str) -> str:
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
        return ""
    prompt = (
        "下面的 Python 在定时任务里失败了。只输出改正后的完整脚本，不要解释。"
        "写回表格时只能调用 context.replace_rows，字段必须和原表一致。"
        "不要删除旧版本，不要再执行脚本。\n"
        f"报错：{error[:500]}\n脚本：\n{source[:4000]}"
    )
    try:
        from openai import OpenAI
    except Exception:
        return ""
    for base_url, api_key, model in configured:
        try:
            client = OpenAI(base_url=base_url, api_key=api_key)
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            text = str(response.choices[0].message.content or "").strip()
        except Exception:
            continue
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
        text = text.strip()
        if text:
            return text
    return ""
