"""每日任务保存在 data_job。到点按北京时间运行任务自己的代码。"""

from __future__ import annotations

import hashlib
import inspect
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from psycopg import Connection

from structured.catalog import replace_rows, sample_rows

CodeFunc = Callable[["JobContext"], None]


class JobError(ValueError):
    pass


class JobContext:
    def __init__(
        self,
        conn: Connection,
        indexes: Any,
        doc_id: str,
        *,
        clock: Callable[[], datetime] | None = None,
        deadline: datetime | None = None,
        code_id: str = "",
        job_id: str = "",
        code_version_id: str = "",
        lineage: Any = None,
    ) -> None:
        self.conn = conn
        self.indexes = indexes
        self.doc_id = doc_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.deadline = deadline
        self.code_id = code_id
        self.job_id = job_id
        self.code_version_id = code_version_id
        self.lineage = lineage
        self.reads: list[dict[str, str]] = []
        self.output_version = ""

    def replace_rows(self, rows: list[dict[str, Any]], *, doc_id: str | None = None) -> dict[str, Any]:
        if self.deadline is not None and self.clock() >= self.deadline:
            raise JobError("运行超过 30 分钟")
        target = doc_id or self.doc_id
        if target != self.doc_id:
            raise JobError("更新函数只能写入自己声明的文件")
        inputs, unresolved = _reads_as_inputs(self.conn, self.reads)
        saved = replace_rows(
            self.conn,
            self.indexes,
            target,
            rows,
            lineage=self.lineage,
            code_id=self.code_id,
            job_id=self.job_id,
            code_version_id=self.code_version_id,
            inputs=inputs if self.reads else None,
            unresolved=unresolved if self.reads else None,
        )
        self.output_version = str(saved.get("data_version_id") or "")
        return saved


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
    schedule_kind: str = "daily",
    schedule_day: int | None = None,
    skill_id: str | None = None,
) -> dict[str, Any]:
    from structured.schema import ensure_schema

    if not str(name).strip() or not str(sop_ref).strip():
        raise JobError("任务名称和 SOP 标识不能为空")
    _reject_code_body(code_id)
    clock = _clock(run_at)
    kind, day = _schedule(schedule_kind, schedule_day)
    skill = _skill(skill_id)
    cleaned = _existing_docs(conn, doc_ids)
    ensure_schema(conn)
    job_id = "job_" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO data_job (
            job_id, name, sop_ref, description, code_id, run_at, schedule_kind, schedule_day,
            skill_id, doc_ids, enabled, last_run_at, last_status, last_error, last_success_on
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, 'never', NULL, NULL)
        """,
        (
            job_id,
            name.strip(),
            sop_ref.strip(),
            description.strip(),
            (code_id or "").strip() or None,
            clock,
            kind,
            day,
            skill,
            cleaned,
            enabled,
        ),
    )
    conn.commit()
    return get_job(conn, job_id)


def update_job(
    conn: Connection,
    job_id: str,
    *,
    description: str | None = None,
    run_at: str | None = None,
    schedule_kind: str | None = None,
    schedule_day: int | None = None,
    skill_id: str | None = None,
) -> dict[str, Any]:
    current = get_job(conn, job_id)
    next_description = current["description"] if description is None else description.strip()
    clock = current["run_at"] if run_at is None else _clock(run_at)
    if schedule_kind is None:
        kind, day = current.get("schedule_kind") or "daily", current.get("schedule_day")
    else:
        kind, day = _schedule(schedule_kind, current.get("schedule_day") if schedule_day is None else schedule_day)
    skill = current.get("skill_id") if skill_id is None else _skill(skill_id)
    conn.execute(
        """
        UPDATE data_job
        SET description = %s, run_at = %s, schedule_kind = %s, schedule_day = %s, skill_id = %s
        WHERE job_id = %s
        """,
        (next_description, clock, kind, day, skill, job_id),
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


def preview_job(conn: Connection, job_id: str, codes: dict[str, Any], store: Any = None) -> dict[str, Any]:
    job = get_job(conn, job_id)
    files = []
    for doc_id in job["doc_ids"]:
        try:
            files.append(_plain_sample(sample_rows(conn, doc_id)))
        except Exception as exc:
            files.append({"doc_id": doc_id, "error": str(exc)})
    code_id = _bound_code(job)
    code = codes.get(code_id)
    source = ""
    registered = code is not None
    if code is not None:
        source = inspect.getsource(code["fn"] if isinstance(code, dict) else code)
    elif store is not None and code_id:
        from structured.code_versions import ready_source

        loaded = ready_source(conn, store, code_id)
        if loaded:
            source, _doc_id = loaded
            registered = True
    return {**job, "files": files, "code_preview": source, "code_registered": registered}


def run_job(
    conn: Connection,
    job_id: str,
    codes: dict[str, Any],
    indexes: Any,
    *,
    today: date | None = None,
    store: Any = None,
    fixer: Any = None,
    clock: Callable[[], datetime] | None = None,
    narrator: Any = None,
    lineage: Any = None,
) -> dict[str, Any]:
    from structured.schema import ensure_schema

    ensure_schema(conn)
    job = get_job(conn, job_id)
    clock = clock or (lambda: datetime.now(timezone.utc))
    current_day = today or _beijing(clock()).date()
    if job["last_success_on"] == current_day.isoformat():
        return job
    code_id = _bound_code(job)
    code = codes.get(code_id)
    source_text = ""
    if code is None and store is not None and code_id:
        from structured.code_versions import ready_source

        loaded = ready_source(conn, store, code_id)
        if loaded:
            source_text, uploaded_doc = loaded

            def uploaded(context, script=source_text, label=code_id):
                exec(compile(script, label, "exec"), {"context": context, "replace_rows": context.replace_rows})

            code = {"fn": uploaded, "doc_id": uploaded_doc}
    if not code_id or code is None:
        return job
    fn = code["fn"] if isinstance(code, dict) else code
    doc_id = code.get("doc_id") if isinstance(code, dict) else getattr(fn, "doc_id", "")
    if not doc_id:
        _mark_failed(conn, job_id, "更新函数没有声明文件")
        return get_job(conn, job_id)
    started = clock()
    context = JobContext(
        conn,
        indexes,
        doc_id,
        clock=clock,
        deadline=started + timedelta(minutes=30),
        code_id=code_id,
        job_id=job_id,
        code_version_id=_ready_version_id(conn, code_id),
        lineage=lineage,
    )
    loaded_before = set(sys.modules)
    conn.execute(
        """
        UPDATE data_job
        SET last_status = 'running', last_error = NULL, run_started_at = %s
        WHERE job_id = %s
        """,
        (started, job_id),
    )
    conn.commit()
    try:
        with _traced_reads(context):
            fn(context)
        if clock() >= started + timedelta(minutes=30):
            raise JobError("运行超过 30 分钟")
        if narrator is not None and context.output_version:
            try:
                _fill_description(narrator, context, source_text, loaded_before, lineage)
            except Exception:
                pass
    except Exception as exc:
        conn.rollback()
        message = str(exc) or type(exc).__name__
        if fixer is not None and store is not None:
            origin = source_text
            if not origin:
                try:
                    origin = inspect.getsource(fn)
                except Exception:
                    origin = ""
            suggestion = ""
            if origin:
                try:
                    suggestion = fixer(origin, message) or ""
                except Exception:
                    suggestion = ""
            if suggestion.strip():
                from structured.code_versions import save_pending_fix

                saved = save_pending_fix(conn, store, code_id, suggestion, doc_id=doc_id, name=code_id)
                message += f"\n已生成待确认修改 {saved['version_id']}，没有替换正在使用的版本，也没有再次运行。"
        _mark_failed(conn, job_id, message, started=started, clock=clock)
        return get_job(conn, job_id)
    finished = clock()
    conn.execute(
        """
        UPDATE data_job
        SET last_status = 'succeeded', last_run_at = %s, last_success_on = %s,
            last_error = NULL, last_duration_seconds = %s
        WHERE job_id = %s
        """,
        (finished, current_day, _seconds(started, finished), job_id),
    )
    conn.commit()
    return get_job(conn, job_id)


def skill_on_named_job(conn: Connection, question: str) -> str:
    """问题里只出现一个带技能的任务名时，返回该技能。多个或没有则不附加。"""
    text = str(question or "")
    try:
        jobs = list_jobs(conn)
    finally:
        conn.rollback()
    found: list[str] = []
    for job in jobs:
        name = str(job.get("name") or "")
        skill_id = str(job.get("skill_id") or "")
        if name and skill_id and name in text and skill_id not in found:
            found.append(skill_id)
    return found[0] if len(found) == 1 else ""


def run_due(
    conn: Connection,
    codes: dict[str, Any],
    indexes: Any,
    now: datetime,
    store: Any = None,
    fixer: Any = None,
    narrator: Any = None,
) -> list[str]:
    """确认运行时刻后由调用方每分钟触发。本函数本身不挂到进程上。"""
    ran = []
    for job in list_jobs(conn):
        if not job["enabled"] or not _is_due(job, now):
            continue
        before = job["last_status"]
        after = run_job(
            conn,
            job["job_id"],
            codes,
            indexes,
            today=now.date(),
            store=store,
            fixer=fixer,
            narrator=narrator,
        )
        if after["last_status"] != before or after["last_run_at"] != job["last_run_at"]:
            ran.append(job["job_id"])
    return ran


def _reads_as_inputs(conn: Connection, reads: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """只按读到的文件内容对应已发布版本。同名但内容不同不算对应。"""
    inputs = []
    unresolved = []
    seen: set[str] = set()
    for item in reads:
        path = str(item.get("path") or "")
        name = str(item.get("name") or "") or (Path(path).name if path else "")
        key = path or name
        if not key or key in seen:
            continue
        seen.add(key)
        digest = _file_checksum(path)
        matched = _ready_by_checksum(conn, digest) if digest else []
        if len(matched) != 1:
            unresolved.append({"source_file": name or "未命名文件"})
            continue
        row = matched[0]
        inputs.append({
            "doc_id": row["doc_id"],
            "import_version_id": row["import_version_id"],
            "data_version_id": row["data_version_id"],
            "source_file": row["source_file"],
        })
    return inputs, unresolved


def _file_checksum(path: str) -> str:
    if not path:
        return ""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _ready_by_checksum(conn: Connection, digest: str) -> list[Any]:
    return list(conn.execute(
        """
        SELECT doc_id, import_version_id, data_version_id, source_file
        FROM doc_info
        WHERE checksum = %s AND status = 'ready' AND COALESCE(data_version_id, '') <> ''
        """,
        (digest,),
    ).fetchall())


def _note_read(context: JobContext, target: Any) -> None:
    if isinstance(target, Path):
        path = target
    elif isinstance(target, str):
        path = Path(target)
    elif hasattr(target, "name"):
        path = Path(str(getattr(target, "name")))
    else:
        return
    if path.suffix.lower() not in {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}:
        return
    name = path.name
    recorded = str(path)
    if name and all(item.get("path") != recorded for item in context.reads):
        context.reads.append({"name": name, "path": recorded})


@contextmanager
def _traced_reads(context: JobContext) -> Iterator[None]:
    """记下这次运行实际打开的表格文件。结束时恢复原来的读取函数。"""
    try:
        import pandas as pd
    except Exception:
        yield
        return
    originals = {}

    def wrap(name: str):
        original = getattr(pd, name, None)
        if original is None:
            return

        def wrapped(*args, **kwargs):
            if args:
                _note_read(context, args[0])
            for key in ("filepath_or_buffer", "io"):
                if key in kwargs:
                    _note_read(context, kwargs[key])
            return original(*args, **kwargs)

        originals[name] = original
        setattr(pd, name, wrapped)

    for name in ("read_csv", "read_excel", "read_table"):
        wrap(name)
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(pd, name, original)


def _fill_description(narrator, context: JobContext, source_text: str, loaded_before: set[str], lineage: Any) -> None:
    from structured.lineage import supplement_lineage

    parts = [source_text]
    root = Path(__file__).resolve().parents[1]
    for name in set(sys.modules) - loaded_before:
        module = sys.modules.get(name)
        file = Path(str(getattr(module, "__file__", "") or ""))
        if file.suffix != ".py" or root not in file.parents or "site-packages" in file.parts:
            continue
        try:
            parts.append(file.read_text(encoding="utf-8")[:4000])
        except Exception:
            continue
    output = ""
    try:
        found = context.conn.execute(
            "SELECT source_file FROM doc_info WHERE doc_id = %s AND status = 'ready'",
            (context.doc_id,),
        ).fetchone()
        output = found["source_file"] if found else ""
    except Exception:
        output = ""
    try:
        narration = narrator("\n".join(parts)[:8000], [item["name"] for item in context.reads], output, context.code_id) or {}
    except Exception:
        narration = {}
    if not isinstance(narration, dict):
        narration = {}
    supplement_lineage(context.output_version, narration, lineage)


def _mark_failed(
    conn: Connection,
    job_id: str,
    message: str,
    *,
    started: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    finished = clock() if clock is not None else datetime.now(timezone.utc)
    conn.execute(
        """
        UPDATE data_job
        SET last_status = 'failed', last_run_at = %s, last_error = %s, last_duration_seconds = %s
        WHERE job_id = %s
        """,
        (finished, message[:1200], _seconds(started, finished) if started else None, job_id),
    )
    conn.commit()


def _seconds(started: datetime | None, finished: datetime) -> int:
    if started is None:
        return 0
    return max(0, int((finished - started).total_seconds()))


def _beijing(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now
    return now.astimezone(ZoneInfo("Asia/Shanghai"))


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


def _schedule(kind: str, day: int | None) -> tuple[str, int | None]:
    text = str(kind or "daily").strip() or "daily"
    if text not in {"daily", "weekly", "monthly"}:
        raise JobError("重复周期只能是每天、每周或每月")
    if text == "daily":
        return "daily", None
    try:
        number = int(day)
    except (TypeError, ValueError) as exc:
        raise JobError("每周或每月要选择具体的一天") from exc
    if text == "weekly" and not 1 <= number <= 7:
        raise JobError("每周要选择周一到周日")
    if text == "monthly" and not 1 <= number <= 31:
        raise JobError("每月要选择 1 到 31 号")
    return text, number


def _skill(skill_id: str | None) -> str | None:
    from structured.skills import skill_by_id

    text = str(skill_id or "").strip()
    if not text:
        return None
    if skill_by_id(text) is None:
        raise JobError("技能不存在")
    return text


def _ready_version_id(conn: Connection, code_id: str) -> str:
    if not code_id:
        return ""
    row = conn.execute(
        "SELECT version_id FROM code_version WHERE code_id = %s AND status = 'ready'",
        (code_id,),
    ).fetchone()
    return str(row["version_id"]) if row else ""


def _bound_code(job: dict[str, Any]) -> str:
    return str(job.get("code_id") or "")


def _is_due(job: dict[str, Any], now: datetime) -> bool:
    local = _beijing(now)
    if job["run_at"] != local.strftime("%H:%M"):
        return False
    kind = job.get("schedule_kind") or "daily"
    day = job.get("schedule_day")
    if kind == "weekly":
        return local.isoweekday() == int(day or 0)
    if kind == "monthly":
        return local.day == int(day or 0)
    return True


def _clock(value: str) -> str:
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":")
        if len(hour) > 2 or len(minute) != 2:
            raise ValueError
        parsed = datetime.strptime(f"{int(hour):02d}:{minute}", "%H:%M")
    except ValueError as exc:
        raise JobError("钟点要写成 HH:MM") from exc
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
    started_at = item.get("run_started_at")
    item["run_started_at"] = started_at.isoformat() if hasattr(started_at, "isoformat") else started_at
    item["doc_ids"] = list(item.get("doc_ids") or [])
    return item
