"""每日任务保存在 data_job。钟点可以保存；Q-004 确认前不自动跑。"""

from __future__ import annotations

import inspect
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable

from psycopg import Connection

from structured.catalog import replace_rows, sample_rows

CodeFunc = Callable[["JobContext"], None]


class JobError(ValueError):
    pass


class JobContext:
    def __init__(self, conn: Connection, indexes: Any, doc_id: str) -> None:
        self.conn = conn
        self.indexes = indexes
        self.doc_id = doc_id

    def replace_rows(self, rows: list[dict[str, Any]], *, doc_id: str | None = None) -> dict[str, Any]:
        target = doc_id or self.doc_id
        if target != self.doc_id:
            raise JobError("更新函数只能写入自己声明的文件")
        return replace_rows(self.conn, self.indexes, target, rows)


def list_jobs(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM data_job ORDER BY name, job_id").fetchall()
    return [_public(row) for row in rows]


def list_documents(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (doc_id) doc_id, source_file, format, status
        FROM doc_info
        ORDER BY doc_id, created_at DESC
        """
    ).fetchall()
    return [
        {
            "doc_id": row["doc_id"],
            "source_file": row["source_file"],
            "format": row["format"],
            "status": row["status"],
        }
        for row in rows
    ]


def save_job(
    conn: Connection,
    *,
    name: str,
    sop_ref: str,
    run_at: str,
    doc_ids: list[str],
    description: str = "",
    code_id: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    if not str(name).strip() or not str(sop_ref).strip():
        raise JobError("任务名称和 SOP 标识不能为空")
    _reject_code_body(code_id)
    clock = _clock(run_at)
    cleaned = _existing_docs(conn, doc_ids)
    job_id = "job_" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO data_job (
            job_id, name, sop_ref, description, code_id, run_at, doc_ids, enabled,
            last_run_at, last_status, last_error, last_success_on
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, 'never', NULL, NULL)
        """,
        (job_id, name.strip(), sop_ref.strip(), description.strip(), (code_id or "").strip() or None, clock, cleaned, enabled),
    )
    conn.commit()
    return get_job(conn, job_id)


def update_job(
    conn: Connection,
    job_id: str,
    *,
    description: str | None = None,
    run_at: str | None = None,
) -> dict[str, Any]:
    current = get_job(conn, job_id)
    next_description = current["description"] if description is None else description.strip()
    clock = current["run_at"] if run_at is None else _clock(run_at)
    conn.execute(
        "UPDATE data_job SET description = %s, run_at = %s WHERE job_id = %s",
        (next_description, clock, job_id),
    )
    conn.commit()
    return get_job(conn, job_id)


def delete_job(conn: Connection, job_id: str) -> None:
    deleted = conn.execute("DELETE FROM data_job WHERE job_id = %s", (job_id,)).rowcount
    conn.commit()
    if not deleted:
        raise JobError("任务不存在")


def get_job(conn: Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM data_job WHERE job_id = %s", (job_id,)).fetchone()
    if row is None:
        raise JobError("任务不存在")
    return _public(row)


def preview_job(conn: Connection, job_id: str, codes: dict[str, Any]) -> dict[str, Any]:
    job = get_job(conn, job_id)
    files = []
    for doc_id in job["doc_ids"]:
        try:
            files.append(_plain_sample(sample_rows(conn, doc_id)))
        except Exception as exc:
            files.append({"doc_id": doc_id, "error": str(exc)})
    code = codes.get(job["code_id"] or "")
    source = ""
    if code is not None:
        source = inspect.getsource(code["fn"] if isinstance(code, dict) else code)
    return {**job, "files": files, "code_preview": source, "code_registered": code is not None}


def run_job(
    conn: Connection,
    job_id: str,
    codes: dict[str, Any],
    indexes: Any,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    job = get_job(conn, job_id)
    current_day = today or datetime.now(timezone.utc).date()
    if job["last_success_on"] == current_day.isoformat():
        return job
    code = codes.get(job["code_id"] or "")
    if not job["code_id"] or code is None:
        return job
    fn = code["fn"] if isinstance(code, dict) else code
    doc_id = code.get("doc_id") if isinstance(code, dict) else getattr(fn, "doc_id", "")
    if not doc_id:
        _mark_failed(conn, job_id, "更新函数没有声明文件")
        return get_job(conn, job_id)
    conn.execute(
        "UPDATE data_job SET last_status = 'running', last_error = NULL WHERE job_id = %s",
        (job_id,),
    )
    conn.commit()
    try:
        fn(JobContext(conn, indexes, doc_id))
    except Exception as exc:
        _mark_failed(conn, job_id, str(exc) or type(exc).__name__)
        return get_job(conn, job_id)
    conn.execute(
        """
        UPDATE data_job
        SET last_status = 'succeeded', last_run_at = %s, last_success_on = %s, last_error = NULL
        WHERE job_id = %s
        """,
        (datetime.now(timezone.utc), current_day, job_id),
    )
    conn.commit()
    return get_job(conn, job_id)


def run_due(conn: Connection, codes: dict[str, Any], indexes: Any, now: datetime) -> list[str]:
    """确认运行时刻后由调用方每分钟触发。本函数本身不挂到进程上。"""
    ran = []
    for job in list_jobs(conn):
        if not job["enabled"]:
            continue
        if job["run_at"] != now.strftime("%H:%M"):
            continue
        before = job["last_status"]
        after = run_job(conn, job["job_id"], codes, indexes, today=now.date())
        if after["last_status"] != before or after["last_run_at"] != job["last_run_at"]:
            ran.append(job["job_id"])
    return ran


def _mark_failed(conn: Connection, job_id: str, message: str) -> None:
    conn.execute(
        """
        UPDATE data_job
        SET last_status = 'failed', last_run_at = %s, last_error = %s
        WHERE job_id = %s
        """,
        (datetime.now(timezone.utc), message[:500], job_id),
    )
    conn.commit()


def _existing_docs(conn: Connection, doc_ids: list[str]) -> list[str]:
    cleaned = []
    for doc_id in doc_ids:
        text = str(doc_id).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        return []
    found = {
        row["doc_id"]
        for row in conn.execute("SELECT DISTINCT doc_id FROM doc_info WHERE doc_id = ANY(%s)", (cleaned,)).fetchall()
    }
    missing = [doc_id for doc_id in cleaned if doc_id not in found]
    if missing:
        raise JobError("任务引用了未登记的文件")
    return cleaned


def _clock(value: str) -> str:
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":")
        if len(hour) > 2 or len(minute) != 2:
            raise ValueError
        parsed = datetime.strptime(f"{int(hour):02d}:{minute}", "%H:%M")
    except ValueError as exc:
        raise JobError("每天的钟点要写成 HH:MM") from exc
    return parsed.strftime("%H:%M")


def _reject_code_body(code_id: str | None) -> None:
    text = str(code_id or "")
    if any(token in text for token in ("\n", "\r", "/", "\\", ".py")):
        raise JobError("不能从任务里提交代码正文或路径")


def _plain_sample(sample: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for row in sample.get("rows") or []:
        rows.append({key: _cell(value) for key, value in dict(row).items()})
    return {**sample, "rows": rows}


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    run_at = item.get("run_at")
    item["run_at"] = run_at.strftime("%H:%M") if hasattr(run_at, "strftime") else str(run_at)[:5]
    success = item.get("last_success_on")
    item["last_success_on"] = success.isoformat() if hasattr(success, "isoformat") else success
    ran = item.get("last_run_at")
    item["last_run_at"] = ran.isoformat() if hasattr(ran, "isoformat") else ran
    item["doc_ids"] = list(item.get("doc_ids") or [])
    return item
