"""现有入库和问答的 FastAPI 入口。路径和返回字段与原来的网站一致。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from http import HTTPStatus
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

import web_server
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
app = FastAPI(title="掌柜智库", docs_url=None, redoc_url=None)

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
        answer = await asyncio.to_thread(
            get_chat_service().send,
            user_id,
            conversation_id,
            content,
            item_name_user_selection=payload.get("item_name_user_selection"),
            item_name_user_confirmed=payload.get("item_name_user_confirmed"),
        )
        return _json({"message": answer}, HTTPStatus.CREATED, new_user_id=new_user_id)
    except Exception as exc:
        return _chat_failure(exc, new_user_id=new_user_id)


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
