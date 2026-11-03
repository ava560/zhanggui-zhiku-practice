"""导入工作流入口：校验文件并按后缀分流到 PDF 或 Markdown 路径。"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from dotenv import load_dotenv

from state import (
    INPUT_DIR,
    OUTPUT_DIR,
    ImportGraphState,
    create_default_state,
    format_state,
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _copy_sidecar_images(src: Path, dest: Path) -> None:
    src_images = src.parent / "images"
    dest_images = dest.parent / "images"
    if src_images.is_dir() and src_images.resolve() != dest_images.resolve():
        shutil.copytree(src_images, dest_images, dirs_exist_ok=True)

    for image in src.parent.iterdir():
        if not image.is_file() or image.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        target = dest.parent / image.name
        if image.resolve() == target.resolve():
            continue
        shutil.copy2(image, target)


def stage_markdown_to_output(src: Path, output_dir: Path) -> Path:
    """把用户上传的 Markdown 和旁边的图片放到 output，后续节点统一从这里读。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / src.name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
        _copy_sidecar_images(src, dest)
        logger.info("已将 Markdown 复制到输出目录: %s", dest.name)
    return dest


def node_entry(state: ImportGraphState) -> ImportGraphState:
    """
    入口节点：只更新状态，不决定下一个节点是谁。
    PDF 打开 is_pdf_read_enabled；MD 打开 is_md_read_enabled；其他格式两个开关都保持关闭。
    """
    logger.info(">>> 开始执行节点：node_entry，当前状态：%s", format_state(state))

    if not state.get("task_id"):
        state["task_id"] = uuid.uuid4().hex[:12]
    if not state.get("local_dir"):
        state["local_dir"] = str(OUTPUT_DIR)

    state["is_pdf_read_enabled"] = False
    state["is_md_read_enabled"] = False
    state["pdf_path"] = ""
    state["md_path"] = ""

    document_path = str(state.get("local_file_path") or "").strip()
    if not document_path:
        logger.error("核心参数缺失：未配置 local_file_path")
        return state

    path = Path(document_path).expanduser()
    suffix = path.suffix.lower()
    state["file_title"] = path.stem
    output_dir = Path(state["local_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    if not path.is_file():
        logger.error("文件不存在或不是文件: %s", path)
        return state

    if suffix == ".pdf":
        state["is_pdf_read_enabled"] = True
        state["pdf_path"] = str(path.resolve())
        logger.info("文件类型为 PDF，开启 PDF 解析: %s", path.name)
    elif suffix == ".md":
        staged = stage_markdown_to_output(path, output_dir)
        state["is_md_read_enabled"] = True
        state["md_path"] = str(staged.resolve())
        state["md_content"] = staged.read_text(encoding="utf-8")
        logger.info("文件类型为 Markdown，跳过 PDF 转换: %s", staged.name)
    elif suffix in {".docx", ".doc"}:
        from upload.word_to_md import stage_word_as_markdown

        staged = stage_word_as_markdown(path, output_dir)
        state["is_md_read_enabled"] = True
        state["md_path"] = str(staged.resolve())
        state["md_content"] = staged.read_text(encoding="utf-8")
        logger.info("文件类型为 Word，已转为 Markdown: %s", staged.name)
    else:
        logger.warning("不支持的文件格式：%s，仅支持 .pdf / .md / .docx / .doc", path.name)

    logger.info(
        "node_entry 完成: file_title=%s pdf=%s md=%s",
        state.get("file_title"),
        state.get("is_pdf_read_enabled"),
        state.get("is_md_read_enabled"),
    )
    logger.info("更新后状态：%s", format_state(state))
    return state


if __name__ == "__main__":
    logger.info("===== 开始 node_entry 单元测试 =====")

    txt_state = create_default_state(
        task_id="test_task_001",
        local_file_path="联想海豚用户手册.txt",
    )
    node_entry(txt_state)
    logger.info(
        "TXT: pdf=%s md=%s",
        txt_state["is_pdf_read_enabled"],
        txt_state["is_md_read_enabled"],
    )

    md_candidates = sorted(OUTPUT_DIR.glob("*.md"))
    md_path = str(md_candidates[0]) if md_candidates else str(INPUT_DIR / "sample.md")
    md_state = create_default_state(task_id="test_task_002", local_file_path=md_path)
    node_entry(md_state)
    logger.info(
        "MD: enabled=%s md_path=%s content_len=%d",
        md_state["is_md_read_enabled"],
        md_state.get("md_path"),
        len(md_state.get("md_content") or ""),
    )

    pdf_candidates = sorted(INPUT_DIR.glob("*.pdf")) if INPUT_DIR.exists() else []
    pdf_path = str(pdf_candidates[0]) if pdf_candidates else "万用表的使用.pdf"
    pdf_state = create_default_state(task_id="test_task_003", local_file_path=pdf_path)
    node_entry(pdf_state)
    logger.info(
        "PDF: enabled=%s pdf_path=%s",
        pdf_state["is_pdf_read_enabled"],
        pdf_state.get("pdf_path"),
    )

    logger.info("===== 结束 node_entry 单元测试 =====")
