"""打开已经配置好的 PostgreSQL，并保证四张元数据表存在。"""

from __future__ import annotations

from psycopg import Connection

from structured.catalog import prepare
from structured.db import connect, ensure_database


def open_db() -> Connection:
    ensure_database()
    conn = connect()
    prepare(conn)
    return conn
