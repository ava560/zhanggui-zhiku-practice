"""将 input 目录中的 PDF 通过 MinerU API（vlm）转换为 Markdown，并保存到 output。"""

from __future__ import annotations

import io
import logging
import os
import re
import time
import traceback
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from state import ImportGraphState, format_state

BASE_URL = "https://mineru.net/api/v4"
MODEL_VERSION = "vlm"
POLL_INTERVAL_SECONDS = 1
MAX_POLL_SECONDS = 600
UPLOAD_RETRY_TIMES = 2
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

# MinerU API、OSS 和 CDN 都直连，不读取系统代理（HTTP_PROXY / HTTPS_PROXY）。
# 当前开发机可能设置不可用的占位代理，因此不能只让 OSS 绕过代理。
NO_PROXY = {"http": None, "https": None}

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_SIGNED_URL_RE = re.compile(r"(https?://[^\s'\"<>]+)\?[^\s'\"<>]*", re.IGNORECASE)
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


class MinerUUploadError(RuntimeError):
    """MinerU 临时 OSS 地址无法完成上传。"""


def sanitize_log_text(text: str) -> str:
    text = _BEARER_RE.sub(r"\1[redacted]", text)
    return _SIGNED_URL_RE.sub(r"\1?[redacted]", text)


def require_mineru_token() -> str:
    token = os.getenv("MINERU_API_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "启动失败：未找到 MINERU_TOKEN。请在项目根目录的 .env 文件中设置 MINERU_TOKEN 后再运行。"
        )
    return token


def _build_direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=(502, 503, 504),
        # 仅对幂等下载/轮询做底层重试。OSS PUT 使用单独会话显式重试，
        # 避免 urllib3 重试与业务重试叠加，也避免把签名 URL 写进日志。
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


direct_session = _build_direct_session()


def auth_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {require_mineru_token()}",
    }


def list_pdf_files(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        raise FileNotFoundError(f"未找到输入目录: {input_dir}")
    pdf_files = sorted(
        path for path in input_dir.rglob("*.pdf") if path.is_file()
    )
    logger.info("在 %s 中找到 %d 个 PDF 文件", input_dir, len(pdf_files))
    return pdf_files


def output_md_path(pdf_path: Path, input_dir: Path, output_dir: Path) -> Path:
    relative = pdf_path.relative_to(input_dir)
    return output_dir / relative.with_suffix(".md")


def apply_upload_url(pdf_path: Path) -> tuple[str, str]:
    payload = {
        "files": [{"name": pdf_path.name, "data_id": pdf_path.stem[:128]}],
        "model_version": MODEL_VERSION,
    }
    logger.info("申请上传地址: %s（模型=%s）", pdf_path.name, MODEL_VERSION)
    response = direct_session.post(
        f"{BASE_URL}/file-urls/batch",
        headers=auth_headers(),
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("code") != 0:
        raise RuntimeError(f"申请上传地址失败: {result.get('msg') or ('code=' + str(result.get('code')))}")

    data = result["data"]
    batch_id = data["batch_id"]
    file_urls = data["file_urls"]
    if not file_urls:
        raise RuntimeError("未返回文件上传地址")
    logger.info("获得 batch_id=%s", batch_id)
    return batch_id, file_urls[0]


def upload_pdf(upload_url: str, pdf_path: Path) -> None:
    logger.info("直连上传 PDF（绕过系统代理）: %s", pdf_path)
    file_bytes = pdf_path.read_bytes()
    last_error: Exception | None = None

    # 与原 PythonProject16 中已验证的实现保持一致：OSS 上传使用全新的
    # Session，明确忽略系统代理。第一次保持 MinerU 示例的无 Content-Type
    # PUT；若远端拒绝或断开，第二次显式声明 PDF 类型。
    upload_session = requests.Session()
    upload_session.trust_env = False
    attempt_headers = ({}, {"Content-Type": "application/pdf"})
    try:
        for attempt, headers in enumerate(attempt_headers, start=1):
            try:
                response = upload_session.put(
                    upload_url,
                    data=file_bytes,
                    headers=headers,
                    timeout=(30, 120),
                )
                if response.status_code == 200:
                    logger.info("上传完成: %s", pdf_path.name)
                    return
                last_error = RuntimeError(f"上传失败, status={response.status_code}")
                logger.warning(
                    "第 %s/%s 次上传返回异常: status=%s",
                    attempt,
                    UPLOAD_RETRY_TIMES,
                    response.status_code,
                )
            except (
                requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                last_error = exc
                logger.warning(
                    "第 %s/%s 次上传网络失败: %s",
                    attempt,
                    UPLOAD_RETRY_TIMES,
                    type(exc).__name__,
                )

            if attempt < UPLOAD_RETRY_TIMES:
                time.sleep(1)
    finally:
        upload_session.close()

    raise MinerUUploadError(f"PDF 上传失败（已重试 {UPLOAD_RETRY_TIMES} 次）") from last_error


def poll_download_url(batch_id: str, pdf_name: str) -> str:
    poll_url = f"{BASE_URL}/extract-results/batch/{batch_id}"
    deadline = time.monotonic() + MAX_POLL_SECONDS
    elapsed = 0

    logger.info("开始轮询解析结果，间隔 %s 秒，最长 %s 秒", POLL_INTERVAL_SECONDS, MAX_POLL_SECONDS)
    while True:
        response = direct_session.get(poll_url, headers=auth_headers(), timeout=60)
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(f"查询任务失败: {result.get('msg') or ('code=' + str(result.get('code')))}")

        extract_result = result["data"]["extract_result"]
        if isinstance(extract_result, dict):
            extract_result = [extract_result]

        item = next(
            (row for row in extract_result if row.get("file_name") == pdf_name),
            extract_result[0] if extract_result else None,
        )
        if item is None:
            raise RuntimeError(f"轮询结果中未找到文件: {pdf_name}")

        state = item.get("state")
        progress = item.get("extract_progress") or {}
        if progress:
            logger.info(
                "解析中: %s, state=%s, pages=%s/%s, elapsed=%ss",
                pdf_name,
                state,
                progress.get("extracted_pages"),
                progress.get("total_pages"),
                elapsed,
            )
        else:
            logger.info("解析中: %s, state=%s, elapsed=%ss", pdf_name, state, elapsed)

        if state == "done":
            zip_url = item.get("full_zip_url")
            if not zip_url:
                raise RuntimeError("任务已完成但未返回下载地址")
            logger.info("已获得下载地址")
            return zip_url

        if state == "failed":
            raise RuntimeError(f"解析失败: {item.get('err_msg') or '未知错误'}")

        if time.monotonic() >= deadline:
            raise TimeoutError(f"轮询超过 {MAX_POLL_SECONDS} 秒仍未完成: {pdf_name}")

        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS


def _normalize_zip_name(name: str) -> str:
    return name.replace("\\", "/")


def _zip_path_is_absolute(name: str) -> bool:
    normalized = _normalize_zip_name(name)
    if normalized.startswith("/") or normalized.startswith("\\"):
        return True
    if len(normalized) >= 2 and normalized[1] == ":":
        return True
    return Path(normalized).is_absolute()


def _zip_path_has_parent_ref(name: str) -> bool:
    parts = _normalize_zip_name(name).split("/")
    return ".." in parts


def _is_unsafe_zip_path(name: str) -> bool:
    return _zip_path_is_absolute(name) or _zip_path_has_parent_ref(name)


def _is_within_parent(parent: Path, target: Path) -> bool:
    parent_resolved = parent.resolve()
    try:
        target.resolve().relative_to(parent_resolved)
    except ValueError:
        return False
    return True


def _image_destination(md_parent: Path, zip_name: str) -> Path | None:
    if _is_unsafe_zip_path(zip_name):
        logger.warning("跳过不安全的 ZIP 路径")
        return None

    parts = [part for part in _normalize_zip_name(zip_name).split("/") if part and part != "."]
    if not parts:
        return None

    images_index = next((i for i, part in enumerate(parts) if part.lower() == "images"), None)
    if images_index is None:
        return None

    relative_parts = parts[images_index + 1 :]
    if not relative_parts:
        return None
    if any(part == ".." for part in relative_parts):
        logger.warning("跳过不安全的 ZIP 路径")
        return None

    suffix = Path(relative_parts[-1]).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        return None

    destination = md_parent.joinpath("images", *relative_parts)
    if not _is_within_parent(md_parent, destination):
        logger.warning("跳过越界的图片路径")
        return None
    return destination


def extract_markdown(archive: zipfile.ZipFile) -> str:
    md_names = [
        name
        for name in archive.namelist()
        if name.lower().endswith(".md") and not name.endswith("/")
    ]
    if not md_names:
        raise RuntimeError("压缩包中未找到 Markdown 文件")

    preferred = next(
        (name for name in md_names if Path(_normalize_zip_name(name)).name.lower() == "full.md"),
        md_names[0],
    )
    logger.info("从压缩包提取 Markdown")
    return archive.read(preferred).decode("utf-8")


def extract_images(archive: zipfile.ZipFile, md_path: Path) -> int:
    md_parent = md_path.parent
    saved = 0
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        destination = _image_destination(md_parent, name)
        if destination is None:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(name))
        saved += 1
    logger.info("已保存 %d 张图片到 %s", saved, md_parent / "images")
    return saved


def download_markdown(zip_url: str, md_path: Path) -> None:
    logger.info("直连下载结果（绕过系统代理）")
    response = direct_session.get(zip_url, timeout=300, proxies=NO_PROXY)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        markdown = extract_markdown(archive)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        extract_images(archive, md_path)

    logger.info("已写入: %s", md_path)
    print("下载成功")


def convert_pdf_locally(pdf_path: Path, md_path: Path) -> None:
    """在 MinerU 的 OSS 通道不可用时，用 PyMuPDF 提取可搜索文本。"""
    try:
        import fitz
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "MinerU OSS 上传不可用，且未安装本地降级依赖 PyMuPDF"
        ) from exc

    logger.warning("MinerU OSS 不可达，改用本地 PyMuPDF 解析: %s", pdf_path.name)
    sections = [f"# {pdf_path.stem}"]
    with fitz.open(pdf_path) as document:
        for page_number, page in enumerate(document, start=1):
            page_text = page.get_text("text").replace("\x00", "").strip()
            if page_text:
                sections.append(f"## 第 {page_number} 页\n\n{page_text}")

    markdown = "\n\n".join(sections).strip() + "\n"
    if len(markdown) <= len(sections[0]) + 2:
        raise RuntimeError("本地 PDF 解析未提取到文本，无法继续向量化")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    logger.info("本地 PDF 解析完成: %s（%d 字符）", md_path.name, len(markdown))


def convert_pdf(pdf_path: Path, md_path: Path) -> None:
    logger.info("开始转换: %s -> %s", pdf_path, md_path)
    try:
        batch_id, upload_url = apply_upload_url(pdf_path)
        upload_pdf(upload_url, pdf_path)
        zip_url = poll_download_url(batch_id, pdf_path.name)
        download_markdown(zip_url, md_path)
    except (MinerUUploadError, requests.exceptions.RequestException) as exc:
        logger.warning("MinerU 网络通道失败（%s），启动本地解析", type(exc).__name__)
        convert_pdf_locally(pdf_path, md_path)


def node_pdf_to_md(state: ImportGraphState) -> ImportGraphState:
    """将 state 中的单个 PDF 转为 Markdown，并回写 md_path / md_content。"""
    logger.info(">>> 开始执行节点：node_pdf_to_md，当前状态：%s", format_state(state))
    pdf_path = Path(str(state.get("pdf_path") or "")).expanduser()
    if not pdf_path.is_file():
        raise ValueError(f"PDF 不存在或不是文件: {pdf_path}")

    output_dir = Path(state.get("local_dir") or OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"{pdf_path.stem}.md"
    convert_pdf(pdf_path, md_path)

    state["md_path"] = str(md_path.resolve())
    state["md_content"] = md_path.read_text(encoding="utf-8")
    logger.info(
        "PDF 已转为 Markdown: %s ，内容长度=%d",
        md_path.name,
        len(state["md_content"]),
    )
    logger.info("更新后状态：%s", format_state(state))
    return state


def main() -> None:
    require_mineru_token()
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pdf_files = list_pdf_files(INPUT_DIR)
    if not pdf_files:
        logger.warning("input 目录中没有 PDF 文件")
        return

    failed: list[str] = []
    for pdf_path in pdf_files:
        md_path = output_md_path(pdf_path, INPUT_DIR, OUTPUT_DIR)
        try:
            convert_pdf(pdf_path, md_path)
        except Exception:
            logger.error("转换失败: %s\n%s", pdf_path, sanitize_log_text(traceback.format_exc()))
            failed.append(str(pdf_path))

    if failed:
        raise SystemExit(f"有 {len(failed)} 个文件转换失败: {failed}")

    logger.info("全部转换完成，共 %d 个文件", len(pdf_files))


if __name__ == "__main__":
    main()
