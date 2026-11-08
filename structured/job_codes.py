"""项目内登记的更新函数。界面只能引用标识，不能提交代码正文。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from structured.sheets import read_table

REGISTRY: dict[str, dict[str, Any]] = {}
SOURCE_CATALOG = "来源清单.csv"


def register(code_id: str, fn, doc_id: str) -> None:
    REGISTRY[code_id] = {"fn": fn, "doc_id": doc_id}


def reload_source_catalog(context) -> None:
    """只重装来源清单。不给其他任务使用。"""
    path = Path(__file__).resolve().parents[1] / "测试数据准备" / SOURCE_CATALOG
    parsed = read_table(path.name, path.read_bytes(), None)
    rows = []
    for record in parsed.rows:
        rows.append({key: record[key] for key in parsed.headers})
    context.replace_rows(rows)


def ensure_builtin_codes(conn) -> None:
    row = conn.execute(
        "SELECT doc_id FROM doc_info WHERE source_file = %s AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
        (SOURCE_CATALOG,),
    ).fetchone()
    if row:
        register("reload_source_catalog", reload_source_catalog, row["doc_id"])


def registered_codes() -> list[dict[str, str]]:
    return [
        {"code_id": code_id, "doc_id": str(item.get("doc_id") or "")}
        for code_id, item in sorted(REGISTRY.items())
    ]
