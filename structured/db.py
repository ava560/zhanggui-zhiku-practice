"""PostgreSQL 连接只读环境变量，不在代码里写地址、账号或库名。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import psycopg
from psycopg import Connection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row


class DatabaseConfigError(ValueError):
    """缺少连接配置。异常文本只包含变量名。"""


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    database: str
    user: str
    password: str
    url: str = ""

    def conninfo(self, *, database: str | None = None) -> str:
        if self.url and database in {None, self.database}:
            return self.url
        return make_conninfo(
            host=self.host,
            port=self.port,
            dbname=database or self.database,
            user=self.user,
            password=self.password,
        )


def load_settings() -> PostgresSettings:
    url = os.getenv("POSTGRES_URL", "").strip()
    if url:
        database = os.getenv("POSTGRES_DATABASE", "").strip() or _database_from_url(url)
        return PostgresSettings("", 5432, database, "", "", url=url)
    names = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DATABASE", "POSTGRES_USER", "POSTGRES_PASSWORD")
    values = {name: os.getenv(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise DatabaseConfigError("缺少 PostgreSQL 配置: " + ", ".join(missing))
    try:
        port = int(values["POSTGRES_PORT"])
    except ValueError as exc:
        raise DatabaseConfigError("POSTGRES_PORT 不是整数") from exc
    return PostgresSettings(
        values["POSTGRES_HOST"],
        port,
        values["POSTGRES_DATABASE"],
        values["POSTGRES_USER"],
        values["POSTGRES_PASSWORD"],
    )


def _database_from_url(url: str) -> str:
    tail = url.rsplit("/", 1)[-1]
    return tail.split("?", 1)[0]


def connect(*, autocommit: bool = False, database: str | None = None) -> Connection:
    settings = load_settings()
    return psycopg.connect(
        settings.conninfo(database=database),
        autocommit=autocommit,
        row_factory=dict_row,
    )


def ensure_database() -> None:
    """库不存在时创建。已存在则不动里面的表。"""
    settings = load_settings()
    admin = psycopg.connect(settings.conninfo(database="postgres"), autocommit=True, row_factory=dict_row)
    try:
        found = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (settings.database,),
        ).fetchone()
        if found is None:
            admin.execute(
                "CREATE DATABASE " + _quote_database(settings.database),
            )
    finally:
        admin.close()


def _quote_database(name: str) -> str:
    if not name or any(char in name for char in "\"\\;\n\r"):
        raise DatabaseConfigError("POSTGRES_DATABASE 含有不能用于库名的字符")
    return '"' + name.replace('"', '""') + '"'
