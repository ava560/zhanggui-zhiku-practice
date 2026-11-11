"""从数据库删掉项目时，同时删掉 MinIO 原件和 Milvus 向量。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from psycopg import Connection, sql

from structured.library import LibraryError
from structured.schema import ensure_schema


METADATA_TABLES = (
    (
        "doc_info",
        "一个导入版本一行。同一逻辑文件的 doc_id 保持不变。",
        (
            ("import_version_id", "text", "主键。内容、工作表或已确认的元数据各用一个新值"),
            ("doc_id", "text", "同一逻辑文件保持不变。同时最多一行 status 为 ready"),
            ("source_file", "text", "原文件名"),
            ("format", "text", "文件格式"),
            ("summary", "text", "简介。发布前必须已确认"),
            ("minio_key", "text", "原件对象键"),
            ("checksum", "text", "原件 SHA-256"),
            ("sheet_name", "text", "表格所选工作表。非表格文件为空"),
            ("upload_task_id", "text", "上传任务标识，可空"),
            ("status", "text", "pending、indexing、ready、archived、failed"),
            ("data_version_id", "text", "当前可查询的数据版本。非 ready 时可空"),
            ("fields_snapshot", "jsonb", "同版字段名和说明，生成后不手改"),
            ("generated_by", "text", "简介来源：llm、user 或 program"),
            ("review_status", "text", "pending_review 或 confirmed"),
            ("created_at", "timestamptz", "创建时间"),
        ),
        ("source_file", "format", "status", "review_status", "created_at"),
    ),
    (
        "table_info",
        "一个导入版本中的一张逻辑业务表一行。",
        (
            ("table_id", "text", "同一逻辑业务表跨导入版本不变"),
            ("import_version_id", "text", "指向 doc_info"),
            ("doc_id", "text", "与该导入版本的 doc_id 一致"),
            ("table_name", "text", "物理表名，库内唯一"),
            ("role", "text", "事实表或维度表"),
            ("summary", "text", "业务简介"),
            ("fields_snapshot", "jsonb", "同版字段名，只供展示"),
            ("generated_by", "text", "简介来源"),
            ("review_status", "text", "pending_review 或 confirmed"),
        ),
        ("table_name", "role", "summary", "review_status"),
    ),
    (
        "column_info",
        "一个导入版本中的一个物理列一行。字段定义以这张表为准。",
        (
            ("column_id", "text", "同一物理列在后续版本中保持不变"),
            ("import_version_id", "text", "指向 doc_info"),
            ("table_id", "text", "指向同版 table_info"),
            ("column_name", "text", "物理列名"),
            ("position", "integer", "从 1 开始"),
            ("data_type", "text", "已确认的数据类型"),
            ("role", "text", "维度、度量、时间、主键或属性"),
            ("aliases", "jsonb", "别名，不代替物理列名"),
            ("description", "text", "字段说明，只解释含义"),
            ("value_index", "boolean", "是否写入值索引"),
            ("generated_by", "text", "说明或别名的来源"),
            ("review_status", "text", "pending_review 或 confirmed"),
        ),
        ("column_name", "data_type", "role", "description"),
    ),
    (
        "metric_info",
        "一个导入版本中的一个指标一行。可以没有指标。",
        (
            ("metric_id", "text", "同一指标跨版本保持不变"),
            ("import_version_id", "text", "指向 doc_info"),
            ("table_id", "text", "只能指向同版的一张表"),
            ("name", "text", "指标名"),
            ("summary", "text", "业务描述"),
            ("aliases", "jsonb", "别名"),
            ("column_ids", "jsonb", "引用列，必须属于同一张表和同一导入版本"),
            ("formula_tree", "jsonb", "受限表达式，不在这里执行"),
            ("formula_text", "text", "给人看的公式"),
            ("status", "text", "draft 或 approved。只有 approved 参与计算"),
            ("review_status", "text", "approved 时必须为 confirmed"),
        ),
        ("name", "status", "review_status", "formula_text"),
    ),
)


def list_tables(conn: Connection) -> dict[str, Any]:
    """四张元数据表，以及已经确认、当前可以查询的业务表。"""
    ensure_schema(conn)
    tables = [_metadata_table(conn, spec) for spec in METADATA_TABLES]
    rows = conn.execute(
        """
        SELECT d.doc_id, d.source_file, d.summary, d.data_version_id, d.import_version_id,
               t.table_id, t.table_name, t.summary AS table_summary, t.role
        FROM doc_info d
        JOIN table_info t ON t.import_version_id = d.import_version_id
        WHERE d.status = 'ready'
        ORDER BY t.table_name, d.doc_id
        """
    ).fetchall()
    for row in rows:
        columns = conn.execute(
            """
            SELECT column_name, data_type, description, position, aliases
            FROM column_info
            WHERE import_version_id = %s AND table_id = %s
            ORDER BY position
            """,
            (row["import_version_id"], row["table_id"]),
        ).fetchall()
        labeled = []
        for column in columns:
            label = _alias(column["aliases"], column["column_name"])
            labeled.append({
                "name": column["column_name"],
                "label": label,
                "type": column["data_type"],
                "description": column["description"] or "",
            })
        tables.append({
            "kind": "business",
            "doc_id": row["doc_id"],
            "source_file": row["source_file"],
            "summary": row["table_summary"] or row["summary"] or "",
            "role": row["role"] or "",
            "table_id": row["table_id"],
            "table_name": row["table_name"],
            "data_version_id": row["data_version_id"] or "",
            "row_count": _row_count(conn, row["table_name"], row["data_version_id"]),
            "columns": labeled,
            "rows": _version_rows(conn, row["table_name"], row["data_version_id"], labeled),
        })
    conn.rollback()
    return {"tables": tables}


def remove_library_item(
    conn: Connection,
    kind: str,
    item_id: str,
    *,
    store: Any,
    indexes: Any,
    milvus_delete: Callable[[str], None],
    delete_basename: Callable[[str], None] | None = None,
    disk_path: Callable[[str], Path] | None = None,
    disk_root: Path | None = None,
) -> dict[str, str]:
    ensure_schema(conn)
    text = str(item_id or "").strip()
    if kind == "folder":
        return _remove_folder(conn, text)
    if kind == "file":
        return _remove_document(conn, text, store, indexes, milvus_delete, delete_basename, disk_root)
    if kind == "code":
        return _remove_code(conn, text, store)
    if kind == "upload":
        return _remove_upload(conn, text, store, indexes, milvus_delete, delete_basename, disk_path, disk_root)
    raise LibraryError("只能删除文件夹、文件或代码")


def delete_milvus_file(file_name: str) -> None:
    """按文件名删掉文档切片和条目名向量。"""
    from upload.node_import_milvus import collection_fields, escape_milvus_string, get_milvus_client, load_settings
    from upload.node_item_name_recognition import load_settings as load_item_settings

    settings = load_settings()
    client = get_milvus_client(settings)
    try:
        _delete_collection_file(client, settings.chunks_collection, file_name, collection_fields, escape_milvus_string)
        item_settings = load_item_settings()
        collection = item_settings.item_name_collection
        if client.has_collection(collection):
            _delete_collection_file(client, collection, file_name, collection_fields, escape_milvus_string)
    except LibraryError:
        raise
    except Exception as exc:
        raise LibraryError("Milvus 删除失败") from exc
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def delete_minio_basename(file_name: str) -> None:
    """删掉对象名正好是这个文件名的原件。已知对象键走各自的存储删除。"""
    from minio import Minio
    from minio.deleteobjects import DeleteObject

    from node_upload_minio import load_settings

    settings = load_settings()
    client = Minio(
        settings.endpoint,
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        secure=settings.secure,
    )
    matched = []
    for obj in client.list_objects(settings.bucket, recursive=True):
        name = str(getattr(obj, "object_name", "") or "")
        if name and Path(name).name == file_name:
            matched.append(DeleteObject(name))
    if not matched:
        return
    errors = list(client.remove_objects(settings.bucket, matched))
    if errors:
        raise LibraryError("MinIO 删除失败")


def _remove_folder(conn: Connection, folder_id: str) -> dict[str, str]:
    found = conn.execute("SELECT 1 FROM library_folder WHERE folder_id = %s", (folder_id,)).fetchone()
    if found is None:
        raise LibraryError("文件夹不存在")
    child = conn.execute("SELECT 1 FROM library_folder WHERE parent_id = %s", (folder_id,)).fetchone()
    held = conn.execute("SELECT 1 FROM library_placement WHERE folder_id = %s", (folder_id,)).fetchone()
    if child or held:
        raise LibraryError("文件夹里还有内容，先移走或删除里面的项目")
    conn.execute("DELETE FROM library_folder WHERE folder_id = %s", (folder_id,))
    conn.commit()
    return {"kind": "folder", "id": folder_id}


def _remove_document(conn, doc_id: str, store: Any, indexes: Any, milvus_delete: Callable[[str], None], delete_basename: Callable[[str], None] | None = None, disk_root: Path | None = None) -> dict[str, str]:
    versions = list(conn.execute(
        """
        SELECT import_version_id, source_file, minio_key, data_version_id
        FROM doc_info
        WHERE doc_id = %s AND status <> 'archived'
        """,
        (doc_id,),
    ).fetchall())
    if not versions:
        raise LibraryError("要删除的文件不存在")
    busy = conn.execute(
        "SELECT name FROM data_job WHERE enabled AND %s = ANY(doc_ids) LIMIT 1",
        (doc_id,),
    ).fetchone()
    if busy:
        raise LibraryError(f"定时任务「{busy['name']}」还在使用这个文件")
    names = sorted({str(row["source_file"]) for row in versions})
    for name in names:
        shared = conn.execute(
            """
            SELECT 1 FROM doc_info
            WHERE source_file = %s AND doc_id <> %s AND status <> 'archived'
            """,
            (name, doc_id),
        ).fetchone()
        if shared is None:
            _clear_disk_copies(name, disk_root)
            _clear_upload_placements(conn, name)
            milvus_delete(name)
            if delete_basename is not None:
                delete_basename(name)
    for row in versions:
        if row["data_version_id"]:
            indexes.delete_values(str(row["data_version_id"]))
        indexes.delete_metadata(str(row["import_version_id"]))
        _delete_key(store, str(row["minio_key"]))
    conn.execute("UPDATE doc_info SET status = 'archived' WHERE doc_id = %s AND status <> 'archived'", (doc_id,))
    conn.execute("DELETE FROM library_placement WHERE item_kind = 'file' AND item_id = %s", (doc_id,))
    conn.commit()
    return {"kind": "file", "id": doc_id}


def _remove_code(conn, code_id: str, store: Any) -> dict[str, str]:
    versions = list(conn.execute(
        "SELECT minio_key FROM code_version WHERE code_id = %s AND status <> 'archived'",
        (code_id,),
    ).fetchall())
    if not versions:
        raise LibraryError("要删除的代码不存在")
    busy = conn.execute(
        "SELECT name FROM data_job WHERE enabled AND code_id = %s LIMIT 1",
        (code_id,),
    ).fetchone()
    if busy:
        raise LibraryError(f"定时任务「{busy['name']}」还在使用这段代码")
    for row in versions:
        _delete_key(store, str(row["minio_key"]))
    conn.execute("UPDATE code_version SET status = 'archived' WHERE code_id = %s AND status <> 'archived'", (code_id,))
    conn.execute("DELETE FROM library_placement WHERE item_kind = 'code' AND item_id = %s", (code_id,))
    conn.commit()
    return {"kind": "code", "id": code_id}


def _remove_upload(conn, item_id, store, indexes, milvus_delete, delete_basename, disk_path, disk_root) -> dict[str, str]:
    task, separator, name = item_id.partition(":")
    if not separator or not task or not name or "/" in name or "\\" in name or ".." in task:
        raise LibraryError("要删除的上传文件不存在")
    path = (disk_path or _task_source)(item_id)
    copies = _disk_copies(name, disk_root)
    if path.is_file() and path not in copies:
        copies.append(path)
    if not copies and not path.is_file():
        raise LibraryError("要删除的上传文件不存在")
    linked = list(conn.execute(
        """
        SELECT doc_id FROM doc_info
        WHERE source_file = %s AND status <> 'archived'
        """,
        (name,),
    ).fetchall())
    for row in linked:
        busy = conn.execute(
            "SELECT name FROM data_job WHERE enabled AND %s = ANY(doc_ids) LIMIT 1",
            (row["doc_id"],),
        ).fetchone()
        if busy:
            raise LibraryError(f"定时任务「{busy['name']}」还在使用这个文件")
    for copy in copies:
        copy.unlink(missing_ok=True)
    if path.is_file():
        path.unlink(missing_ok=True)
    for row in linked:
        _remove_document(conn, row["doc_id"], store, indexes, milvus_delete, delete_basename, disk_root)
    if not linked:
        milvus_delete(name)
        if delete_basename is not None:
            delete_basename(name)
    _clear_upload_placements(conn, name)
    conn.commit()
    return {"kind": "upload", "id": item_id}


def _disk_copies(name: str, root: Path | None = None) -> list[Path]:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        return []
    base = root if root is not None else _task_root()
    if not base.exists():
        return []
    return [path for path in base.glob(f"*/source/{name}") if path.is_file()]


def _clear_disk_copies(name: str, root: Path | None = None) -> None:
    for path in _disk_copies(name, root):
        path.unlink(missing_ok=True)


def _clear_upload_placements(conn, name: str) -> None:
    rows = conn.execute("SELECT item_id FROM library_placement WHERE item_kind = 'upload'").fetchall()
    for row in rows:
        if str(row["item_id"]).endswith(":" + name):
            conn.execute(
                "DELETE FROM library_placement WHERE item_kind = 'upload' AND item_id = %s",
                (row["item_id"],),
            )


def _task_root() -> Path:
    from web_server import TASK_ROOT

    return Path(TASK_ROOT)


def _task_source(item_id: str) -> Path:
    from web_server import TASK_ROOT

    task, _, name = item_id.partition(":")
    root = Path(TASK_ROOT).resolve()
    path = (root / task / "source" / name).resolve()
    if root not in path.parents:
        raise LibraryError("要删除的上传文件不存在")
    return path


def _delete_collection_file(client, collection: str, file_name: str, fields_of, escape) -> None:
    if not client.has_collection(collection):
        return
    fields = fields_of(client, collection)
    escaped = escape(file_name)
    client.load_collection(collection_name=collection)
    if "file_name" in fields:
        client.delete(collection_name=collection, filter=f'file_name == "{escaped}"')
    elif "file_title" in fields:
        client.delete(collection_name=collection, filter=f'file_title == "{escaped}"')


def _delete_key(store: Any, key: str) -> None:
    if not key:
        return
    try:
        store.delete(key)
    except Exception as exc:
        text = str(exc)
        if "NoSuchKey" in text or "NoSuchKey" in type(exc).__name__ or "not found" in text.lower():
            return
        raise LibraryError("MinIO 删除失败") from exc


def _metadata_table(conn: Connection, spec: tuple) -> dict[str, Any]:
    name, summary, columns, preview_names = spec
    preview = _metadata_preview(conn, name, preview_names)
    return {
        "kind": "metadata",
        "doc_id": "",
        "source_file": name,
        "summary": summary,
        "role": "元数据表",
        "table_id": name,
        "table_name": name,
        "data_version_id": "",
        "row_count": _plain_count(conn, name),
        "columns": [{"name": column[0], "label": column[0], "type": column[1], "description": column[2]} for column in columns],
        "rows": preview,
    }


def version_preview(conn: Connection, doc_id: str, data_version_id: str, limit: int = 20) -> dict[str, Any]:
    """按数据版本取出加工结果。列名用已确认的字段说明，不用物理列名。"""
    if not doc_id or not data_version_id:
        return {"columns": [], "rows": []}
    found = conn.execute(
        """
        SELECT t.table_name, d.import_version_id
        FROM doc_info d
        JOIN table_info t ON t.import_version_id = d.import_version_id
        WHERE d.doc_id = %s AND d.status = 'ready'
        """,
        (doc_id,),
    ).fetchone()
    if found is None:
        return {"columns": [], "rows": []}
    stored = conn.execute(
        """
        SELECT column_name, aliases
        FROM column_info
        WHERE import_version_id = %s
        ORDER BY position
        """,
        (found["import_version_id"],),
    ).fetchall()
    columns = [
        {"name": column["column_name"], "label": _alias(column["aliases"], column["column_name"])}
        for column in stored
    ]
    return {
        "columns": [column["label"] for column in columns],
        "rows": _version_rows(conn, found["table_name"], data_version_id, columns, limit),
    }


def _alias(raw: Any, fallback: str) -> str:
    aliases = raw
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except ValueError:
            aliases = []
    if isinstance(aliases, list) and aliases and isinstance(aliases[0], str) and aliases[0].strip():
        return aliases[0]
    return fallback


def _version_rows(
    conn: Connection,
    table_name: str,
    data_version_id: str | None,
    columns: list[dict[str, str]],
    limit: int = 8,
) -> list[dict[str, str]]:
    if not data_version_id or not columns:
        return []
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s",
        (table_name,),
    ).fetchone()
    if exists is None:
        return []
    selected = sql.SQL(", ").join(sql.Identifier(column["name"]) for column in columns)
    statement = sql.SQL("SELECT {} FROM {} WHERE data_version_id = %s ORDER BY loaded_at LIMIT %s").format(
        selected,
        sql.Identifier(table_name),
    )
    found = conn.execute(statement, (data_version_id, limit)).fetchall()
    rows = []
    for row in found:
        rows.append({
            column["label"]: "" if row[column["name"]] is None else str(row[column["name"]])
            for column in columns
        })
    return rows


def _metadata_preview(conn: Connection, table_name: str, column_names: tuple[str, ...]) -> list[dict[str, str]]:
    if table_name not in {item[0] for item in METADATA_TABLES}:
        return []
    order = " ORDER BY created_at DESC" if "created_at" in column_names else ""
    selected = sql.SQL(", ").join(sql.Identifier(name) for name in column_names)
    statement = sql.SQL("SELECT {} FROM {}{} LIMIT 8").format(selected, sql.Identifier(table_name), sql.SQL(order))
    found = conn.execute(statement).fetchall()
    rows = []
    for row in found:
        rows.append({name: "" if row[name] is None else str(row[name]) for name in column_names})
    return rows


def _plain_count(conn: Connection, table_name: str) -> int:
    if table_name not in {item[0] for item in METADATA_TABLES}:
        return 0
    row = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table_name))).fetchone()
    return int(row["n"])


def _row_count(conn: Connection, table_name: str, data_version_id: str | None) -> int:
    if not data_version_id:
        return 0
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s",
        (table_name,),
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute(
        sql.SQL("SELECT count(*) AS n FROM {} WHERE data_version_id = %s").format(sql.Identifier(table_name)),
        (data_version_id,),
    ).fetchone()
    return int(row["n"])
