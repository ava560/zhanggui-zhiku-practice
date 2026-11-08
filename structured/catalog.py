"""表格入库：先待确认，人员确认后再写业务行并切换可查询版本。"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any

from psycopg import Connection, sql

from structured.formula import FormulaError, compile_formula
from structured.schema import ensure_schema
from structured.sheets import (
    RESERVED,
    SheetChoiceNeeded,
    infer_type,
    is_table_file,
    physical_name,
    read_table,
)

logger = logging.getLogger(__name__)

TYPE_SQL = {
    "bigint": sql.SQL("bigint"),
    "numeric": sql.SQL("numeric"),
    "text": sql.SQL("text"),
    "boolean": sql.SQL("boolean"),
    "date": sql.SQL("date"),
    "timestamptz": sql.SQL("timestamptz"),
}
ROLES = {"维度", "度量", "时间", "主键", "属性"}
TABLE_ROLES = {"事实表", "维度表"}
MEASURE_AGGS = {"sum", "avg"}


class CatalogError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


def prepare(conn: Connection) -> None:
    ensure_schema(conn)


def stage_table(
    conn: Connection,
    store: Any,
    filename: str,
    data: bytes,
    *,
    sheet_name: str | None = None,
    upload_task_id: str | None = None,
    doc_id: str | None = None,
    revision: bool = False,
) -> dict[str, Any]:
    if not is_table_file(filename):
        raise CatalogError("只有表格文件可以建业务表")
    try:
        parsed = read_table(filename, data, sheet_name)
    except SheetChoiceNeeded as exc:
        return {"need_sheet": True, "sheets": exc.sheets, "source_file": filename}
    checksum = hashlib.sha256(data).hexdigest()
    logical_id = doc_id or _doc_id_for_filename(conn, filename) or _new_id("doc")
    key = store.put(filename, data)
    if not revision:
        existing = _duplicate_version(conn, logical_id, checksum, parsed.sheet_name, parsed.headers)
        if existing:
            store.delete(key)
            return {"duplicate": True, "doc_id": logical_id, "import_version_id": existing, "source_file": filename}
    import_version_id = _new_id("iv")
    table_id, table_name = _table_identity(conn, logical_id)
    used: set[str] = set()
    columns = []
    for position, header in enumerate(parsed.headers, start=1):
        name = physical_name(header, position, used)
        sample = [row.get(header) for row in parsed.rows[:20]]
        data_type = infer_type(sample)
        columns.append({
            "column_id": _new_id("col"),
            "column_name": name,
            "position": position,
            "data_type": data_type,
            "role": "时间" if data_type in {"date", "timestamptz"} else "属性",
            "aliases": [header],
            "description": f"{header}。含义待确认，说明里的示例不是表中记录。",
            "value_index": False,
            "generated_by": "program",
            "review_status": "pending_review",
        })
    now = datetime.now(timezone.utc)
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO doc_info (
                import_version_id, doc_id, source_file, format, summary, minio_key, checksum,
                sheet_name, upload_task_id, status, data_version_id, fields_snapshot,
                generated_by, review_status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NULL, %s::jsonb, 'program', 'pending_review', %s)
            """,
            (
                import_version_id,
                logical_id,
                filename,
                filename.rsplit(".", 1)[-1].lower(),
                "",
                key,
                checksum,
                parsed.sheet_name,
                upload_task_id,
                _snapshot(columns),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO table_info (
                table_id, import_version_id, doc_id, table_name, role, summary,
                fields_snapshot, generated_by, review_status
            ) VALUES (%s, %s, %s, %s, NULL, '', %s::jsonb, 'program', 'pending_review')
            """,
            (table_id, import_version_id, logical_id, table_name, _names(columns)),
        )
        for column in columns:
            conn.execute(
                """
                INSERT INTO column_info (
                    column_id, import_version_id, table_id, column_name, position, data_type,
                    role, aliases, description, value_index, generated_by, review_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, false, 'program', 'pending_review')
                """,
                (
                    column["column_id"],
                    import_version_id,
                    table_id,
                    column["column_name"],
                    column["position"],
                    column["data_type"],
                    column["role"],
                    _json(column["aliases"]),
                    column["description"],
                ),
            )
    conn.commit()
    return {
        "duplicate": False,
        "doc_id": logical_id,
        "import_version_id": import_version_id,
        "table_id": table_id,
        "table_name": table_name,
        "source_file": filename,
        "sheet_name": parsed.sheet_name,
        "row_count": len(parsed.rows),
        "columns": columns,
        "status": "pending",
    }


def confirm_and_publish(
    conn: Connection,
    store: Any,
    indexes: Any,
    import_version_id: str,
    confirmation: dict[str, Any],
) -> dict[str, Any]:
    version = _require_version(conn, import_version_id)
    if version["status"] == "ready":
        raise CatalogError("该版本已经发布")
    summary = str(confirmation.get("summary") or "").strip()
    table_role = str(confirmation.get("table_role") or "")
    table_summary = str(confirmation.get("table_summary") or "").strip()
    if not summary or not table_summary or table_role not in TABLE_ROLES:
        raise CatalogError("发布前要确认文件简介、表角色和表简介")
    columns = _columns(conn, import_version_id)
    confirmed = _apply_column_confirmation(columns, list(confirmation.get("columns") or []))
    metrics = _validated_metrics(list(confirmation.get("metrics") or []), confirmed, version["table_id"])
    parsed = read_table(version["source_file"], store.get(version["minio_key"]), version["sheet_name"])
    _require_same_headers(parsed.headers, confirmed)
    data_version_id = _new_id("dv")
    records = [_coerce_row(row, confirmed) for row in parsed.rows]
    try:
        with conn.transaction():
            _save_confirmation(conn, version, summary, table_role, table_summary, confirmed, metrics)
            _ensure_business_table(conn, version["table_name"], confirmed)
            _insert_rows(conn, version, confirmed, records, data_version_id)
            conn.execute(
                """
                UPDATE doc_info
                SET status = 'indexing', data_version_id = %s, review_status = 'confirmed', summary = %s
                WHERE import_version_id = %s
                """,
                (data_version_id, summary, import_version_id),
            )
    except Exception:
        conn.rollback()
        raise
    try:
        _write_indexes(indexes, version, confirmed, metrics, records, data_version_id, summary, table_summary)
    except Exception as exc:
        conn.execute(
            "UPDATE doc_info SET status = 'failed' WHERE import_version_id = %s AND status = 'indexing'",
            (import_version_id,),
        )
        conn.commit()
        raise CatalogError(f"索引未完成，可查询版本未切换：{exc}") from exc
    previous = conn.execute(
        """
        SELECT import_version_id, data_version_id FROM doc_info
        WHERE doc_id = %s AND status = 'ready' AND import_version_id <> %s
        """,
        (version["doc_id"], import_version_id),
    ).fetchall()
    with conn.transaction():
        conn.execute(
            "UPDATE doc_info SET status = 'archived' WHERE doc_id = %s AND status = 'ready' AND import_version_id <> %s",
            (version["doc_id"], import_version_id),
        )
        conn.execute(
            "UPDATE doc_info SET status = 'ready' WHERE import_version_id = %s",
            (import_version_id,),
        )
    conn.commit()
    for item in previous:
        try:
            if item["data_version_id"]:
                indexes.delete_values(item["data_version_id"])
            indexes.delete_metadata(item["import_version_id"])
        except Exception:
            logger.warning("旧版本索引清理失败，当前可查询版本已切换")
    return {
        "doc_id": version["doc_id"],
        "import_version_id": import_version_id,
        "data_version_id": data_version_id,
        "table_name": version["table_name"],
        "source_file": version["source_file"],
        "status": "ready",
        "row_count": len(records),
    }


def register_document(
    conn: Connection,
    store: Any,
    filename: str,
    data: bytes,
    *,
    upload_task_id: str | None = None,
    summary: str = "",
) -> dict[str, Any]:
    """PDF、Word、Markdown 只登记文件，不建业务表，也不建值索引。"""
    if is_table_file(filename):
        raise CatalogError("表格文件不能走文档登记")
    checksum = hashlib.sha256(data).hexdigest()
    logical_id = _doc_id_for_filename(conn, filename) or _new_id("doc")
    existing = conn.execute(
        """
        SELECT import_version_id FROM doc_info
        WHERE doc_id = %s AND checksum = %s AND sheet_name IS NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (logical_id, checksum),
    ).fetchone()
    if existing:
        return {"duplicate": True, "doc_id": logical_id, "import_version_id": existing["import_version_id"]}
    key = store.put(filename, data)
    import_version_id = _new_id("iv")
    text = summary.strip()
    confirmed = bool(text)
    conn.execute(
        """
        INSERT INTO doc_info (
            import_version_id, doc_id, source_file, format, summary, minio_key, checksum,
            sheet_name, upload_task_id, status, data_version_id, fields_snapshot,
            generated_by, review_status, created_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, NULL, NULL, 'program', %s, %s
        )
        """,
        (
            import_version_id,
            logical_id,
            filename,
            filename.rsplit(".", 1)[-1].lower(),
            text,
            key,
            checksum,
            upload_task_id,
            "ready" if confirmed else "pending",
            "confirmed" if confirmed else "pending_review",
            datetime.now(timezone.utc),
        ),
    )
    conn.commit()
    return {
        "duplicate": False,
        "doc_id": logical_id,
        "import_version_id": import_version_id,
        "status": "ready" if confirmed else "pending",
    }


def replace_rows(
    conn: Connection,
    indexes: Any,
    doc_id: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """每日任务只增加数据版本。字段不一致时不改当前可查询版本。"""
    current = _ready_table(conn, doc_id)
    columns = _columns(conn, current["import_version_id"])
    names = {column["column_name"] for column in columns}
    for row in rows:
        if set(row) != names:
            raise CatalogError("入库字段与元数据不一致，本次不入库")
    data_version_id = _new_id("dv")
    records = [_coerce_row(_row_by_header(row, columns), columns) for row in rows]
    with conn.transaction():
        _insert_rows(conn, current, columns, records, data_version_id)
    conn.commit()
    indexed = [column for column in columns if column["value_index"]]
    changed = _indexed_values_changed(conn, current, indexed, data_version_id)
    if changed:
        try:
            indexes.write_values(data_version_id, _value_records(current, indexed, records, data_version_id))
        except Exception as exc:
            raise CatalogError(f"新数据版本的值索引未完成，仍查询旧版本：{exc}") from exc
    previous = current["data_version_id"]
    with conn.transaction():
        conn.execute(
            "UPDATE doc_info SET data_version_id = %s WHERE import_version_id = %s AND status = 'ready'",
            (data_version_id, current["import_version_id"]),
        )
    conn.commit()
    if changed and previous:
        indexes.delete_values(previous)
    return {"doc_id": doc_id, "data_version_id": data_version_id, "import_version_id": current["import_version_id"]}


def ready_metadata(conn: Connection) -> list[dict[str, Any]]:
    docs = conn.execute(
        """
        SELECT d.*, t.table_id, t.table_name, t.role AS table_role, t.summary AS table_summary
        FROM doc_info d
        JOIN table_info t ON t.import_version_id = d.import_version_id
        WHERE d.status = 'ready'
        """
    ).fetchall()
    result = []
    for doc in docs:
        item = dict(doc)
        item["columns"] = _columns(conn, doc["import_version_id"])
        item["metrics"] = conn.execute(
            "SELECT * FROM metric_info WHERE import_version_id = %s",
            (doc["import_version_id"],),
        ).fetchall()
        result.append(item)
    return result


def sample_rows(conn: Connection, doc_id: str, limit: int = 5) -> dict[str, Any]:
    current = _ready_table(conn, doc_id)
    columns = _columns(conn, current["import_version_id"])
    names = [sql.Identifier(column["column_name"]) for column in columns]
    statement = sql.SQL(
        "SELECT {} FROM {} WHERE import_version_id = %s AND data_version_id = %s ORDER BY loaded_at LIMIT %s"
    ).format(sql.SQL(", ").join(names), sql.Identifier(current["table_name"]))
    rows = conn.execute(
        statement,
        (current["import_version_id"], current["data_version_id"], limit),
    ).fetchall()
    return {
        "doc_id": doc_id,
        "source_file": current["source_file"],
        "table_name": current["table_name"],
        "columns": [column["column_name"] for column in columns],
        "rows": rows,
    }


def _write_indexes(indexes, version, columns, metrics, records, data_version_id, summary, table_summary) -> None:
    indexed = [column for column in columns if column["value_index"]]
    if indexed:
        indexes.write_values(data_version_id, _value_records(version, indexed, records, data_version_id))
    documents = [{
        "id": f"{version['import_version_id']}:table",
        "kind": "table",
        "table_id": version["table_id"],
        "text": " ".join([summary, table_summary, version["table_name"]]),
    }]
    for column in columns:
        alias_text = " ".join(column["aliases"])
        documents.append({
            "id": f"{version['import_version_id']}:column:{column['column_id']}",
            "kind": "column",
            "table_id": version["table_id"],
            "column_id": column["column_id"],
            "text": " ".join([table_summary, column["column_name"], alias_text, column["description"]]),
        })
    for metric in metrics:
        documents.append({
            "id": f"{version['import_version_id']}:metric:{metric['metric_id']}",
            "kind": "metric",
            "table_id": version["table_id"],
            "metric_id": metric["metric_id"],
            "text": " ".join([metric["name"], metric.get("summary") or "", " ".join(metric.get("aliases") or [])]),
        })
    indexes.write_metadata(version["import_version_id"], documents)


def _save_confirmation(conn, version, summary, table_role, table_summary, columns, metrics) -> None:
    conn.execute(
        """
        UPDATE doc_info
        SET summary = %s, fields_snapshot = %s::jsonb, review_status = 'confirmed', generated_by = 'user'
        WHERE import_version_id = %s
        """,
        (summary, _snapshot(columns), version["import_version_id"]),
    )
    conn.execute(
        """
        UPDATE table_info
        SET role = %s, summary = %s, fields_snapshot = %s::jsonb, review_status = 'confirmed', generated_by = 'user'
        WHERE import_version_id = %s
        """,
        (table_role, table_summary, _names(columns), version["import_version_id"]),
    )
    for column in columns:
        conn.execute(
            """
            UPDATE column_info
            SET data_type = %s, role = %s, aliases = %s::jsonb, description = %s,
                value_index = %s, generated_by = %s, review_status = 'confirmed'
            WHERE column_id = %s AND import_version_id = %s
            """,
            (
                column["data_type"],
                column["role"],
                _json(column["aliases"]),
                column["description"],
                column["value_index"],
                column["generated_by"],
                column["column_id"],
                version["import_version_id"],
            ),
        )
    for metric in metrics:
        conn.execute(
            """
            INSERT INTO metric_info (
                metric_id, import_version_id, table_id, name, summary, aliases, column_ids,
                formula_tree, formula_text, status, review_status
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s)
            """,
            (
                metric["metric_id"],
                version["import_version_id"],
                version["table_id"],
                metric["name"],
                metric["summary"],
                _json(metric["aliases"]),
                _json(metric["column_ids"]),
                _json(metric["formula_tree"]),
                metric["formula_text"],
                metric["status"],
                metric["review_status"],
            ),
        )


def _apply_column_confirmation(existing: list[dict[str, Any]], submitted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {column["column_id"]: column for column in existing}
    if {item.get("column_id") for item in submitted} != set(by_id):
        raise CatalogError("确认的字段和待发布版本不一致")
    confirmed = []
    for item in submitted:
        column = dict(by_id[item["column_id"]])
        data_type = str(item.get("data_type") or "")
        role = str(item.get("role") or "")
        description = str(item.get("description") or "").strip()
        if data_type not in TYPE_SQL or role not in ROLES or not description:
            raise CatalogError("字段类型、角色和说明都要确认")
        aliases = item.get("aliases")
        if aliases is None:
            aliases = column["aliases"]
        if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
            raise CatalogError("字段别名必须是字符串列表")
        column["data_type"] = data_type
        column["role"] = role
        column["description"] = description
        column["aliases"] = aliases
        column["value_index"] = bool(item.get("value_index"))
        if str(item.get("description") or "").strip() != str(by_id[item["column_id"]]["description"]):
            column["generated_by"] = "user"
        column["review_status"] = "confirmed"
        confirmed.append(column)
    return sorted(confirmed, key=lambda column: column["position"])


def _validated_metrics(submitted: list[dict[str, Any]], columns: list[dict[str, Any]], table_id: str) -> list[dict[str, Any]]:
    by_id = {column["column_id"]: column for column in columns}
    metrics = []
    for item in submitted:
        status = str(item.get("status") or "draft")
        if status not in {"draft", "approved"}:
            raise CatalogError("指标状态只能是 draft 或 approved")
        tree = item.get("formula_tree")
        try:
            compile_formula(tree, by_id)
        except FormulaError as exc:
            if status == "approved":
                raise CatalogError(str(exc)) from exc
        column_ids = list(item.get("column_ids") or [])
        if any(column_id not in by_id for column_id in column_ids):
            raise CatalogError("指标只能引用同一张表的字段")
        review = "confirmed" if status == "approved" else "pending_review"
        if status == "approved" and not str(item.get("name") or "").strip():
            raise CatalogError("已确认指标必须有名称")
        metrics.append({
            "metric_id": str(item.get("metric_id") or _new_id("met")),
            "table_id": table_id,
            "name": str(item.get("name") or "").strip() or "未命名指标",
            "summary": str(item.get("summary") or ""),
            "aliases": list(item.get("aliases") or []),
            "column_ids": column_ids,
            "formula_tree": tree,
            "formula_text": str(item.get("formula_text") or ""),
            "status": status,
            "review_status": review,
        })
    return metrics


def _require_same_headers(headers: list[str], columns: list[dict[str, Any]]) -> None:
    expected = []
    for column in sorted(columns, key=lambda item: item["position"]):
        aliases = column["aliases"] or []
        expected.append(aliases[0] if aliases else column["column_name"])
    if headers != expected:
        raise CatalogError("入库字段与元数据不一致，本次不入库")


def _ensure_business_table(conn: Connection, table_name: str, columns: list[dict[str, Any]]) -> None:
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s",
        (table_name,),
    ).fetchone()
    pieces = [
        sql.SQL("{} {}").format(sql.Identifier(column["column_name"]), TYPE_SQL[column["data_type"]])
        for column in columns
    ]
    pieces.extend([
        sql.SQL("import_version_id text NOT NULL"),
        sql.SQL("data_version_id text NOT NULL"),
        sql.SQL("source_file text NOT NULL"),
        sql.SQL("loaded_at timestamptz NOT NULL"),
    ])
    if exists is None:
        conn.execute(
            sql.SQL("CREATE TABLE {} ({})").format(sql.Identifier(table_name), sql.SQL(", ").join(pieces))
        )
        return
    present = {
        row["column_name"]
        for row in conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table_name,),
        ).fetchall()
    }
    wanted = {column["column_name"] for column in columns} | RESERVED
    if present != wanted:
        raise CatalogError("业务表结构和已确认字段不一致，不自动删列或填值")


def _insert_rows(conn, version, columns, records, data_version_id: str) -> None:
    if not records:
        return
    names = [sql.Identifier(column["column_name"]) for column in columns]
    names.extend([
        sql.Identifier("import_version_id"),
        sql.Identifier("data_version_id"),
        sql.Identifier("source_file"),
        sql.Identifier("loaded_at"),
    ])
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(version["table_name"]),
        sql.SQL(", ").join(names),
        sql.SQL(", ").join(sql.Placeholder() for _ in names),
    )
    now = datetime.now(timezone.utc)
    payload = []
    for record in records:
        values = [record[column["column_name"]] for column in columns]
        values.extend([version["import_version_id"], data_version_id, version["source_file"], now])
        payload.append(tuple(values))
    conn.cursor().executemany(statement, payload)


def _coerce_row(row: dict[str, Any], columns: list[dict[str, Any]]) -> dict[str, Any]:
    coerced = {}
    for column in columns:
        header = (column["aliases"] or [column["column_name"]])[0]
        raw = row.get(header, row.get(column["column_name"]))
        try:
            coerced[column["column_name"]] = _coerce(raw, column["data_type"])
        except (TypeError, ValueError) as exc:
            raise CatalogError(f"字段 {header} 的值和确认类型不一致") from exc
    return coerced


def _coerce(value: Any, data_type: str) -> Any:
    if value in (None, ""):
        return None
    if data_type == "text":
        return str(value)
    if data_type == "bigint":
        if isinstance(value, bool):
            raise TypeError(value)
        return int(value)
    if data_type == "numeric":
        if isinstance(value, bool):
            raise TypeError(value)
        return str(value) if not isinstance(value, (int, float)) else value
    if data_type == "boolean":
        if isinstance(value, bool):
            return value
        raise TypeError(value)
    if data_type == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])
    if data_type == "timestamptz":
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        raise TypeError(value)
    raise TypeError(data_type)


def _row_by_header(row: dict[str, Any], columns: list[dict[str, Any]]) -> dict[str, Any]:
    mapped = {}
    for column in columns:
        header = (column["aliases"] or [column["column_name"]])[0]
        if column["column_name"] in row:
            mapped[header] = row[column["column_name"]]
        elif header in row:
            mapped[header] = row[header]
    return mapped


def _value_records(version, columns, records, data_version_id: str) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for column in columns:
        for record in records:
            value = record.get(column["column_name"])
            if value in (None, ""):
                continue
            text = str(value)
            key = (column["column_id"], text)
            if key in seen:
                continue
            seen.add(key)
            output.append({
                "column_id": column["column_id"],
                "table_id": version["table_id"],
                "import_version_id": version["import_version_id"],
                "data_version_id": data_version_id,
                "value": text,
            })
    return output


def _indexed_values_changed(conn, current, columns, new_version: str) -> bool:
    if not columns:
        return False
    for column in columns:
        statement = sql.SQL(
            "SELECT DISTINCT {} AS value FROM {} WHERE import_version_id = %s AND data_version_id = %s"
        ).format(sql.Identifier(column["column_name"]), sql.Identifier(current["table_name"]))
        old_values = {
            str(row["value"])
            for row in conn.execute(statement, (current["import_version_id"], current["data_version_id"])).fetchall()
            if row["value"] not in (None, "")
        }
        new_values = {
            str(row["value"])
            for row in conn.execute(statement, (current["import_version_id"], new_version)).fetchall()
            if row["value"] not in (None, "")
        }
        if old_values != new_values:
            return True
    return False


def _ready_table(conn: Connection, doc_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT d.import_version_id, d.doc_id, d.source_file, d.data_version_id, t.table_id, t.table_name
        FROM doc_info d
        JOIN table_info t ON t.import_version_id = d.import_version_id
        WHERE d.doc_id = %s AND d.status = 'ready'
        """,
        (doc_id,),
    ).fetchone()
    if row is None or not row["data_version_id"]:
        raise CatalogError("没有可查询的表版本")
    return dict(row)


def _require_version(conn: Connection, import_version_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT d.*, t.table_id, t.table_name
        FROM doc_info d
        JOIN table_info t ON t.import_version_id = d.import_version_id
        WHERE d.import_version_id = %s
        """,
        (import_version_id,),
    ).fetchone()
    if row is None:
        raise CatalogError("导入版本不存在")
    return dict(row)


def _columns(conn: Connection, import_version_id: str) -> list[dict[str, Any]]:
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


def _duplicate_version(conn, doc_id: str, checksum: str, sheet_name: str, headers: list[str]) -> str | None:
    rows = conn.execute(
        """
        SELECT import_version_id FROM doc_info
        WHERE doc_id = %s AND checksum = %s AND sheet_name = %s
        ORDER BY created_at DESC
        """,
        (doc_id, checksum, sheet_name),
    ).fetchall()
    for row in rows:
        columns = _columns(conn, row["import_version_id"])
        signature = [(column["aliases"] or [column["column_name"]])[0] for column in columns]
        if signature == headers:
            return row["import_version_id"]
    return None


def _doc_id_for_filename(conn, filename: str) -> str | None:
    row = conn.execute(
        "SELECT doc_id FROM doc_info WHERE source_file = %s ORDER BY created_at DESC LIMIT 1",
        (filename,),
    ).fetchone()
    return None if row is None else row["doc_id"]


def _table_identity(conn, doc_id: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT table_id, table_name FROM table_info WHERE doc_id = %s ORDER BY table_id LIMIT 1",
        (doc_id,),
    ).fetchone()
    if row:
        return row["table_id"], row["table_name"]
    table_id = _new_id("tbl")
    table_name = "t" + uuid.uuid4().hex[:16]
    taken = conn.execute("SELECT 1 FROM table_info WHERE table_name = %s", (table_name,)).fetchone()
    if taken:
        table_name = "t" + uuid.uuid4().hex[:16]
    return table_id, table_name


def _snapshot(columns: list[dict[str, Any]]) -> str:
    return _json([
        {"column_name": column["column_name"], "description": column["description"]}
        for column in sorted(columns, key=lambda item: item["position"])
    ])


def _names(columns: list[dict[str, Any]]) -> str:
    return _json([column["column_name"] for column in sorted(columns, key=lambda item: item["position"])])


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
