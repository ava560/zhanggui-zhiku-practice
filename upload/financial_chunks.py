"""按 FAQ、财报表格、产品和政策文档切分，避免把条件和数字拆开。"""

from __future__ import annotations

import hashlib
import re
from typing import Any


def detect_content_type(text: str, filename: str = "") -> str:
    name = filename or ""
    body = text or ""
    if "用户指南" in name or ("开机" in body and "基金" not in body and "净值" not in body):
        return "generic"
    if body.count("问：") >= 1 and "答：" in body:
        return "faq"
    if any(token in body or token in name for token in ("赎回费率", "招募说明书", "C类份额", "基金合同")):
        return "prospectus"
    if "中国人民银行" in body or ("适用范围" in body and "发布" in body):
        return "policy"
    if any(token in body for token in ("主要财务指标", "营业收入", "净利润")) and (
        "报告" in name or "财务" in body or "|" in body
    ):
        return "financial_report"
    return "generic"


def split_by_document_type(text: str, content_type: str, title: str = "") -> list[dict[str, Any]]:
    if content_type == "faq":
        return _split_faq(text, title)
    if content_type == "financial_report":
        return _split_financial_report(text, title)
    if content_type == "policy":
        return [_policy_chunk(text, title)]
    if content_type == "prospectus":
        return [_section_chunk(text, title, content_type)]
    return [_section_chunk(text, title, content_type or "generic")]


def _split_faq(text: str, title: str) -> list[dict[str, Any]]:
    parts = [part.strip() for part in re.split(r"(?=问：)", text) if part.strip().startswith("问：")]
    chunks = []
    for index, part in enumerate(parts, start=1):
        chunks.append(_base_chunk(part, title, "faq", section=f"问答{index}"))
    return chunks or [_base_chunk(text, title, "faq")]


def _split_financial_report(text: str, title: str) -> list[dict[str, Any]]:
    table = _markdown_table(text)
    if table is None:
        chunk = _base_chunk(text.strip(), title, "financial_report", section=title)
        chunk["parse_quality"] = "degraded"
        chunk["numeric_verified"] = False
        return [chunk]
    parent_id = "table:" + hashlib.sha1(table["header"].encode("utf-8")).hexdigest()[:12]
    unit = table["unit"]
    section = table["section"] or title
    chunks = []
    for row in table["rows"]:
        content = "\n".join(line for line in (f"## {section}" if section else "", unit, table["header"], row) if line)
        chunk = _base_chunk(content, title, "financial_report", section=section)
        chunk["parent_id"] = parent_id
        chunk["parse_quality"] = "ok"
        chunk["numeric_verified"] = True
        chunks.append(chunk)
    return chunks


def _markdown_table(text: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in text.splitlines()]
    unit = next((line for line in lines if line.startswith("单位")), "")
    section = ""
    for line in lines:
        if line.startswith("#"):
            section = line.lstrip("#").strip()
    rows = [line for line in lines if line.startswith("|")]
    if len(rows) < 2:
        return None
    header = " | ".join(cell.strip() for cell in rows[0].strip("|").split("|"))
    data = []
    for line in rows[1:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if set(cells) <= {"", "---", ":---", "---:", ":---:"} or all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        data.append(" | ".join(cells))
    if not data:
        return None
    return {"header": header, "rows": data, "unit": unit, "section": section}


def _policy_chunk(text: str, title: str) -> dict[str, Any]:
    chunk = _base_chunk(text.strip(), title, "policy", section=title or "政策")
    if "中国人民银行" in text:
        chunk["issuer"] = "中国人民银行"
    matched = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    if matched:
        year, month, day = matched.groups()
        chunk["publish_date"] = f"{year}-{int(month):02d}-{int(day):02d}"
    return chunk


def _section_chunk(text: str, title: str, content_type: str) -> dict[str, Any]:
    section = title
    for line in text.splitlines():
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            break
    return _base_chunk(text.strip(), title, content_type, section=section)


def _base_chunk(content: str, title: str, content_type: str, section: str = "") -> dict[str, Any]:
    return {
        "content": content,
        "title": title,
        "section": section,
        "content_type": content_type,
        "parent_id": "",
        "parse_quality": "ok",
        "numeric_verified": content_type != "financial_report",
        "page": None,
        "issuer": "",
        "publish_date": "",
    }
