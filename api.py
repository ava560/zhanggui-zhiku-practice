"""现有入库和问答的 FastAPI 入口。路径和返回字段与原来的网站一致。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

import web_server
from search.long_term_memory import get_memory_book
from web_server import (
    MAX_UPLOAD_BYTES,
    PROJECT_ROOT,
    STAGES,
    TASK_ROOT,
    WEB_ROOT,
    get_chat_service,
    process_task,
    safe_filename,
    task_executor,
    task_store,
    utc_now,
)

load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)


def _job_loop(stop: threading.Event) -> None:
    while not stop.wait(60):
        try:
            from structured.http_api import run_due_jobs

            run_due_jobs()
        except Exception:
            logger.exception("定时任务检查失败")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    stop = threading.Event()
    thread = threading.Thread(target=_job_loop, args=(stop,), name="data-jobs", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="掌柜智库", docs_url=None, redoc_url=None, lifespan=_lifespan)

STATIC_FILES = {
    "/styles.css": (WEB_ROOT / "styles.css", "text/css; charset=utf-8"),
    "/app.js": (WEB_ROOT / "app.js", "text/javascript; charset=utf-8"),
    "/chat.css": (WEB_ROOT / "chat.css", "text/css; charset=utf-8"),
    "/chat.js": (WEB_ROOT / "chat.js", "text/javascript; charset=utf-8"),
}


def _json(payload: object, status: int = 200, *, new_user_id: str | None = None) -> JSONResponse:
    response = JSONResponse(payload, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    if new_user_id:
        response.set_cookie(
            "knowledge_user",
            new_user_id,
            max_age=31536000,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


def _chat_identity(request: Request) -> tuple[str, str | None]:
    value = request.cookies.get("knowledge_user", "")
    if re.fullmatch(r"[0-9a-f]{32}", value or ""):
        return value, None
    user_id = uuid.uuid4().hex
    return user_id, user_id


def _chat_failure(exc: Exception, *, new_user_id: str | None = None) -> JSONResponse:
    from chat_service import AnswerError

    if isinstance(exc, LookupError):
        return _json({"error": str(exc)}, HTTPStatus.NOT_FOUND, new_user_id=new_user_id)
    if isinstance(exc, ValueError):
        return _json({"error": str(exc)}, HTTPStatus.BAD_REQUEST, new_user_id=new_user_id)
    if isinstance(exc, AnswerError):
        logger.exception("对话检索链失败")
        return _json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY, new_user_id=new_user_id)
    logger.exception("聊天接口失败")
    return _json(
        {"error": "聊天服务暂不可用，请检查 MongoDB 配置与连接"},
        HTTPStatus.SERVICE_UNAVAILABLE,
        new_user_id=new_user_id,
    )


def _read_json(body: bytes) -> dict:
    if not body or len(body) > 16384:
        raise ValueError("请求内容为空或过长")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("请求 JSON 无效") from exc
    if not isinstance(payload, dict):
        raise ValueError("请求内容必须是 JSON 对象")
    return payload


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html", media_type="text/html; charset=utf-8")


@app.get("/chat")
def chat_page() -> FileResponse:
    return FileResponse(WEB_ROOT / "chat.html", media_type="text/html; charset=utf-8")


@app.get("/styles.css")
@app.get("/app.js")
@app.get("/chat.css")
@app.get("/chat.js")
def static_file(request: Request) -> Response:
    path, media_type = STATIC_FILES[request.url.path]
    if not path.is_file():
        return Response(status_code=HTTPStatus.NOT_FOUND)
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health() -> JSONResponse:
    return _json({"status": "ok", "time": utc_now(), "python_executable": sys.executable})


@app.get("/api/config")
def config() -> JSONResponse:
    return _json(
        {
            "allowed_extensions": sorted(web_server.ALLOWED_EXTENSIONS),
            "table_extensions": [".csv", ".xls", ".xlsm", ".xlsx"],
            "max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024,
            "stages": STAGES,
        }
    )


@app.get("/api/tasks")
def list_tasks() -> JSONResponse:
    return _json({"tasks": task_store.list()})


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> JSONResponse:
    item = task_store.get(task_id)
    if item is None:
        return _json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
    return _json(item)


@app.post("/api/tasks", status_code=HTTPStatus.ACCEPTED)
async def create_task(request: Request, filename: str = "") -> JSONResponse:
    safe_name = safe_filename(filename)
    suffix = Path(safe_name).suffix.lower()
    if not safe_name or suffix not in web_server.ALLOWED_EXTENSIONS:
        return _json({"error": "仅支持上传 PDF、Markdown 或 Word 文件"}, HTTPStatus.BAD_REQUEST)
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length <= 0:
        return _json({"error": "上传内容为空"}, HTTPStatus.BAD_REQUEST)
    if content_length > MAX_UPLOAD_BYTES:
        return _json(
            {"error": f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB 限制"},
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        )
    task_id = uuid.uuid4().hex[:12]
    task_dir = TASK_ROOT / task_id
    source_dir = task_dir / "source"
    work_dir = task_dir / "artifacts"
    source_dir.mkdir(parents=True, exist_ok=False)
    work_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / safe_name
    written = 0
    with source_path.open("wb") as handle:
        async for chunk in request.stream():
            handle.write(chunk)
            written += len(chunk)
    if written != content_length:
        source_path.unlink(missing_ok=True)
        return _json({"error": "文件上传不完整，请重试"}, HTTPStatus.BAD_REQUEST)
    item = task_store.create(task_id, safe_name, content_length)
    task_executor.submit(process_task, task_id, source_path, work_dir)
    return _json(item, HTTPStatus.ACCEPTED)


@app.get("/api/conversations")
def list_conversations(request: Request) -> JSONResponse:
    user_id, new_user_id = _chat_identity(request)
    try:
        return _json({"conversations": get_chat_service().list(user_id)}, new_user_id=new_user_id)
    except Exception as exc:
        return _chat_failure(exc, new_user_id=new_user_id)


@app.post("/api/conversations", status_code=HTTPStatus.CREATED)
def create_conversation(request: Request) -> JSONResponse:
    user_id, new_user_id = _chat_identity(request)
    try:
        return _json(get_chat_service().create(user_id), HTTPStatus.CREATED, new_user_id=new_user_id)
    except Exception as exc:
        return _chat_failure(exc, new_user_id=new_user_id)


@app.get("/api/sources/download")
def download_source(name: str) -> Response:
    from search.source_files import find_imported_file

    path = find_imported_file(name)
    if path is None:
        return _json({"error": "没有找到可下载的资料"}, HTTPStatus.NOT_FOUND)
    return FileResponse(path, filename=path.name, content_disposition_type="attachment")


@app.get("/api/memory")
def list_memory(request: Request) -> JSONResponse:
    user_id, new_user_id = _chat_identity(request)
    return _json({"items": get_memory_book().export(user_id)}, new_user_id=new_user_id)


@app.post("/api/memory", status_code=HTTPStatus.CREATED)
async def save_memory(request: Request) -> JSONResponse:
    user_id, new_user_id = _chat_identity(request)
    try:
        payload = _read_json(await request.body())
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("text 必须是文本")
        item = get_memory_book().add(user_id, text, "settings")
        return _json(item, HTTPStatus.CREATED, new_user_id=new_user_id)
    except Exception as exc:
        return _chat_failure(exc, new_user_id=new_user_id)


@app.delete("/api/memory")
async def delete_memory(request: Request) -> JSONResponse:
    user_id, new_user_id = _chat_identity(request)
    try:
        payload = _read_json(await request.body())
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("text 必须是文本")
        removed = get_memory_book().delete(user_id, text)
        return _json({"removed": removed}, new_user_id=new_user_id)
    except Exception as exc:
        return _chat_failure(exc, new_user_id=new_user_id)


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request) -> JSONResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", conversation_id):
        return _json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
    user_id, new_user_id = _chat_identity(request)
    try:
        get_chat_service().delete(user_id, conversation_id)
        return _json({"removed": True}, new_user_id=new_user_id)
    except Exception as exc:
        return _chat_failure(exc, new_user_id=new_user_id)


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, request: Request) -> JSONResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", conversation_id):
        return _json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
    user_id, new_user_id = _chat_identity(request)
    try:
        return _json(get_chat_service().get(user_id, conversation_id), new_user_id=new_user_id)
    except Exception as exc:
        return _chat_failure(exc, new_user_id=new_user_id)


@app.post("/api/conversations/{conversation_id}/messages", status_code=HTTPStatus.CREATED)
async def send_message(conversation_id: str, request: Request) -> JSONResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", conversation_id):
        return _json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
    user_id, new_user_id = _chat_identity(request)
    try:
        payload = _read_json(await request.body())
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("content 必须是文本")
        from structured.session import open_db
        from structured.jobs import skill_on_named_job
        from structured.skills import bge_skill_score, resolve_turn

        job_skill_id = ""
        try:
            conn = open_db()
            try:
                job_skill_id = skill_on_named_job(conn, content)
            finally:
                conn.close()
        except Exception:
            job_skill_id = ""
        resolved = resolve_turn(
            content,
            skill_id=str(payload.get("skill_id") or ""),
            job_skill_id=job_skill_id,
            scorer=bge_skill_score,
        )
        if resolved["stop"]:
            raise ValueError(str(resolved["message"]))
        answer = await asyncio.to_thread(
            get_chat_service().send,
            user_id,
            conversation_id,
            content,
            item_name_user_selection=payload.get("item_name_user_selection"),
            item_name_user_confirmed=payload.get("item_name_user_confirmed"),
            answer_mode=_answer_mode(payload),
            use_web_search=_use_web_search(payload),
            skill_instructions=str(resolved["instructions"] or ""),
            skill_id=str(resolved["skill_id"] or ""),
        )
        return _json({"message": answer}, HTTPStatus.CREATED, new_user_id=new_user_id)
    except Exception as exc:
        return _chat_failure(exc, new_user_id=new_user_id)


def _answer_mode(payload: dict) -> str:
    mode = payload.get("mode") or "auto"
    if mode not in {"auto", "fast", "deep"}:
        raise ValueError("mode 只能是 auto、fast 或 deep")
    return mode


def _use_web_search(payload: dict) -> bool:
    if "web_search" not in payload:
        return True
    value = payload.get("web_search")
    if not isinstance(value, bool):
        raise ValueError("web_search 必须是布尔值")
    return value


def _sse(event: dict) -> str:
    return f"event: {event.get('event') or 'message'}\ndata: {json.dumps(event.get('data') or {}, ensure_ascii=False)}\n\n"


@app.post("/api/conversations/{conversation_id}/messages/stream")
async def stream_message(conversation_id: str, request: Request) -> Response:
    if not re.fullmatch(r"[0-9a-f]{32}", conversation_id):
        return _json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
    user_id, new_user_id = _chat_identity(request)
    try:
        payload = _read_json(await request.body())
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("content 必须是文本")
        from structured.session import open_db
        from structured.jobs import skill_on_named_job
        from structured.skills import bge_skill_score, resolve_turn

        job_skill_id = ""
        try:
            conn = open_db()
            try:
                job_skill_id = skill_on_named_job(conn, content)
            finally:
                conn.close()
        except Exception:
            job_skill_id = ""
        resolved = resolve_turn(
            content,
            skill_id=str(payload.get("skill_id") or ""),
            job_skill_id=job_skill_id,
            scorer=bge_skill_score,
        )
        if resolved["stop"]:
            raise ValueError(str(resolved["message"]))
        skill_instructions = str(resolved["instructions"] or "")
        skill_id = str(resolved["skill_id"] or "")
        mode = _answer_mode(payload)
        use_web = _use_web_search(payload)
    except Exception as exc:
        return _chat_failure(exc, new_user_id=new_user_id)

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def produce() -> None:
        def report(text: str) -> None:
            asyncio.run_coroutine_threadsafe(
                queue.put({"event": "status", "data": {"text": text}}),
                loop,
            ).result()

        try:
            for event in get_chat_service().iter_events(
                user_id,
                conversation_id,
                content,
                item_name_user_selection=payload.get("item_name_user_selection"),
                item_name_user_confirmed=payload.get("item_name_user_confirmed"),
                answer_mode=mode,
                use_web_search=use_web,
                skill_instructions=skill_instructions,
                skill_id=skill_id,
                on_status=report,
            ):
                asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
        except Exception as exc:
            from chat_service import AnswerError

            if isinstance(exc, AnswerError):
                message = str(exc)
            elif isinstance(exc, (LookupError, ValueError)):
                message = str(exc)
            else:
                logger.exception("流式回答失败")
                message = "聊天服务暂不可用，请检查 MongoDB 配置与连接"
            asyncio.run_coroutine_threadsafe(
                queue.put({"event": "error", "data": {"message": message}}),
                loop,
            ).result()
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

    producer = asyncio.create_task(asyncio.to_thread(produce))

    async def generate():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield _sse(event)
        finally:
            await producer

    response = StreamingResponse(generate(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Accel-Buffering"] = "no"
    if new_user_id:
        response.set_cookie(
            "knowledge_user",
            new_user_id,
            max_age=31536000,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


def _service_error(exc: Exception) -> JSONResponse:
    from structured.catalog import CatalogError
    from structured.db import DatabaseConfigError
    from structured.jobs import JobError
    from structured.library import LibraryError

    if isinstance(exc, (CatalogError, JobError, LibraryError, ValueError)):
        return _json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
    if isinstance(exc, DatabaseConfigError):
        return _json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
    logger.exception("结构化数据接口失败")
    return _json({"error": "结构化数据查询失败，不能据此判断表里是否有记录。"}, HTTPStatus.BAD_GATEWAY)


@app.get("/api/library")
def library_list(folder_id: str = "") -> JSONResponse:
    from structured.http_api import library_view

    try:
        return _json(library_view(folder_id))
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/library/folders", status_code=HTTPStatus.CREATED)
async def library_folder(request: Request) -> JSONResponse:
    from structured.http_api import library_make_folder

    try:
        payload = _read_json(await request.body())
        return _json(library_make_folder(str(payload.get("name") or ""), str(payload.get("parent_id") or "")))
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/library/folders/rename")
async def library_folder_rename(request: Request) -> JSONResponse:
    from structured.http_api import library_rename_folder

    try:
        payload = _read_json(await request.body())
        return _json(library_rename_folder(str(payload.get("folder_id") or ""), str(payload.get("name") or "")))
    except Exception as exc:
        return _service_error(exc)


@app.get("/api/tables")
def tables_list() -> JSONResponse:
    from structured.http_api import data_tables

    try:
        return _json(data_tables())
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/library/delete")
async def library_delete(request: Request) -> JSONResponse:
    from structured.http_api import library_remove_item

    try:
        payload = _read_json(await request.body())
        return _json(library_remove_item(str(payload.get("kind") or ""), str(payload.get("id") or "")))
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/library/move")
async def library_move(request: Request) -> JSONResponse:
    from structured.http_api import library_move_item

    try:
        payload = _read_json(await request.body())
        return _json(library_move_item(
            str(payload.get("kind") or ""),
            str(payload.get("id") or ""),
            str(payload.get("folder_id") or ""),
        ))
    except Exception as exc:
        return _service_error(exc)


@app.get("/api/data-lineage")
def data_lineage_list() -> JSONResponse:
    from structured.http_api import lineage_board

    result = lineage_board()
    status = HTTPStatus.BAD_GATEWAY if result["message"] == "血缘读取失败" else HTTPStatus.OK
    return _json(result, status)


@app.get("/api/table-imports")
def table_imports() -> JSONResponse:
    from structured.http_api import published_tables

    try:
        return _json(published_tables())
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/table-imports", status_code=HTTPStatus.ACCEPTED)
async def stage_table_import(request: Request, filename: str = "", sheet_name: str = "") -> JSONResponse:
    from structured.http_api import stage_upload
    from structured.sheets import is_table_file

    safe_name = safe_filename(filename)
    if not safe_name or not is_table_file(safe_name):
        return _json({"error": "表格文件只接受 xlsx、xlsm、xls 或 csv"}, HTTPStatus.BAD_REQUEST)
    data = await request.body()
    if not data:
        return _json({"error": "上传内容为空"}, HTTPStatus.BAD_REQUEST)
    if len(data) > MAX_UPLOAD_BYTES:
        return _json({"error": "文件超过大小限制"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    try:
        return _json(stage_upload(safe_name, data, sheet_name or None), HTTPStatus.ACCEPTED)
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/table-imports/{import_version_id}/confirm")
async def confirm_table_import(import_version_id: str, request: Request) -> JSONResponse:
    from structured.http_api import publish_upload

    if not re.fullmatch(r"iv_[0-9a-f]{32}", import_version_id):
        return _json({"error": "导入版本不存在"}, HTTPStatus.NOT_FOUND)
    try:
        payload = json.loads(await request.body() or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return _json(publish_upload(import_version_id, payload))
    except Exception as exc:
        return _service_error(exc)


@app.get("/api/data-jobs")
def data_jobs() -> JSONResponse:
    from structured.http_api import jobs_list

    try:
        return _json(jobs_list())
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/data-jobs", status_code=HTTPStatus.CREATED)
async def create_data_job(request: Request) -> JSONResponse:
    from structured.http_api import jobs_save

    try:
        payload = _read_json(await request.body())
        return _json(jobs_save(payload), HTTPStatus.CREATED)
    except Exception as exc:
        return _service_error(exc)


@app.get("/api/data-jobs/{job_id}")
def data_job_detail(job_id: str) -> JSONResponse:
    from structured.http_api import jobs_preview

    try:
        return _json(jobs_preview(job_id))
    except Exception as exc:
        return _service_error(exc)


@app.patch("/api/data-jobs/{job_id}")
async def patch_data_job(job_id: str, request: Request) -> JSONResponse:
    from structured.http_api import jobs_update

    try:
        payload = _read_json(await request.body())
        return _json(jobs_update(job_id, payload))
    except Exception as exc:
        return _service_error(exc)


@app.get("/api/data-lineage/{data_version_id}")
def data_lineage(data_version_id: str) -> JSONResponse:
    from structured.lineage import lookup_lineage

    result = lookup_lineage(data_version_id)
    status = HTTPStatus.BAD_GATEWAY if result["message"] == "血缘读取失败" else HTTPStatus.OK
    return _json(result, status)


@app.post("/api/data-jobs/{job_id}/run")
def run_data_job(job_id: str) -> JSONResponse:
    from structured.http_api import jobs_run

    try:
        return _json(jobs_run(job_id))
    except Exception as exc:
        return _service_error(exc)


@app.get("/api/skills")
def skills() -> JSONResponse:
    from structured.skills import list_skills

    try:
        return _json({"skills": list_skills()})
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/skills")
async def create_skill(request: Request) -> JSONResponse:
    from structured.session import open_db
    from structured.skills import save_skill

    try:
        payload = json.loads(await request.body() or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        conn = open_db()
        try:
            saved = save_skill(
                conn,
                name=str(payload.get("name") or ""),
                instructions=str(payload.get("instructions") or ""),
                summary=str(payload.get("summary") or ""),
                code_id=str(payload.get("code_id") or ""),
            )
        finally:
            conn.close()
        return _json(saved, HTTPStatus.CREATED)
    except Exception as exc:
        return _service_error(exc)


@app.patch("/api/skills/{skill_id}")
async def revise_skill(skill_id: str, request: Request) -> JSONResponse:
    from structured.session import open_db
    from structured.skills import save_skill

    if not re.fullmatch(r"sk_[0-9a-f]{32}", skill_id):
        return _json({"error": "技能不存在"}, HTTPStatus.NOT_FOUND)
    try:
        payload = json.loads(await request.body() or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        conn = open_db()
        try:
            saved = save_skill(
                conn,
                skill_id=skill_id,
                name=str(payload.get("name") or ""),
                instructions=str(payload.get("instructions") or ""),
                summary=str(payload.get("summary") or ""),
                code_id=str(payload.get("code_id") or ""),
            )
        finally:
            conn.close()
        return _json(saved)
    except Exception as exc:
        return _service_error(exc)


@app.delete("/api/skills/{skill_id}")
def remove_skill(skill_id: str) -> JSONResponse:
    from structured.session import open_db
    from structured.skills import delete_skill

    if not re.fullmatch(r"sk_[0-9a-f]{32}", skill_id):
        return _json({"error": "技能不存在"}, HTTPStatus.NOT_FOUND)
    try:
        conn = open_db()
        try:
            delete_skill(conn, skill_id)
        finally:
            conn.close()
        return _json({"removed": True})
    except Exception as exc:
        return _service_error(exc)


@app.get("/api/code-imports")
def code_imports() -> JSONResponse:
    from structured.http_api import code_catalog

    try:
        return _json(code_catalog())
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/code-imports", status_code=HTTPStatus.ACCEPTED)
async def stage_code_import(request: Request, filename: str = "", code_id: str = "") -> JSONResponse:
    from structured.http_api import stage_code_upload

    safe_name = safe_filename(filename)
    if not safe_name.lower().endswith(".py"):
        return _json({"error": "代码文件只接受 .py"}, HTTPStatus.BAD_REQUEST)
    data = await request.body()
    if not data:
        return _json({"error": "上传内容为空"}, HTTPStatus.BAD_REQUEST)
    if len(data) > MAX_UPLOAD_BYTES:
        return _json({"error": "文件超过大小限制"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    try:
        return _json(stage_code_upload(safe_name, data, code_id or None), HTTPStatus.ACCEPTED)
    except Exception as exc:
        return _service_error(exc)


@app.post("/api/code-imports/{version_id}/confirm")
async def confirm_code_import(version_id: str, request: Request) -> JSONResponse:
    from structured.http_api import confirm_code_upload

    if not re.fullmatch(r"cv_[0-9a-f]{32}", version_id):
        return _json({"error": "代码版本不存在"}, HTTPStatus.NOT_FOUND)
    try:
        payload = json.loads(await request.body() or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return _json(confirm_code_upload(version_id, payload))
    except Exception as exc:
        return _service_error(exc)


@app.delete("/api/data-jobs/{job_id}")
def remove_data_job(job_id: str) -> JSONResponse:
    from structured.http_api import jobs_delete

    try:
        jobs_delete(job_id)
        return _json({"removed": True})
    except Exception as exc:
        return _service_error(exc)


def main() -> None:
    import uvicorn

    if not WEB_ROOT.is_dir():
        raise SystemExit(f"前端目录不存在: {WEB_ROOT}")
    TASK_ROOT.mkdir(parents=True, exist_ok=True)
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
