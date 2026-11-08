"""最终回答必须能回到私有库文件或网页链接。"""

from __future__ import annotations

import re


NO_INFO = "当前知识库中未检索到足够信息，建议查看正式产品文件、公告原文或咨询相关工作人员。"
RETRIEVAL_ERROR = "知识库检索失败，不能据此判断库里是否有资料。"
WEB_ERROR = "本次联网搜索暂不可用，仅使用知识库资料。"

_SENTENCE = re.compile(r"[^。！？\n]*[。！？\n]?")


def enforce_sourced_answer(answer: str, chunks: list[dict]) -> str:
    """去掉对不上来源的句子。固定的失败说明和无资料说明保持原样。"""
    text = (answer or "").strip()
    if not text:
        return NO_INFO
    if text.startswith(RETRIEVAL_ERROR) or text.startswith(NO_INFO) or text == WEB_ERROR:
        return text
    kept = [part for part in _SENTENCE.findall(text) if part.strip() and _supported(part, chunks)]
    if not kept:
        return NO_INFO
    return "".join(kept)


def _supported(sentence: str, chunks: list[dict]) -> bool:
    for chunk in chunks:
        url = str(chunk.get("url") or "").strip()
        title = str(chunk.get("title") or "")
        file_name = str(chunk.get("file_name") or "")
        if url and url in sentence:
            return True
        if title and title in sentence:
            return False
        if file_name and file_name in sentence:
            return True
    return False


def annotate_answer(answer: str, chunks: list[dict], download_for=None) -> tuple[str, list[dict]]:
    """给回答标上 [n]，并给出可打开的网页地址或可下载的资料。"""
    citations: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for chunk in chunks:
        url = str(chunk.get("url") or "").strip()
        file_name = str(chunk.get("file_name") or "").strip()
        key = ("url", url) if url else ("file", file_name)
        if not key[1] or key in seen:
            continue
        seen.add(key)
        excerpt = " ".join(str(chunk.get("content") or "").split())[:120]
        item: dict = {"index": len(citations) + 1}
        if excerpt:
            item["excerpt"] = excerpt
        if url:
            item["title"] = str(chunk.get("title") or "网页")
            item["url"] = url
        else:
            item["source_file"] = file_name
            if chunk.get("item_name"):
                item["product_name"] = chunk.get("item_name")
            link = ""
            saved_name = ""
            if download_for is not None:
                found = download_for(file_name) or ""
                if isinstance(found, tuple):
                    link, saved_name = str(found[0] or ""), str(found[1] or "")
                else:
                    link = str(found)
            if link:
                item["download_url"] = link
            if saved_name:
                item["download_name"] = saved_name
        citations.append(item)

    lines = [line for line in (answer or "").splitlines() if not line.strip().startswith("来源：")]
    text = "\n".join(lines).strip()
    if not citations or text.startswith(NO_INFO) or text.startswith(RETRIEVAL_ERROR):
        return text, citations

    sentences = [part for part in _SENTENCE.findall(text) if part] or [text]
    ordered: list[dict] = []
    rebuilt: list[str] = []
    for sentence in sentences:
        marks = []
        for item in citations:
            if item in ordered or not _sentence_cites(sentence, item):
                continue
            ordered.append(item)
            marks.append(len(ordered))
        rebuilt.append(sentence.rstrip() + "".join(f"[{index}]" for index in marks))
    for index, item in enumerate(ordered, start=1):
        item["index"] = index
    return "".join(rebuilt).rstrip(), ordered


def _sentence_cites(sentence: str, item: dict) -> bool:
    url = str(item.get("url") or "")
    file_name = str(item.get("source_file") or "")
    title = str(item.get("title") or "")
    excerpt = str(item.get("excerpt") or "")
    if url and url in sentence:
        return True
    stem = file_name.rsplit(".", 1)[0]
    if stem and stem in sentence:
        return True
    if title and title in sentence:
        return True
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{4,}", excerpt)
    return any(token in sentence for token in tokens)


def citations_for(chunks: list[dict]) -> list[dict]:
    """只输出资料里已经有的字段，网页必须带链接。"""
    citations = []
    for chunk in chunks:
        url = str(chunk.get("url") or "").strip()
        if url:
            item = {"title": chunk.get("title") or "", "url": url}
            if chunk.get("content"):
                item["content"] = chunk.get("content")
            citations.append(item)
            continue
        file_name = str(chunk.get("file_name") or "").strip()
        if not file_name and not chunk.get("item_name"):
            continue
        item = {}
        for key, target in (
            ("file_name", "source_file"),
            ("item_name", "product_name"),
            ("document_title", "document_title"),
            ("content_type", "content_type"),
            ("product_code", "product_code"),
            ("publish_date", "publish_date"),
        ):
            value = chunk.get(key)
            if value:
                item[target] = value
        if item:
            citations.append(item)
    return citations


def compose_sourced_text(chunks: list[dict], gaps: list[str] | None = None, web_error: str = "") -> str:
    """用检索结果本身组织回答，不把没有来源的句子留在正文里。"""
    lines = []
    for chunk in chunks:
        url = str(chunk.get("url") or "").strip()
        content = str(chunk.get("content") or "").strip()
        if url:
            title = str(chunk.get("title") or "网页")
            lines.append(f"{title}：{content} {url}")
        elif chunk.get("file_name"):
            lines.append(f"根据{chunk['file_name']}：{content}")
    body = enforce_sourced_answer("\n".join(lines), chunks) if lines else NO_INFO
    notes = []
    if web_error:
        notes.append(web_error)
    for gap in gaps or []:
        if gap and gap not in notes:
            notes.append(f"未完成：{gap}")
    if notes and body == NO_INFO and not lines:
        return body
    if notes:
        return body + "\n" + "\n".join(notes)
    return body
