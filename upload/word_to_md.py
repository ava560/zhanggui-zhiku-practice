"""把 Word 转成 Markdown，随后走现有的 Markdown 入库路径。"""

from __future__ import annotations

import logging
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

logger = logging.getLogger(__name__)

WD_FORMAT_XML_DOCUMENT = 12


def _cell_text(text: str) -> str:
    return " ".join(text.replace("|", "\\|").split())


def _table_markdown(table: Table) -> str:
    rows = [
        [_cell_text(cell.text) for cell in row.cells]
        for row in table.rows
    ]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    def line(cells: list[str]) -> str:
        padded = cells + [""] * (width - len(cells))
        return "| " + " | ".join(padded) + " |"
    header = line(rows[0])
    separator = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = [line(row) for row in rows[1:]]
    return "\n".join([header, separator, *body])


def docx_to_markdown(path: Path) -> str:
    document = Document(str(path))
    blocks: list[str] = []
    for child in document.element.body:
        if child.tag == qn("w:p"):
            text = Paragraph(child, document).text.strip()
            if text:
                blocks.append(text)
        elif child.tag == qn("w:tbl"):
            rendered = _table_markdown(Table(child, document))
            if rendered:
                blocks.append(rendered)
    return "\n\n".join(blocks).strip() + ("\n" if blocks else "")


def _convert_doc_to_docx(path: Path, dest: Path) -> None:
    try:
        import win32com.client
    except ImportError as exc:
        raise ValueError("缺少 pywin32，无法把旧版 .doc 转成 .docx。") from exc
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    document = None
    try:
        document = word.Documents.Open(str(path.resolve()), False, True)
        document.SaveAs2(str(dest.resolve()), FileFormat=WD_FORMAT_XML_DOCUMENT)
    except Exception as exc:
        raise ValueError(
            "本机 Word 未能转换这份 .doc。请安装 Microsoft Word，或另存为 .docx 后再上传。"
        ) from exc
    finally:
        if document is not None:
            document.Close(False)
        word.Quit()


def stage_word_as_markdown(src: Path, output_dir: Path) -> Path:
    """把 .docx 或 .doc 写成同名 Markdown，供后续节点读取。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower()
    docx_path = src
    temporary = None
    if suffix == ".doc":
        temporary = output_dir / f"{src.stem}.converted.docx"
        _convert_doc_to_docx(src, temporary)
        docx_path = temporary
    elif suffix != ".docx":
        raise ValueError(f"不是 Word 文件: {src.name}")
    markdown = docx_to_markdown(docx_path)
    if not markdown.strip():
        raise ValueError(f"Word 文件没有可入库的正文: {src.name}")
    dest = output_dir / f"{src.stem}.md"
    dest.write_text(markdown, encoding="utf-8")
    if temporary is not None:
        temporary.unlink(missing_ok=True)
    logger.info("已将 Word 转为 Markdown: %s", dest.name)
    return dest
