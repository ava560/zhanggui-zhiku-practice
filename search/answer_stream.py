"""按句放出已核对文本。承诺收益和资料中没有的数字不能先展示给用户。"""

from __future__ import annotations

import re
from collections.abc import Iterable


_PROMISE = re.compile(r"一定赚钱|保证收益|保本无风险|稳赚不赔|一定会上涨|现在必须买入")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_BOUNDARY = "不承诺收益，也不提供买入、卖出或持有决定。"
_NUMBER_GAP = "有数字未能在已检索资料中核对，这里不列出。"


def guard_sentence(sentence: str, evidence: str, question: str = "") -> str:
    text = sentence.strip()
    if not text:
        return ""
    if _PROMISE.search(text):
        return _BOUNDARY
    allowed = f"{evidence}\n{question}"
    if evidence and any(number not in allowed for number in _NUMBER.findall(text)):
        return _NUMBER_GAP
    return text


def iter_released(pieces: Iterable[str], *, evidence: str, question: str) -> Iterable[str]:
    buffer = ""
    for piece in pieces:
        buffer += piece
        ready, buffer = _split_ready(buffer, flush=False)
        for sentence in ready:
            guarded = guard_sentence(sentence, evidence, question)
            if guarded:
                yield guarded
    ready, _buffer = _split_ready(buffer, flush=True)
    for sentence in ready:
        guarded = guard_sentence(sentence, evidence, question)
        if guarded:
            yield guarded


def release_sentences(pieces: Iterable[str], *, evidence: str, question: str) -> list[str]:
    return list(iter_released(pieces, evidence=evidence, question=question))


def _split_ready(buffer: str, *, flush: bool) -> tuple[list[str], str]:
    ready: list[str] = []
    rest = buffer
    while True:
        match = re.search(r"[。！？\n]", rest)
        if match is None:
            break
        ready.append(rest[: match.end()])
        rest = rest[match.end() :]
    if flush and rest.strip():
        ready.append(rest)
        rest = ""
    return ready, rest
