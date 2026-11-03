"""使用百炼 EnhancedSearch MCP 检索改写后问题的前五条网页结果。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from state import ImportGraphState, OUTPUT_DIR

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logger = logging.getLogger(__name__)

TOP_K = 5
_URL_FIELDS = ("url", "link", "href", "web_url", "source_url")
_CONTENT_FIELDS = ("summary", "snippet", "description", "abstract", "content")
_QUERY_FIELDS = ("query", "search_query", "q", "keyword", "keywords")
_COUNT_FIELDS = ("count", "top_k", "limit", "num_results", "max_results")


def _tool_value(tool: Any, key: str, fallback: str = "") -> Any:
    if isinstance(tool, dict):
        return tool.get(key, tool.get(fallback))
    value = getattr(tool, key, None)
    return value if value is not None or not fallback else getattr(tool, fallback, None)


def _choose_tool(tools: list[Any]) -> Any:
    preferred = os.getenv("MCP_DASHSCOPE_SEARCH_TOOL", "").strip()
    if preferred:
        selected = next((tool for tool in tools if _tool_value(tool, "name") == preferred), None)
        if selected is None:
            raise ValueError(f"MCP 服务未提供配置的搜索工具 {preferred}")
        return selected

    for name in ("search_pro", "web_search", "search"):
        selected = next((tool for tool in tools if _tool_value(tool, "name") == name), None)
        if selected is not None:
            return selected
    candidates = [tool for tool in tools if "search" in str(_tool_value(tool, "name") or "").lower()]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError("无法唯一确定 EnhancedSearch 的搜索工具，请设置 MCP_DASHSCOPE_SEARCH_TOOL")


def _tool_arguments(tool: Any, question: str) -> dict[str, Any]:
    schema = _tool_value(tool, "input_schema", "inputSchema") or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    query_field = next((field for field in _QUERY_FIELDS if field in properties), None)
    if query_field is None and not properties:
        query_field = "query"
    if query_field is None:
        raise ValueError(f"MCP 搜索工具 {_tool_value(tool, 'name')} 缺少可识别的查询参数")

    arguments: dict[str, Any] = {query_field: question}
    count_field = next((field for field in _COUNT_FIELDS if field in properties), None)
    if count_field:
        arguments[count_field] = TOP_K
    missing = set(schema.get("required", [])) - set(arguments)
    if missing:
        raise ValueError(f"MCP 搜索工具还有未配置的必填参数：{', '.join(sorted(missing))}")
    return arguments


def _find_rows(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    if isinstance(value, str):
        try:
            return _find_rows(json.loads(value), depth + 1)
        except (TypeError, ValueError):
            return []
    if isinstance(value, list):
        if any(isinstance(item, dict) and any(field in item for field in _URL_FIELDS) for item in value):
            return [item for item in value if isinstance(item, dict)]
        rows: list[dict[str, Any]] = []
        for item in value:
            rows.extend(_find_rows(item, depth + 1))
        return rows
    if isinstance(value, dict):
        if any(field in value for field in _URL_FIELDS):
            return [value]
        rows = []
        for nested in value.values():
            rows.extend(_find_rows(nested, depth + 1))
        return rows
    return []


def _first_text(row: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_search_results(response: Any) -> list[dict[str, str]]:
    """读取 MCP 结构化结果或 JSON 文本；不根据自由文本猜造网页地址。"""
    if bool(_tool_value(response, "is_error", "isError")):
        raise RuntimeError("EnhancedSearch MCP 工具返回错误")

    sources = [
        _tool_value(response, "data"),
        _tool_value(response, "structured_content", "structuredContent"),
    ]
    for block in _tool_value(response, "content") or []:
        if _tool_value(block, "type") == "text":
            sources.append(_tool_value(block, "text"))

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for source in sources:
        for row in _find_rows(source):
            url = _first_text(row, _URL_FIELDS)
            if not url.lower().startswith(("https://", "http://")) or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append({
                "title": _first_text(row, ("title", "name", "page_title")),
                "url": url,
                "content": _first_text(row, _CONTENT_FIELDS),
            })
            if len(results) == TOP_K:
                return results
    if not results and any(source for source in sources):
        raise ValueError("EnhancedSearch MCP 响应未包含可解析的网页 title/url/content")
    return results


async def search_web(question: str) -> list[dict[str, str]]:
    """通过 Streamable HTTP 调用服务端实际提供的搜索工具。"""
    url = os.getenv("MCP_DASHSCOPE_BASE_URL", "").strip()
    api_key = os.getenv("MCP_DASHSCOPE_API_KEY", "").strip()
    if not url or not api_key:
        raise ValueError("请配置 MCP_DASHSCOPE_BASE_URL 和 MCP_DASHSCOPE_API_KEY")
    try:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport
    except ImportError as exc:
        raise RuntimeError("未安装 fastmcp，请安装 requirements.txt 中的依赖") from exc

    transport = StreamableHttpTransport(url=url, headers={"Authorization": f"Bearer {api_key}"})
    async with Client(transport) as client:
        tool = _choose_tool(await client.list_tools())
        tool_name = str(_tool_value(tool, "name"))
        result = await client.call_tool(tool_name, _tool_arguments(tool, question))
    return parse_search_results(result)


def _output_path(state: ImportGraphState, question: str) -> Path:
    explicit = str(state.get("web_search_output_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    identity = str(state.get("task_id") or state.get("conversation_id") or state.get("session_id") or "conversation")
    safe_identity = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", identity).strip("._")[:80]
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
    directory = Path(str(state.get("local_dir") or OUTPUT_DIR)).expanduser()
    return directory / f"{safe_identity or 'conversation'}_{digest}_web_search_mcp_top5.json"


async def anode_web_search_mcp(state: ImportGraphState) -> ImportGraphState:
    """异步 LangGraph 节点；同步工作流可调用 node_web_search_mcp。"""
    question = str(state.get("question_rewritten") or "").strip()
    if not question:
        raise ValueError("question_rewritten 不能为空")
    results = await search_web(question)
    payload = {"question_rewritten": question, "top5": results}
    path = _output_path(state, question)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    state["web_search_top5"] = results
    state["web_search_json_path"] = str(path)
    logger.info("MCP 联网检索完成：results=%d json=%s", len(results), path)
    return state


def node_web_search_mcp(state: ImportGraphState) -> ImportGraphState:
    """同步 LangGraph 节点；异步工作流请直接使用 anode_web_search_mcp。

    FastAPI 异步路由所在线程已经有事件循环，不能再调用 asyncio.run。
    这种情况下把搜索放到没有事件循环的线程里执行。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(anode_web_search_mcp(state))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(anode_web_search_mcp(state))).result()
