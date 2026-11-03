"""灵库资料入库网站：上传文件、排队执行现有流水线并查询进度。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from state import ImportGraphState, create_default_state
from chat_service import AnswerError, create_chat_service

PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"
TASK_ROOT = PROJECT_ROOT / "output" / "tasks"
ALLOWED_EXTENSIONS = {".pdf", ".md", ".docx", ".doc"}
MAX_UPLOAD_BYTES = max(1, int(os.getenv("WEB_MAX_UPLOAD_MB", "200"))) * 1024 * 1024
HOST = os.getenv("WEB_HOST", "127.0.0.1")
PORT = int(os.getenv("WEB_PORT", "8000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

STAGES = [
    {"key": "queued", "label": "等待处理", "progress": 6},
    {"key": "validate", "label": "校验文件", "progress": 12},
    {"key": "minio", "label": "原件存入 MinIO", "progress": 23},
    {"key": "parse", "label": "解析文档", "progress": 39},
    {"key": "images", "label": "处理图片", "progress": 53},
    {"key": "split", "label": "文档切片", "progress": 65},
    {"key": "recognize", "label": "识别商品", "progress": 75},
    {"key": "embedding", "label": "生成向量", "progress": 88},
    {"key": "milvus", "label": "写入 Milvus", "progress": 97},
    {"key": "completed", "label": "入库完成", "progress": 100},
]
STAGE_BY_KEY = {stage["key"]: stage for stage in STAGES}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_filename(filename: str) -> str:
    name = Path(unquote(filename)).name.strip()
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    return name[:180]


def public_error(exc: BaseException) -> str:
    if isinstance(exc, ModuleNotFoundError):
        dependency = str(getattr(exc, "name", "") or "未知组件")
        return f"缺少 Python 依赖 {dependency}，请先安装 requirements.txt 中的依赖。"
    message = str(exc).strip().replace(str(PROJECT_ROOT), "项目目录")
    message = re.sub(r"\s+", " ", message)
    return (message or type(exc).__name__)[:360]


class TaskStore:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, task_id: str, filename: str, size: int) -> dict[str, Any]:
        item = {
            "id": task_id,
            "filename": filename,
            "size": size,
            "status": "queued",
            "stage": "queued",
            "stage_label": STAGE_BY_KEY["queued"]["label"],
            "progress": STAGE_BY_KEY["queued"]["progress"],
            "message": "文件已接收，等待处理",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._items[task_id] = item
        return dict(item)

    def update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            item = self._items[task_id]
            item.update(changes)
            item["updated_at"] = utc_now()
            return dict(item)

    def set_stage(self, task_id: str, key: str, message: str = "") -> dict[str, Any]:
        stage = STAGE_BY_KEY[key]
        changes: dict[str, Any] = {
            "status": "completed" if key == "completed" else "running",
            "stage": key,
            "stage_label": stage["label"],
            "progress": stage["progress"],
        }
        if message:
            changes["message"] = message
        return self.update(task_id, **changes)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(task_id)
            return dict(item) if item else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(item)
                for item in sorted(
                    self._items.values(), key=lambda value: value["created_at"], reverse=True
                )
            ]


task_store = TaskStore()
# 当前处理节点会回写一个兼容旧脚本的全局 chunks.json，故这里刻意单线程执行。
task_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="knowledge-import")
_chat_service = None
_chat_service_lock = threading.Lock()


def get_chat_service():
    global _chat_service
    with _chat_service_lock:
        if _chat_service is None:
            _chat_service = create_chat_service()
        return _chat_service


def _run_node(
    task_id: str,
    state: ImportGraphState,
    stage: str,
    message: str,
    node: Callable[[ImportGraphState], ImportGraphState],
) -> ImportGraphState:
    task_store.set_stage(task_id, stage, message)
    return node(state)


def _result_summary(state: ImportGraphState, started: float) -> dict[str, Any]:
    chunks = list(state.get("chunks") or [])
    dense_vector = chunks[0].get("dense_vector") if chunks else None
    return {
        "item_name": str(state.get("item_name") or ""),
        "chunks_count": len(chunks),
        "vector_dimension": len(dense_vector) if isinstance(dense_vector, list) else 0,
        "minio_object": str(state.get("minio_source_object") or ""),
        "md_file": Path(str(state.get("md_path") or "")).name,
        "chunks_file": Path(str(state.get("split_path") or "")).name,
        "duration_seconds": round(time.monotonic() - started, 1),
        "milvus_written": bool(chunks and all(chunk.get("chunk_id") is not None for chunk in chunks)),
    }


def process_task(task_id: str, source_path: Path, work_dir: Path) -> None:
    started = time.monotonic()
    task_store.update(task_id, status="running", started_at=utc_now(), message="开始处理")
    task_store.set_stage(task_id, "validate", "正在加载并校验处理组件")
    try:
        # 延迟导入，让网页即使在公司外部依赖不可用时也能启动并明确报告任务失败。
        from upload.node_bge_embedding import node_bge_embedding
        from upload.node_docment_split import node_docment_split
        from upload.node_entry import node_entry
        from upload.node_import_milvus import node_import_milvus
        from upload.node_item_name_recognition import node_item_name_recognition
        from upload.node_md_to_img import node_md_to_img
        from upload.node_pdf_to_md import node_pdf_to_md
        from node_upload_minio import node_upload_minio

        state = create_default_state(
            task_id=task_id,
            local_file_path=str(source_path),
            local_dir=str(work_dir),
        )
        state = _run_node(task_id, state, "validate", "正在确认文件类型", node_entry)
        if not state.get("is_pdf_read_enabled") and not state.get("is_md_read_enabled"):
            raise ValueError("文件未通过校验，仅支持 PDF、Markdown 或 Word")

        state = _run_node(task_id, state, "minio", "正在保存原始文件", node_upload_minio)
        if state.get("is_pdf_read_enabled"):
            state = _run_node(task_id, state, "parse", "正在将 PDF 解析为 Markdown", node_pdf_to_md)
        else:
            task_store.set_stage(task_id, "parse", "Markdown 无需 PDF 解析")

        state = _run_node(task_id, state, "images", "正在识别图片并存入 MinIO", node_md_to_img)
        state = _run_node(task_id, state, "split", "正在按语义结构切分文档", node_docment_split)
        state = _run_node(task_id, state, "recognize", "正在识别文档对应商品", node_item_name_recognition)
        state = _run_node(task_id, state, "embedding", "正在生成稠密与稀疏向量", node_bge_embedding)
        state = _run_node(task_id, state, "milvus", "正在写入 Milvus 集合", node_import_milvus)

        result = _result_summary(state, started)
        task_store.set_stage(task_id, "completed", "文件已完成向量化入库")
        task_store.update(task_id, completed_at=utc_now(), result=result)
        logger.info("Web 导入完成: task_id=%s file=%s", task_id, source_path.name)
    except Exception as exc:
        logger.error("Web 导入失败: task_id=%s\n%s", task_id, traceback.format_exc())
        task_store.update(
            task_id,
            status="failed",
            message="处理失败",
            error=public_error(exc),
            completed_at=utc_now(),
        )


class AppHandler(BaseHTTPRequestHandler):
    server_version = "KnowledgeImport/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _json(
        self, payload: Any, status: HTTPStatus = HTTPStatus.OK, *, new_user_id: str | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if new_user_id:
            self.send_header(
                "Set-Cookie",
                f"knowledge_user={new_user_id}; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000",
            )
        self.end_headers()
        self.wfile.write(body)

    def _chat_identity(self) -> tuple[str, str | None]:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie["knowledge_user"].value if "knowledge_user" in cookie else ""
        except Exception:
            value = ""
        if re.fullmatch(r"[0-9a-f]{32}", value):
            return value, None
        user_id = uuid.uuid4().hex
        return user_id, user_id

    def _chat_failure(self, exc: Exception, *, new_user_id: str | None = None) -> None:
        if isinstance(exc, LookupError):
            self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND, new_user_id=new_user_id)
        elif isinstance(exc, ValueError):
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST, new_user_id=new_user_id)
        elif isinstance(exc, AnswerError):
            logger.exception("对话检索链失败")
            self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY, new_user_id=new_user_id)
        else:
            logger.exception("聊天接口失败")
            self._json(
                {"error": "聊天服务暂不可用，请检查 MongoDB 配置与连接"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                new_user_id=new_user_id,
            )

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求长度无效") from exc
        if length <= 0 or length > 16384:
            raise ValueError("请求内容为空或过长")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求 JSON 无效") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/chat":
            self._file(WEB_ROOT / "chat.html", "text/html; charset=utf-8")
            return
        static_files = {
            "/styles.css": (WEB_ROOT / "styles.css", "text/css; charset=utf-8"),
            "/app.js": (WEB_ROOT / "app.js", "text/javascript; charset=utf-8"),
            "/chat.css": (WEB_ROOT / "chat.css", "text/css; charset=utf-8"),
            "/chat.js": (WEB_ROOT / "chat.js", "text/javascript; charset=utf-8"),
        }
        if parsed.path in static_files:
            self._file(*static_files[parsed.path])
            return
        if parsed.path == "/api/conversations":
            user_id, new_user_id = self._chat_identity()
            try:
                self._json(
                    {"conversations": get_chat_service().list(user_id)}, new_user_id=new_user_id
                )
            except Exception as exc:
                self._chat_failure(exc, new_user_id=new_user_id)
            return
        match = re.fullmatch(r"/api/conversations/([0-9a-f]{32})", parsed.path)
        if match:
            user_id, new_user_id = self._chat_identity()
            try:
                self._json(
                    get_chat_service().get(user_id, match.group(1)), new_user_id=new_user_id
                )
            except Exception as exc:
                self._chat_failure(exc, new_user_id=new_user_id)
            return
        if parsed.path == "/api/config":
            self._json(
                {
                    "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
                    "max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024,
                    "stages": STAGES,
                }
            )
            return
        if parsed.path == "/api/tasks":
            self._json({"tasks": task_store.list()})
            return
        match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)", parsed.path)
        if match:
            item = task_store.get(match.group(1))
            if item is None:
                self._json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(item)
            return
        if parsed.path == "/api/health":
            self._json(
                {
                    "status": "ok",
                    "time": utc_now(),
                    "python_executable": sys.executable,
                }
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/conversations":
            user_id, new_user_id = self._chat_identity()
            try:
                self._json(
                    get_chat_service().create(user_id), HTTPStatus.CREATED,
                    new_user_id=new_user_id,
                )
            except Exception as exc:
                self._chat_failure(exc, new_user_id=new_user_id)
            return
        match = re.fullmatch(r"/api/conversations/([0-9a-f]{32})/messages", parsed.path)
        if match:
            user_id, new_user_id = self._chat_identity()
            try:
                payload = self._read_json_body()
                content = payload.get("content")
                if not isinstance(content, str):
                    raise ValueError("content 必须是文本")
                answer = get_chat_service().send(
                    user_id, match.group(1), content,
                    item_name_user_selection=payload.get("item_name_user_selection"),
                    item_name_user_confirmed=payload.get("item_name_user_confirmed"),
                )
                self._json({"message": answer}, HTTPStatus.CREATED, new_user_id=new_user_id)
            except Exception as exc:
                self._chat_failure(exc, new_user_id=new_user_id)
            return
        if parsed.path != "/api/tasks":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(parsed.query)
        filename = safe_filename((query.get("filename") or [""])[0])
        suffix = Path(filename).suffix.lower()
        if not filename or suffix not in ALLOWED_EXTENSIONS:
            self._json({"error": "仅支持上传 PDF、Markdown 或 Word 文件"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self._json({"error": "上传内容为空"}, HTTPStatus.BAD_REQUEST)
            return
        if content_length > MAX_UPLOAD_BYTES:
            self._json(
                {"error": f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB 限制"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return

        task_id = uuid.uuid4().hex[:12]
        task_dir = TASK_ROOT / task_id
        source_dir = task_dir / "source"
        work_dir = task_dir / "artifacts"
        source_dir.mkdir(parents=True, exist_ok=False)
        work_dir.mkdir(parents=True, exist_ok=True)
        source_path = source_dir / filename
        remaining = content_length
        with source_path.open("wb") as file_handle:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                file_handle.write(chunk)
                remaining -= len(chunk)
        if remaining:
            source_path.unlink(missing_ok=True)
            self._json({"error": "文件上传不完整，请重试"}, HTTPStatus.BAD_REQUEST)
            return

        item = task_store.create(task_id, filename, content_length)
        task_executor.submit(process_task, task_id, source_path, work_dir)
        self._json(item, HTTPStatus.ACCEPTED)


def main() -> None:
    if not WEB_ROOT.is_dir():
        raise SystemExit(f"前端目录不存在: {WEB_ROOT}")
    TASK_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    logger.info("知识入库网站已启动: http://%s:%d", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("正在停止网站")
    finally:
        server.server_close()
        task_executor.shutdown(wait=False, cancel_futures=False)


if __name__ == "__main__":
    main()
