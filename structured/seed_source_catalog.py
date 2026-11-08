"""把来源清单登记进 PostgreSQL。字段说明在这里写明，不调用模型草拟。"""

from __future__ import annotations

from pathlib import Path

from structured.catalog import confirm_and_publish, stage_table
from structured.indexes import live_index
from structured.job_codes import ensure_builtin_codes
from structured.objects import MinioStore
from structured.session import open_db

SOURCE_NAME = "来源清单.csv"
SUMMARY = "已下载资料的来源清单，一行对应一个文件。"
TABLE_SUMMARY = "记录文件名、大小、页数、来源地址和校验值。清单里的数字是文件属性，不是业务流水。"
FIELDS = {
    "name": ("维度", "资料文件名。"),
    "actual_bytes": ("属性", "文件字节数。说明文字不能代替表中的字节记录。"),
    "actual_MB": ("属性", "文件大小，单位是 MB。"),
    "pages": ("属性", "页数。空值表示这份清单没有填写页数。"),
    "url": ("属性", "原件下载地址。"),
    "source": ("属性", "来源编号。"),
    "sha256": ("属性", "文件校验值。"),
    "format": ("属性", "版式说明。"),
    "parser_note": ("属性", "解析备注。"),
}


def publish_source_catalog() -> dict:
    path = Path(__file__).resolve().parents[1] / "测试数据准备" / SOURCE_NAME
    data = path.read_bytes()
    conn = open_db()
    try:
        ready = conn.execute(
            """
            SELECT doc_id, import_version_id FROM doc_info
            WHERE source_file = %s AND status = 'ready'
            ORDER BY created_at DESC LIMIT 1
            """,
            (SOURCE_NAME,),
        ).fetchone()
        if ready:
            ensure_builtin_codes(conn)
            return {"status": "ready", "doc_id": ready["doc_id"], "import_version_id": ready["import_version_id"]}
        staged = stage_table(conn, MinioStore(), SOURCE_NAME, data)
        if staged.get("duplicate"):
            version_id = staged["import_version_id"]
            columns = _columns(conn, version_id)
        else:
            version_id = staged["import_version_id"]
            columns = staged["columns"]
        result = confirm_and_publish(
            conn,
            MinioStore(),
            live_index(),
            version_id,
            {
                "summary": SUMMARY,
                "table_role": "维度表",
                "table_summary": TABLE_SUMMARY,
                "columns": [_confirm(column) for column in columns],
                "metrics": [],
            },
        )
        ensure_builtin_codes(conn)
        return result
    finally:
        conn.close()


def _columns(conn, import_version_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM column_info WHERE import_version_id = %s ORDER BY position",
        (import_version_id,),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["aliases"] = list(item.get("aliases") or [])
        output.append(item)
    return output


def _confirm(column: dict) -> dict:
    header = (column.get("aliases") or [column["column_name"]])[0]
    role, description = FIELDS[header]
    return {
        "column_id": column["column_id"],
        "description": description,
        "role": role,
        "data_type": column["data_type"],
        "aliases": column.get("aliases") or [header],
        "value_index": False,
    }


if __name__ == "__main__":
    published = publish_source_catalog()
    print(published.get("status"), published.get("source_file", SOURCE_NAME), published.get("row_count", ""))
