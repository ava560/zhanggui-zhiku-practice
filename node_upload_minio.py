"""把导入原件保存到 MinIO，并把对象信息写回工作流状态。"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from minio import Minio

from state import ImportGraphState, format_state

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    source_dir: str
    secure: bool


def load_settings() -> Settings:
    values = {
        "MINIO_ENDPOINT": os.getenv("MINIO_ENDPOINT", "").strip(),
        "MINIO_ACCESS_KEY": os.getenv("MINIO_ACCESS_KEY", "").strip(),
        "MINIO_SECRET_KEY": os.getenv("MINIO_SECRET_KEY", "").strip(),
        "MINIO_BUCKET_NAME": os.getenv("MINIO_BUCKET_NAME", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"缺少 MinIO 配置: {', '.join(missing)}")
    return Settings(
        endpoint=values["MINIO_ENDPOINT"],
        access_key=values["MINIO_ACCESS_KEY"],
        secret_key=values["MINIO_SECRET_KEY"],
        bucket=values["MINIO_BUCKET_NAME"],
        source_dir=(
            os.getenv("MINIO_FILE_DIR", "").strip()
            or os.getenv("MINIO_PDF_DIR", "upload-files").strip()
            or "upload-files"
        ).strip("/"),
        secure=os.getenv("MINIO_SECURE", "False").strip().lower()
        in {"1", "true", "yes", "on"},
    )


def _safe_object_filename(filename: str) -> str:
    """保留可读文件名，同时去掉会改变对象层级的字符。"""
    name = Path(filename).name.strip() or "document"
    return re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)


def upload_source_file(state: ImportGraphState) -> tuple[str, str]:
    source = Path(str(state.get("local_file_path") or "")).expanduser()
    if not source.is_file():
        raise ValueError(f"待上传原件不存在: {source.name or '未命名文件'}")

    settings = load_settings()
    client = Minio(
        settings.endpoint,
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        secure=settings.secure,
    )
    if not client.bucket_exists(settings.bucket):
        client.make_bucket(settings.bucket)

    task_id = re.sub(r"[^A-Za-z0-9_-]", "", str(state.get("task_id") or "")) or "unknown"
    object_name = f"{settings.source_dir}/{task_id}/{_safe_object_filename(source.name)}"
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    client.fput_object(
        bucket_name=settings.bucket,
        object_name=object_name,
        file_path=str(source),
        content_type=content_type,
    )

    protocol = "https" if settings.secure else "http"
    public_url = f"{protocol}://{settings.endpoint}/{settings.bucket}/{quote(object_name, safe='/')}"
    return object_name, public_url


def node_upload_minio(state: ImportGraphState) -> ImportGraphState:
    """上传当前导入原件；失败时终止任务，避免出现只有向量、没有原件的记录。"""
    logger.info(">>> 开始执行节点：node_upload_minio，当前状态：%s", format_state(state))
    object_name, public_url = upload_source_file(state)
    state["minio_source_object"] = object_name
    state["minio_source_url"] = public_url
    logger.info("原件已上传 MinIO: %s", object_name)
    return state
