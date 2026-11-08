from structured.indexes import live_index
from structured.query_table import query_table_data
from structured.session import open_db

conn = open_db()
try:
    row = conn.execute(
        """
        SELECT t.table_id
        FROM table_info t
        JOIN doc_info d ON d.import_version_id = t.import_version_id
        WHERE d.source_file = %s AND d.status = 'ready'
        """,
        ("来源清单.csv",),
    ).fetchone()
    found = query_table_data(
        "来源清单里平安银行 2025 年度报告有多少页",
        conn=conn,
        indexes=live_index(),
        plan={
            "table_id": row["table_id"],
            "columns": ["name", "pages"],
            "filters": [{"column": "name", "op": "contains", "value": "平安银行_2025"}],
        },
    )
    print(found.get("table_reason"), found.get("rows") or found.get("table_results", [{}])[0].get("rows"))
finally:
    conn.close()
