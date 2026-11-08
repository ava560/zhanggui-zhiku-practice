"""表格导入和每日任务的 HTTP 适配。对话接口不调用运行函数。"""

from __future__ import annotations

from typing import Any

from structured.catalog import confirm_and_publish, stage_table
from structured.indexes import live_index
from structured.job_codes import REGISTRY, ensure_builtin_codes, registered_codes
from structured.jobs import (
    delete_job,
    get_job,
    list_documents,
    list_jobs,
    preview_job,
    save_job,
    update_job,
)
from structured.objects import MinioStore
from structured.session import open_db


def stage_upload(filename: str, data: bytes, sheet_name: str | None) -> dict[str, Any]:
    conn = open_db()
    try:
        result = stage_table(conn, MinioStore(), filename, data, sheet_name=sheet_name or None)
    finally:
        conn.close()
    result["review"] = not result.get("need_sheet") and not result.get("duplicate")
    return result


def publish_upload(import_version_id: str, confirmation: dict[str, Any]) -> dict[str, Any]:
    conn = open_db()
    try:
        return confirm_and_publish(conn, MinioStore(), live_index(), import_version_id, confirmation)
    finally:
        conn.close()


def jobs_list() -> dict[str, Any]:
    conn = open_db()
    try:
        ensure_builtin_codes(conn)
        return {"jobs": list_jobs(conn), "codes": registered_codes(), "documents": list_documents(conn)}
    finally:
        conn.close()


def jobs_save(payload: dict[str, Any]) -> dict[str, Any]:
    if any(key in payload for key in ("code", "command", "script", "source")):
        raise ValueError("不能从界面提交代码正文")
    conn = open_db()
    try:
        return save_job(
            conn,
            name=str(payload.get("name") or ""),
            sop_ref=str(payload.get("sop_ref") or ""),
            run_at=str(payload.get("run_at") or ""),
            doc_ids=list(payload.get("doc_ids") or []),
            description=str(payload.get("description") or ""),
            code_id=payload.get("code_id"),
            enabled=bool(payload.get("enabled", True)),
        )
    finally:
        conn.close()


def jobs_update(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    conn = open_db()
    try:
        return update_job(
            conn,
            job_id,
            description=None if "description" not in payload else str(payload.get("description") or ""),
            run_at=None if "run_at" not in payload else str(payload.get("run_at") or ""),
        )
    finally:
        conn.close()


def jobs_delete(job_id: str) -> None:
    conn = open_db()
    try:
        delete_job(conn, job_id)
    finally:
        conn.close()


def jobs_preview(job_id: str) -> dict[str, Any]:
    conn = open_db()
    try:
        return preview_job(conn, job_id, REGISTRY)
    finally:
        conn.close()


def job_or_none(job_id: str) -> dict[str, Any]:
    conn = open_db()
    try:
        return get_job(conn, job_id)
    finally:
        conn.close()
