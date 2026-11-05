"""研究工具白名单。资料正文里的指令不能扩大权限。"""

from __future__ import annotations

from typing import Any, Callable


ALLOWED_TOOLS = {
    "resolve_entities",
    "list_available_sources",
    "search_evidence",
    "fetch_evidence",
    "query_relations",
    "extract_metrics",
    "calculate_metrics",
}


def instruction_overrides_tools(evidence: str) -> bool:
    """文档内容永远不能改写工具权限。"""
    del evidence
    return False


def execute_tool(
    name: str,
    args: dict[str, Any],
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
) -> Any:
    if name not in ALLOWED_TOOLS:
        raise PermissionError(f"工具不在白名单：{name}")
    blocked = {"cypher", "sql", "command", "shell", "code"}
    if blocked.intersection(args):
        raise PermissionError("不允许任意查询或命令")
    handler = handlers.get(name)
    if handler is None:
        raise PermissionError(f"工具未配置：{name}")
    return handler(args)
