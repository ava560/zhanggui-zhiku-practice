"""稳定逻辑 ID。名称和 Milvus 自增主键都不是长期身份。"""

from __future__ import annotations

import hashlib


def digest(*parts: str) -> str:
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def short_id(prefix: str, *parts: str, length: int = 16) -> str:
    return f"{prefix}:{digest(*parts)[:length]}"


def gid(kb_id: str, build_id: str, kind: str, logical_id: str) -> str:
    return digest(kb_id, build_id, SCHEMA_MARK, kind, logical_id)[:24]


SCHEMA_MARK = "kg-2026-11-05"
