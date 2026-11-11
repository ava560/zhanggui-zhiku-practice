"""用户要求时，把已经查到且字段一致的结果写成新数据版本。对话不执行脚本。"""

from __future__ import annotations

from typing import Any

from structured.catalog import ready_metadata, replace_rows

_REQUESTS = ("写回", "写回到", "保存到表", "追加到表", "写入这张表", "写入该表")


def wants_writeback(question: str) -> bool:
    text = str(question or "")
    return any(token in text for token in _REQUESTS)


def write_back_rows(
    conn,
    indexes: Any,
    table_id: str,
    rows: list[dict[str, Any]],
    *,
    question: str,
    lineage: Any = None,
    skill_id: str = "",
) -> dict[str, Any]:
    if not wants_writeback(question):
        return {"written": False, "message": "只读提问不写回", "data_version_id": ""}
    table = next((item for item in ready_metadata(conn) if item["table_id"] == table_id), None)
    if table is None:
        return {"written": False, "message": "没有可写回的已登记表", "data_version_id": ""}
    accepted = _matching_rows(table, rows)
    if not accepted:
        return {"written": False, "message": "结果和已确认字段不一致，没有写回", "data_version_id": ""}
    saved = replace_rows(
        conn,
        indexes,
        table["doc_id"],
        accepted,
        lineage=lineage,
        producer="llm",
        skill_id=skill_id,
        steps=[{"order": 1, "action": "这是模型步骤：按用户要求写回已查到且字段一致的结果"}],
    )
    return {"written": True, "message": "已写回新的数据版本", "data_version_id": saved["data_version_id"]}


def _matching_rows(table: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    columns = list(table.get("columns") or [])
    names = {column["column_name"] for column in columns}
    headers = {(column.get("aliases") or [column["column_name"]])[0] for column in columns}
    accepted = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cleaned = {key: value for key, value in row.items() if key in names or key in headers}
        keys = set(cleaned)
        if keys == names or keys == headers:
            accepted.append(cleaned)
    return accepted
