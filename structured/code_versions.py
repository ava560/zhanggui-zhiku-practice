"""代码原件放在 MinIO。新版本只把旧版本标成归档，不删除对象。"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection

from structured.jobs import JobError
from structured.schema import ensure_schema


def stage_code(conn: Connection, store: Any, filename: str, data: bytes, *, code_id: str | None = None) -> dict[str, Any]:
    if not filename.lower().endswith(".py") or not data.strip():
        raise JobError("代码文件必须是非空的 .py")
    ensure_schema(conn)
    logical = (code_id or "").strip() or "code_" + uuid.uuid4().hex
    if code_id and not conn.execute("SELECT 1 FROM code_version WHERE code_id = %s", (logical,)).fetchone():
        raise JobError("要更新的代码不存在")
    version_id = "cv_" + uuid.uuid4().hex
    key = store.put(filename, data)
    conn.execute(
        """
        INSERT INTO code_version (version_id, code_id, name, description, doc_id, minio_key, status)
        VALUES (%s, %s, %s, '', NULL, %s, 'pending')
        """,
        (version_id, logical, filename, key),
    )
    conn.commit()
    return {"version_id": version_id, "code_id": logical, "status": "pending", "minio_key": key}


def confirm_code(
    conn: Connection,
    version_id: str,
    *,
    name: str,
    description: str,
    doc_id: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    title = str(name or "").strip()
    if not title or not str(description or "").strip():
        raise JobError("代码名称和说明都要人工确认")
    current = conn.execute("SELECT * FROM code_version WHERE version_id = %s", (version_id,)).fetchone()
    if current is None or current["status"] == "archived":
        raise JobError("代码版本不存在")
    target = (doc_id or "").strip() or None
    if target:
        found = conn.execute("SELECT 1 FROM doc_info WHERE doc_id = %s", (target,)).fetchone()
        if found is None:
            raise JobError("代码要更新的文件还没有登记")
    with conn.transaction():
        conn.execute(
            "UPDATE code_version SET status = 'archived' WHERE code_id = %s AND status = 'ready' AND version_id <> %s",
            (current["code_id"], version_id),
        )
        conn.execute(
            """
            UPDATE code_version
            SET status = 'ready', name = %s, description = %s, doc_id = %s
            WHERE version_id = %s
            """,
            (title, description.strip(), target, version_id),
        )
    conn.commit()
    return _public(conn.execute("SELECT * FROM code_version WHERE version_id = %s", (version_id,)).fetchone())


def list_codes(conn: Connection) -> list[dict[str, Any]]:
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM code_version ORDER BY created_at DESC").fetchall()
    conn.rollback()
    return [_public(row) for row in rows]


def ready_source(conn: Connection, store: Any, code_id: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT minio_key, doc_id FROM code_version WHERE code_id = %s AND status = 'ready'",
        (code_id,),
    ).fetchone()
    if row is None:
        return None
    text = store.get(row["minio_key"]).decode("utf-8")
    return text, str(row["doc_id"] or "")


def save_pending_fix(
    conn: Connection,
    store: Any,
    code_id: str,
    source: str,
    *,
    doc_id: str = "",
    name: str = "",
) -> dict[str, Any]:
    """只新增待确认版本。正在使用的版本和它的对象都保留。"""
    ensure_schema(conn)
    current = conn.execute(
        """
        SELECT name, description, doc_id FROM code_version
        WHERE code_id = %s AND status = 'ready'
        ORDER BY created_at DESC LIMIT 1
        """,
        (code_id,),
    ).fetchone()
    version_id = "cv_" + uuid.uuid4().hex
    key = store.put(f"{code_id}.py", source.encode("utf-8"))
    conn.execute(
        """
        INSERT INTO code_version (version_id, code_id, name, description, doc_id, minio_key, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        """,
        (
            version_id,
            code_id,
            (current["name"] if current else name) or code_id,
            (current["description"] if current else "报错后生成的待确认修改"),
            (current["doc_id"] if current else doc_id) or None,
            key,
        ),
    )
    conn.commit()
    return {"version_id": version_id, "code_id": code_id, "status": "pending", "minio_key": key}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    created = item.get("created_at")
    item["created_at"] = created.isoformat() if hasattr(created, "isoformat") else created
    return item
