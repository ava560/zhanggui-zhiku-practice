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
    run_job,
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
    from structured.skills import list_skills

    conn = open_db()
    try:
        ensure_builtin_codes(conn)
        from structured.code_versions import list_codes

        codes = registered_codes()
        seen = {item["code_id"] for item in codes}
        for item in list_codes(conn):
            if item["status"] == "ready" and item["code_id"] not in seen:
                codes.append({"code_id": item["code_id"], "doc_id": item.get("doc_id") or "", "name": item["name"]})
                seen.add(item["code_id"])
        return {
            "jobs": list_jobs(conn),
            "codes": codes,
            "documents": list_documents(conn),
            "skills": list_skills(conn),
        }
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
            schedule_kind=str(payload.get("schedule_kind") or "daily"),
            schedule_day=payload.get("schedule_day"),
            skill_id=payload.get("skill_id"),
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
            schedule_kind=None if "schedule_kind" not in payload else str(payload.get("schedule_kind") or "daily"),
            schedule_day=payload.get("schedule_day") if "schedule_kind" in payload else None,
            skill_id=None if "skill_id" not in payload else payload.get("skill_id"),
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
        ensure_builtin_codes(conn)
        return preview_job(conn, job_id, REGISTRY, MinioStore())
    finally:
        conn.close()


def run_due_jobs(now=None) -> list[str]:
    """按北京时间检查到点任务。调用方每分钟触发一次。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from structured.code_fix import suggest_code_fix
    from structured.jobs import run_due

    moment = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    conn = open_db()
    try:
        ensure_builtin_codes(conn)
        from structured.lineage import describe_processing

        return run_due(
            conn,
            REGISTRY,
            live_index(),
            moment,
            store=MinioStore(),
            fixer=suggest_code_fix,
            narrator=describe_processing,
        )
    finally:
        conn.close()


def jobs_run(job_id: str) -> dict[str, Any]:
    from structured.code_fix import suggest_code_fix

    conn = open_db()
    try:
        ensure_builtin_codes(conn)
        from structured.lineage import describe_processing

        return run_job(
            conn,
            job_id,
            REGISTRY,
            live_index(),
            store=MinioStore(),
            fixer=suggest_code_fix,
            narrator=describe_processing,
        )
    finally:
        conn.close()


def library_view(folder_id: str = "") -> dict[str, Any]:
    from structured.library import list_library

    conn = open_db()
    try:
        return list_library(conn, folder_id)
    finally:
        conn.close()


def library_make_folder(name: str, parent_id: str = "") -> dict[str, str]:
    from structured.library import create_folder

    conn = open_db()
    try:
        return create_folder(conn, name, parent_id)
    finally:
        conn.close()


def library_rename_folder(folder_id: str, name: str) -> dict[str, str]:
    from structured.library import rename_folder

    conn = open_db()
    try:
        return rename_folder(conn, folder_id, name)
    finally:
        conn.close()


def lineage_board() -> dict[str, Any]:
    from structured.lineage import list_lineage, present_lineage
    from structured.schema import ensure_schema

    listed = list_lineage()
    if listed.get("message"):
        return listed
    conn = open_db()
    try:
        ensure_schema(conn)
        files = {
            row["doc_id"]: row["source_file"]
            for row in conn.execute(
                """
                SELECT DISTINCT ON (doc_id) doc_id, source_file
                FROM doc_info
                WHERE status <> 'archived'
                ORDER BY doc_id, created_at DESC
                """
            ).fetchall()
        }
        jobs = {row["job_id"]: row["name"] for row in conn.execute("SELECT job_id, name FROM data_job").fetchall()}
        codes = {
            row["code_id"]: row["name"]
            for row in conn.execute("SELECT code_id, name FROM code_version WHERE status = 'ready'").fetchall()
        }
        listed["records"] = present_lineage(listed["records"], files=files, jobs=jobs, codes=codes)
        conn.rollback()
        return listed
    finally:
        conn.close()


def data_tables() -> dict[str, Any]:
    from structured.removal import list_tables

    conn = open_db()
    try:
        return list_tables(conn)
    finally:
        conn.close()


def library_remove_item(kind: str, item_id: str) -> dict[str, str]:
    from structured.indexes import live_index
    from structured.removal import delete_milvus_file, delete_minio_basename, remove_library_item

    conn = open_db()
    try:
        return remove_library_item(
            conn,
            kind,
            item_id,
            store=MinioStore(),
            indexes=live_index(),
            milvus_delete=delete_milvus_file,
            delete_basename=delete_minio_basename,
        )
    finally:
        conn.close()


def library_move_item(kind: str, item_id: str, folder_id: str = "") -> dict[str, str]:
    from structured.library import move_item

    conn = open_db()
    try:
        return move_item(conn, kind, item_id, folder_id)
    finally:
        conn.close()


def published_tables() -> dict[str, Any]:
    conn = open_db()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (d.doc_id)
                   d.doc_id, d.source_file, d.format, d.summary, t.table_name, t.summary AS table_summary
            FROM doc_info d
            LEFT JOIN table_info t
              ON t.doc_id = d.doc_id AND t.import_version_id = d.import_version_id
            WHERE d.status = 'ready'
            ORDER BY d.doc_id, t.table_name
            """
        ).fetchall()
        return {"tables": [dict(row) for row in rows]}
    finally:
        conn.close()


def stage_code_upload(filename: str, data: bytes, code_id: str | None = None) -> dict[str, Any]:
    from structured.code_versions import stage_code

    conn = open_db()
    try:
        return stage_code(conn, MinioStore(), filename, data, code_id=code_id or None)
    finally:
        conn.close()


def confirm_code_upload(version_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from structured.code_versions import confirm_code

    conn = open_db()
    try:
        return confirm_code(
            conn,
            version_id,
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            doc_id=str(payload.get("doc_id") or ""),
        )
    finally:
        conn.close()


def code_catalog() -> dict[str, Any]:
    from structured.code_versions import list_codes

    conn = open_db()
    try:
        return {"versions": list_codes(conn)}
    finally:
        conn.close()


def job_or_none(job_id: str) -> dict[str, Any]:
    conn = open_db()
    try:
        return get_job(conn, job_id)
    finally:
        conn.close()
