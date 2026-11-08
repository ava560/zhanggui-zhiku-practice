"""把连成一段的回答拆成结论和分点，方便直接阅读。"""

from __future__ import annotations

import re

_LEAD = re.compile(r"^先给结论[：:]\s*")
_TRANSITION = re.compile(r"(以下按[^。\n]{0,24}。)")
_SECTION = re.compile(r"(?<!^)(?<!\n)([一二三四五六七八九十]{1,3})、")
_SECTION_TITLE = re.compile(r"(?m)^([一二三四五六七八九十]{1,3})、([^：:\n]{1,20})[：:]\s*")
_SENTENCE = re.compile(r"[^。！？]+[。！？]|[^。！？]+$")
_LEADING_CITE = re.compile(r"^((?:\[\d+\])+)(.*)$", re.DOTALL)


def format_answer_layout(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n").strip()
    if not value:
        return value
    value = _LEAD.sub("结论\n", value)
    value = _TRANSITION.sub(r"\n\n\1\n\n", value)
    value = _SECTION.sub(r"\n\n\1、", value)
    value = _SECTION_TITLE.sub(r"\2\n", value)
    blocks: list[str] = []
    for block in re.split(r"\n{2,}", value):
        block = block.strip()
        if not block:
            continue
        heading, body = _heading(block)
        if heading:
            blocks.append(heading)
        if not body:
            continue
        sentences = _sentences(body)
        blocks.extend(sentences or [body])
    return "\n\n".join(blocks).strip()


def _heading(block: str) -> tuple[str, str]:
    title, _, rest = block.partition("\n")
    title = title.strip()
    if rest and title and len(title) <= 20 and "。" not in title and "！" not in title and "？" not in title:
        return title, rest.strip()
    return "", block


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE.findall(text) if part.strip()]
    merged: list[str] = []
    for part in parts:
        cited = _LEADING_CITE.match(part)
        if cited and merged:
            merged[-1] += cited.group(1)
            rest = cited.group(2).strip()
            if rest:
                merged.append(rest)
            continue
        if merged and len(part) < 8 and part[-1] not in "。！？":
            merged[-1] += part
            continue
        merged.append(part)
    return merged
