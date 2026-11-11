"""数据库目录：已上传的表、文件和代码，以及用户建的文件夹。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg import Connection

from structured.schema import ensure_schema


class LibraryError(ValueError):
    pass


def list_library(conn: Connection, folder_id: str = "", uploads: list[dict[str, str]] | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    current = str(folder_id or "")
    if current:
        _require_folder(conn, current)
    placements = {
        (row["item_kind"], row["item_id"]): str(row["folder_id"] or "")
        for row in conn.execute("SELECT item_kind, item_id, folder_id FROM library_placement").fetchall()
    }
    items: list[dict[str, str]] = []
    folders = conn.execute(
        "SELECT folder_id, name, created_at FROM library_folder WHERE parent_id = %s ORDER BY name, folder_id",
        (current,),
    ).fetchall()
    for folder in folders:
        items.append(_item("folder", folder["folder_id"], folder["name"], "文件夹", folder["created_at"]))
    for row in _documents(conn):
        if placements.get(("file", row["doc_id"]), "") != current:
            continue
        detail = row["table_summary"] or _status_text(row["status"])
        items.append(_item("file", row["doc_id"], row["source_file"], detail, row["created_at"]))
    for item in uploads if uploads is not None else disk_uploads():
        item_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        if not item_id or not name or placements.get(("upload", item_id), "") != current:
            continue
        items.append(_item("upload", item_id, name, "已上传文件", item.get("modified") or ""))
    for row in _codes(conn):
        if placements.get(("code", row["code_id"]), "") != current:
            continue
        detail = "可运行代码" if row["status"] == "ready" else "待确认代码"
        items.append(_item("code", row["code_id"], row["name"] or row["code_id"], detail, row["created_at"]))
    crumbs = _breadcrumb(conn, current)
    conn.rollback()
    return {"folder_id": current, "breadcrumb": crumbs, "items": items}


def create_folder(conn: Connection, name: str, parent_id: str = "") -> dict[str, str]:
    ensure_schema(conn)
    title = _folder_name(name)
    parent = str(parent_id or "")
    if parent:
        _require_folder(conn, parent)
    folder_id = "fld_" + uuid.uuid4().hex
    conn.execute(
        "INSERT INTO library_folder (folder_id, name, parent_id) VALUES (%s, %s, %s)",
        (folder_id, title, parent),
    )
    conn.commit()
    return {"folder_id": folder_id, "name": title, "parent_id": parent}


def rename_folder(conn: Connection, folder_id: str, name: str) -> dict[str, str]:
    ensure_schema(conn)
    text = str(folder_id or "").strip()
    _require_folder(conn, text)
    title = _folder_name(name)
    conn.execute("UPDATE library_folder SET name = %s WHERE folder_id = %s", (title, text))
    conn.commit()
    return {"folder_id": text, "name": title}


def move_item(conn: Connection, kind: str, item_id: str, folder_id: str = "", uploads: list[dict[str, str]] | None = None) -> dict[str, str]:
    ensure_schema(conn)
    target = str(folder_id or "")
    if target:
        _require_folder(conn, target)
    text = str(item_id or "").strip()
    if kind == "folder":
        _move_folder(conn, text, target)
    elif kind in {"file", "code", "upload"}:
        _require_item(conn, kind, text, uploads)
        conn.execute(
            """
            INSERT INTO library_placement (item_kind, item_id, folder_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (item_kind, item_id) DO UPDATE SET folder_id = EXCLUDED.folder_id
            """,
            (kind, text, target),
        )
    else:
        raise LibraryError("只能移动文件夹、文件或代码")
    conn.commit()
    return {"kind": kind, "id": text, "folder_id": target}


def disk_uploads() -> list[dict[str, str]]:
    """扫描入库目录里的原件。任务内存列表重启后仍能从这里看到。"""
    try:
        from web_server import TASK_ROOT
    except Exception:
        return []
    root = Path(TASK_ROOT)
    if not root.exists():
        return []
    found = []
    for path in root.glob("*/source/*"):
        if not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        found.append({"id": f"{path.parent.parent.name}:{path.name}", "name": path.name, "modified": modified})
    found.sort(key=lambda item: item["name"])
    return found


def _documents(conn: Connection) -> list[dict[str, Any]]:
    return list(conn.execute(
        """
        SELECT DISTINCT ON (d.doc_id)
               d.doc_id, d.source_file, d.status, d.created_at, COALESCE(t.summary, '') AS table_summary
        FROM doc_info d
        LEFT JOIN table_info t ON t.import_version_id = d.import_version_id
        WHERE d.status <> 'archived'
        ORDER BY d.doc_id, d.created_at DESC
        """
    ).fetchall())


def _codes(conn: Connection) -> list[dict[str, Any]]:
    if not conn.execute("SELECT to_regclass('public.code_version') AS name").fetchone()["name"]:
        return []
    return list(conn.execute(
        """
        SELECT DISTINCT ON (code_id) code_id, name, status, created_at
        FROM code_version
        WHERE status <> 'archived'
        ORDER BY code_id, (status = 'ready') DESC, created_at DESC
        """
    ).fetchall())


def _breadcrumb(conn: Connection, folder_id: str) -> list[dict[str, str]]:
    crumbs = [{"id": "", "name": "数据库"}]
    chain = []
    current = folder_id
    seen = set()
    while current and current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT folder_id, name, parent_id FROM library_folder WHERE folder_id = %s",
            (current,),
        ).fetchone()
        if row is None:
            break
        chain.append({"id": row["folder_id"], "name": row["name"]})
        current = str(row["parent_id"] or "")
    crumbs.extend(reversed(chain))
    return crumbs


def _require_folder(conn: Connection, folder_id: str) -> None:
    found = conn.execute("SELECT 1 FROM library_folder WHERE folder_id = %s", (folder_id,)).fetchone()
    if found is None:
        raise LibraryError("文件夹不存在")


def _require_item(conn: Connection, kind: str, item_id: str, uploads: list[dict[str, str]] | None) -> None:
    if kind == "file":
        found = conn.execute("SELECT 1 FROM doc_info WHERE doc_id = %s", (item_id,)).fetchone()
    elif kind == "code":
        found = conn.execute("SELECT 1 FROM code_version WHERE code_id = %s", (item_id,)).fetchone()
    else:
        known = uploads if uploads is not None else disk_uploads()
        found = next((item for item in known if item.get("id") == item_id), None)
    if not found:
        raise LibraryError("要移动的项目不存在")


def _move_folder(conn: Connection, folder_id: str, parent_id: str) -> None:
    _require_folder(conn, folder_id)
    if folder_id == parent_id:
        raise LibraryError("文件夹不能移进自己")
    current = parent_id
    seen = set()
    while current and current not in seen:
        if current == folder_id:
            raise LibraryError("文件夹不能移进自己的下级")
        seen.add(current)
        row = conn.execute("SELECT parent_id FROM library_folder WHERE folder_id = %s", (current,)).fetchone()
        current = str(row["parent_id"] or "") if row else ""
    conn.execute("UPDATE library_folder SET parent_id = %s WHERE folder_id = %s", (parent_id, folder_id))


def _folder_name(name: str) -> str:
    text = str(name or "").strip()
    if not text or "/" in text or "\\" in text:
        raise LibraryError("文件夹名称不能为空，也不能包含斜杠")
    if len(text) > 80:
        raise LibraryError("文件夹名称不能超过 80 字")
    return text


def _status_text(status: str) -> str:
    return {"pending": "待确认", "ready": "已发布", "failed": "失败", "indexing": "处理中"}.get(status, status)


def _item(kind: str, item_id: str, name: str, detail: str, modified: Any) -> dict[str, str]:
    return {
        "kind": kind,
        "id": str(item_id),
        "name": name,
        "detail": detail,
        "type": _entry_type(kind, name),
        "modified": _iso(modified),
    }


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _entry_type(kind: str, name: str) -> str:
    if kind == "folder":
        return "文件夹"
    if kind == "code":
        return "Python 代码"
    labels = {
        ".csv": "CSV 文件",
        ".xlsx": "Excel 工作表",
        ".xls": "Excel 工作表",
        ".pdf": "PDF 文件",
        ".doc": "Word 文档",
        ".docx": "Word 文档",
        ".txt": "文本文档",
        ".py": "Python 文件",
        ".md": "Markdown 文件",
    }
    suffix = Path(name).suffix.lower()
    if suffix in labels:
        return labels[suffix]
    if suffix:
        return f"{suffix[1:].upper()} 文件"
    return "表格" if kind == "file" else "文件"
