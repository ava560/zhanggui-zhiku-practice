"""遍历 output 中的 Markdown，为本地图片生成中文摘要并上传 MinIO，另存为 *_new.md。"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from minio import Minio
from minio.deleteobjects import DeleteObject

from state import ImportGraphState, format_state

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"

load_dotenv(BASE_DIR / ".env")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
VISION_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
MD_IMAGE_RE = re.compile(r"!\[(.*?)\]\((.*?)\)")
HTML_IMAGE_RE = re.compile(
    r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\'][^>]*>',
    re.IGNORECASE,
)
CONTEXT_LEN = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_SIGNED_URL_RE = re.compile(r"(https?://[^\s'\"<>]+)\?[^\s'\"<>]*", re.IGNORECASE)
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


def sanitize_log_text(text: str) -> str:
    text = _BEARER_RE.sub(r"\1[redacted]", text)
    return _SIGNED_URL_RE.sub(r"\1?[redacted]", text)


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str
    deepseek_base_url: str
    vl_model: str
    requests_per_minute: int
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_img_dir: str
    minio_secure: bool


def load_settings() -> Settings:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "启动失败：未找到 DEEPSEEK_API_KEY。请在项目根目录的 .env 中设置后再运行。"
        )

    endpoint = os.getenv("MINIO_ENDPOINT", "").strip()
    access_key = os.getenv("MINIO_ACCESS_KEY", "").strip()
    secret_key = os.getenv("MINIO_SECRET_KEY", "").strip()
    bucket = os.getenv("MINIO_BUCKET_NAME", "").strip()
    missing = [
        name
        for name, value in (
            ("MINIO_ENDPOINT", endpoint),
            ("MINIO_ACCESS_KEY", access_key),
            ("MINIO_SECRET_KEY", secret_key),
            ("MINIO_BUCKET_NAME", bucket),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"启动失败：缺少 MinIO 配置 {', '.join(missing)}。")

    rpm = os.getenv("VL_REQUESTS_PER_MINUTE", "20").strip()
    try:
        requests_per_minute = max(1, int(rpm))
    except ValueError:
        requests_per_minute = 20

    return Settings(
        deepseek_api_key=api_key,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        vl_model=os.getenv("VL_MODEL", "deepseek-v4-flash-vision-exp").strip()
        or "deepseek-v4-flash-vision-exp",
        requests_per_minute=requests_per_minute,
        minio_endpoint=endpoint,
        minio_access_key=access_key,
        minio_secret_key=secret_key,
        minio_bucket=bucket,
        minio_img_dir=os.getenv("MINIO_IMG_DIR", "upload-images").strip().strip("/"),
        minio_secure=os.getenv("MINIO_SECURE", "False").strip().lower() in {"1", "true", "yes"},
    )


@dataclass
class ImageRef:
    filename: str
    local_path: Path
    pre_text: str
    post_text: str


def is_remote_src(src: str) -> bool:
    value = src.strip().lower()
    return value.startswith(("http://", "https://", "data:", "//"))


def iter_local_src(md_content: str) -> Iterator[tuple[int, int, str]]:
    for match in MD_IMAGE_RE.finditer(md_content):
        src = match.group(2).strip()
        if src and not is_remote_src(src):
            yield match.start(), match.end(), src
    for match in HTML_IMAGE_RE.finditer(md_content):
        src = match.group(1).strip()
        if src and not is_remote_src(src):
            yield match.start(), match.end(), src


def _is_within(parent: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def resolve_local_image(md_path: Path, src: str) -> Path | None:
    clean = src.strip().split("?")[0].split("#")[0].replace("\\", "/")
    if not clean:
        return None
    candidate = (md_path.parent / clean).resolve()
    allowed_roots = [md_path.parent.resolve(), OUTPUT_DIR.resolve()]
    if not any(_is_within(root, candidate) for root in allowed_roots):
        logger.warning("跳过越界图片路径: %s", md_path.name)
        return None
    if not candidate.is_file():
        return None
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    return candidate


def collect_image_refs(md_path: Path, md_content: str) -> list[ImageRef]:
    seen: set[Path] = set()
    refs: list[ImageRef] = []
    for start, end, src in iter_local_src(md_content):
        local_path = resolve_local_image(md_path, src)
        if local_path is None:
            logger.warning("本地图片不存在或格式不支持，跳过: %s", src)
            continue
        if local_path in seen:
            continue
        seen.add(local_path)
        refs.append(
            ImageRef(
                filename=local_path.name,
                local_path=local_path,
                pre_text=md_content[max(0, start - CONTEXT_LEN) : start],
                post_text=md_content[end : min(len(md_content), end + CONTEXT_LEN)],
            )
        )
    logger.info("%s 共找到 %d 张本地图片引用", md_path.name, len(refs))
    return refs


def apply_rate_limit(request_times: deque[float], requests_per_minute: int) -> None:
    window = 60.0
    now = time.monotonic()
    while request_times and now - request_times[0] >= window:
        request_times.popleft()
    if len(request_times) >= requests_per_minute:
        sleep_for = window - (now - request_times[0]) + 0.05
        if sleep_for > 0:
            time.sleep(sleep_for)
        now = time.monotonic()
        while request_times and now - request_times[0] >= window:
            request_times.popleft()
    request_times.append(time.monotonic())


def clean_summary(text: str) -> str:
    summary = text.strip().replace("\n", "").replace("\r", "")
    summary = re.sub(r"[\[\]\(\)]", "", summary)
    summary = summary.strip(" \"'")
    return summary[:50] or "图片描述"


def summarize_image(settings: Settings, ref: ImageRef, doc_stem: str) -> str:
    mime = VISION_MIME.get(ref.local_path.suffix.lower(), "image/jpeg")
    image_b64 = base64.b64encode(ref.local_path.read_bytes()).decode("utf-8")
    prompt = (
        f"这是“{doc_stem}”文件中的一张图片，图片上文部分为“{ref.pre_text}”，"
        f"下文部分为“{ref.post_text}”，请用中文简要总结这张图片的内容，"
        "用于 Markdown 图片标题，控制在50字以内。"
    )
    payload = {
        "model": settings.vl_model,
        "temperature": 0.1,
        "max_tokens": 200,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
    }
    response = requests.post(
        f"{settings.deepseek_base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return clean_summary(str(content or ""))


def get_minio_client(settings: Settings) -> Minio:
    client = Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)
        logger.info("已创建 MinIO 存储桶")
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{settings.minio_bucket}/*"],
            }
        ],
    }
    try:
        client.set_bucket_policy(settings.minio_bucket, json.dumps(policy))
    except Exception as exc:
        logger.warning("设置 MinIO 桶策略失败: %s", type(exc).__name__)
    return client


def minio_object_prefix(settings: Settings, doc_stem: str) -> str:
    return f"{settings.minio_img_dir}/{doc_stem}".replace(" ", "").strip("/")


def clean_minio_prefix(client: Minio, settings: Settings, prefix: str) -> None:
    objects = client.list_objects(settings.minio_bucket, prefix=prefix, recursive=True)
    delete_list = [DeleteObject(obj.object_name) for obj in objects]
    if not delete_list:
        return
    logger.info("清理 MinIO 旧文件 %d 个，前缀=%s", len(delete_list), prefix)
    for error in client.remove_objects(settings.minio_bucket, delete_list):
        logger.error("MinIO 删除失败: %s", sanitize_log_text(str(error)))


def upload_to_minio(client: Minio, settings: Settings, local_path: Path, object_name: str) -> str | None:
    suffix = local_path.suffix.lower().lstrip(".") or "jpeg"
    content_type = f"image/{'jpeg' if suffix == 'jpg' else suffix}"
    try:
        client.fput_object(
            bucket_name=settings.minio_bucket,
            object_name=object_name,
            file_path=str(local_path),
            content_type=content_type,
        )
    except Exception as exc:
        logger.error("上传 MinIO 失败: %s (%s)", local_path.name, type(exc).__name__)
        return None

    protocol = "https" if settings.minio_secure else "http"
    quoted = quote(object_name, safe="/")
    return f"{protocol}://{settings.minio_endpoint}/{settings.minio_bucket}/{quoted}"


def replace_image_refs(md_content: str, filename: str, summary: str, url: str) -> str:
    md_pattern = re.compile(
        r"!\[.*?\]\(.*?" + re.escape(filename) + r".*?\)",
        re.IGNORECASE,
    )
    md_content = md_pattern.sub(lambda _m, s=summary, u=url: f"![{s}]({u})", md_content)

    html_pattern = re.compile(
        r"<img\b[^>]*?\bsrc\s*=\s*[\"'][^\"']*?"
        + re.escape(filename)
        + r"[^\"']*[\"'][^>]*>",
        re.IGNORECASE,
    )
    escaped_alt = html.escape(summary, quote=True)
    md_content = html_pattern.sub(
        lambda _m, a=escaped_alt, u=url: f'<img src="{u}" alt="{a}"/>',
        md_content,
    )
    return md_content


def process_markdown(
    md_path: Path,
    settings: Settings,
    minio_client: Minio,
    md_content: str | None = None,
) -> tuple[Path, str]:
    if md_content is None:
        md_content = md_path.read_text(encoding="utf-8")
    refs = collect_image_refs(md_path, md_content)
    new_content = md_content
    if not refs:
        new_path = md_path.with_name(f"{md_path.stem}_new.md")
        new_path.write_text(new_content, encoding="utf-8")
        logger.info("无本地图片引用，已另存: %s", new_path.name)
        return new_path, new_content

    prefix = minio_object_prefix(settings, md_path.stem)
    clean_minio_prefix(minio_client, settings, prefix)

    request_times: deque[float] = deque()
    replaced = 0
    for index, ref in enumerate(refs, start=1):
        logger.info("处理图片 %d/%d: %s", index, len(refs), ref.filename)
        summary = "图片描述"
        try:
            apply_rate_limit(request_times, settings.requests_per_minute)
            summary = summarize_image(settings, ref, md_path.stem)
            logger.info("摘要生成成功: %s -> %s", ref.filename, summary)
        except Exception:
            logger.error(
                "摘要生成失败，继续处理: %s\n%s",
                ref.filename,
                sanitize_log_text(traceback.format_exc()),
            )

        object_name = f"{prefix}/{ref.filename}"
        url = upload_to_minio(minio_client, settings, ref.local_path, object_name)
        if not url:
            logger.error("图片上传失败，保留原引用: %s", ref.filename)
            continue

        new_content = replace_image_refs(new_content, ref.filename, summary, url)
        replaced += 1

    new_path = md_path.with_name(f"{md_path.stem}_new.md")
    new_path.write_text(new_content, encoding="utf-8")
    logger.info("已保存 %s，成功替换 %d/%d 张图片", new_path.name, replaced, len(refs))
    return new_path, new_content


def node_md_to_img(state: ImportGraphState) -> ImportGraphState:
    """处理当前 Markdown 中的本地图片，并把 *_new.md 路径和内容写回状态。"""
    logger.info(">>> 开始执行节点：node_md_to_img，当前状态：%s", format_state(state))
    md_path = Path(str(state.get("md_path") or "")).expanduser()
    if not md_path.is_file():
        raise ValueError(f"Markdown 不存在或不是文件: {md_path}")

    try:
        settings = load_settings()
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc

    minio_client = get_minio_client(settings)
    md_content = state.get("md_content") or None
    new_path, new_content = process_markdown(md_path, settings, minio_client, md_content)
    state["md_path"] = str(new_path.resolve())
    state["md_content"] = new_content
    logger.info("图片处理完成，新文件: %s ，内容长度=%d", new_path.name, len(new_content))
    logger.info("更新后状态：%s", format_state(state))
    return state


def list_source_markdown(output_dir: Path) -> list[Path]:
    files = sorted(
        path
        for path in output_dir.rglob("*.md")
        if path.is_file() and not path.name.endswith("_new.md")
    )
    logger.info("在 %s 中找到 %d 个 Markdown 文件", output_dir, len(files))
    return files


def main() -> None:
    settings = load_settings()
    if not OUTPUT_DIR.exists():
        raise SystemExit(f"启动失败：未找到 output 目录: {OUTPUT_DIR}")

    md_files = list_source_markdown(OUTPUT_DIR)
    if not md_files:
        logger.warning("output 目录中没有待处理的 Markdown 文件")
        return

    minio_client = get_minio_client(settings)
    failed: list[str] = []
    for md_path in md_files:
        try:
            process_markdown(md_path, settings, minio_client)
        except Exception:
            logger.error("处理 Markdown 失败: %s\n%s", md_path, sanitize_log_text(traceback.format_exc()))
            failed.append(str(md_path))

    if failed:
        raise SystemExit(f"有 {len(failed)} 个 Markdown 处理失败: {failed}")
    logger.info("全部处理完成，共 %d 个文件", len(md_files))


if __name__ == "__main__":
    main()
