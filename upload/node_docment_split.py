"""将 output 中的 *_new.md 按标题切块、合并后输出统一 JSON。"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from state import ImportGraphState, format_state

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
CHUNKS_JSON = OUTPUT_DIR / "chunks.json"

MIN_CHUNK_TOKENS = 1000
MAX_CHUNK_TOKENS = 2000
OVERLAP_TOKENS = 100
SOFT_MAX_TOKENS = 2400
ABSORB_MAX_TOKENS = 2400

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
SECTION_NUM_RE = re.compile(r"^(\d+)\.\d+")
CHAPTER_TITLE_RE = re.compile(r"^(\d+)\s+\S")
TABLE_CAPTION_RE = re.compile(r"^(图|表)\s*\d+")
ATOMIC_RE = re.compile(
    r"(<table\b[\s\S]*?(?:</table>|$))"
    r"|(```[\s\S]*?```)"
    r"|(!\[[^\]]*\]\([^)]+\))"
    r"|(<img\b[^>]*>)",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；;!?\n])")
FRONT_MATTER_TITLES = {
    "前言",
    "前 言",
    "读者对象",
    "本书约定",
    "资料获取方式",
    "技术支持",
    "资料意见反馈",
    "目录",
    "目 录",
}

_tiktoken_encoding = None
_tiktoken_unavailable = False


def count_tokens(text: str) -> int:
    global _tiktoken_encoding, _tiktoken_unavailable
    if not text:
        return 0
    if not _tiktoken_unavailable:
        if _tiktoken_encoding is None:
            try:
                import tiktoken

                _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
                logger.info("使用 tiktoken cl100k_base 统计 token")
            except Exception:
                _tiktoken_unavailable = True
                logger.info("未安装 tiktoken，改用中英混合估算")
        if _tiktoken_encoding is not None:
            return len(_tiktoken_encoding.encode(text, disallowed_special=()))
    return _fallback_count_tokens(text)


def _fallback_count_tokens(text: str) -> int:
    cjk = len(CJK_RE.findall(text))
    rest = CJK_RE.sub("", text)
    rest_chars = len(re.sub(r"\s+", "", rest))
    return max(1, cjk + (rest_chars + 3) // 4) if text.strip() else 0


def normalize_blank_lines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_heading_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?sub>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?sup>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def is_continuation_heading(title: str) -> bool:
    compact = re.sub(r"\s+", "", title)
    if compact in {"说明", "注意", "提示", "注释", "Λ注意"} or compact.startswith("Λ注意"):
        return True
    stripped = title.strip()
    if stripped.startswith(("页面向导", "本页面为您提供")):
        return True
    if stripped.startswith(("•", "·", "-", "—")):
        return True
    if TABLE_CAPTION_RE.match(stripped):
        return True
    return False


def explicit_group(title: str) -> str | None:
    stripped = title.strip()
    compact = re.sub(r"\s+", "", stripped)
    if stripped in FRONT_MATTER_TITLES or compact in {"前言", "目录"}:
        return "front"
    if "您想了解什么" in stripped:
        return "ch1"
    numbered_section = SECTION_NUM_RE.match(stripped)
    if numbered_section:
        return f"ch{numbered_section.group(1)}"
    chapter_title = CHAPTER_TITLE_RE.match(stripped)
    if chapter_title:
        return f"ch{chapter_title.group(1)}"
    if "附录" in stripped:
        return f"appendix:{compact}"
    return None


def annotate_groups(sections: list[Section]) -> list[Section]:
    last_group = "front"
    for section in sections:
        group = explicit_group(section.title)
        if group is None:
            section.group = last_group
        else:
            section.group = group
            last_group = group
    return sections


def extract_doc_title(content: str, path: Path) -> str:
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if match:
        title = clean_heading_text(match.group(1))
        if title:
            return title

    collected: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            title = clean_heading_text(stripped.lstrip("#").strip())
            return title or _title_from_filename(path)
        if stripped.startswith(("Copyright", "未经", "H3C")):
            break
        if len(stripped) > 80:
            break
        collected.append(stripped)
        if len(collected) >= 3 or sum(len(item) for item in collected) >= 50:
            break
    if collected:
        return " ".join(collected)
    return _title_from_filename(path)


def _title_from_filename(path: Path) -> str:
    name = path.name
    if name.endswith("_new.md"):
        name = name[: -len("_new.md")]
    elif name.endswith(".md"):
        name = name[: -len(".md")]
    return name


@dataclass
class Section:
    title: str
    heading_line: str
    body: str
    level: int
    group: str = "front"

    def text(self) -> str:
        parts: list[str] = []
        if self.heading_line:
            parts.append(self.heading_line)
        if self.body.strip():
            parts.append(self.body.strip())
        return normalize_blank_lines("\n\n".join(parts))


@dataclass
class ChunkDraft:
    titles: list[str] = field(default_factory=list)
    content: str = ""
    group: str = "front"

    def token_count(self) -> int:
        return count_tokens(self.content)

    def part_title(self) -> str:
        unique: list[str] = []
        for title in self.titles:
            cleaned = title.strip()
            if cleaned and cleaned not in unique:
                unique.append(cleaned)
        if not unique:
            return "正文"
        if len(unique) == 1:
            return unique[0]
        if len(unique) <= 3:
            return " / ".join(unique)
        return f"{unique[0]} ~ {unique[-1]}"


def parse_heading_sections(content: str) -> list[Section]:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    raw: list[Section] = []
    current = Section(title="文档开头", heading_line="", body="", level=0)

    def flush() -> None:
        nonlocal current
        if current.heading_line or current.body.strip() or current.title:
            current.body = current.body.rstrip()
            raw.append(current)

    for line in lines:
        match = HEADING_RE.match(line)
        if match:
            flush()
            hashes, raw_title = match.group(1), match.group(2)
            title = clean_heading_text(raw_title) or "未命名小节"
            current = Section(
                title=title,
                heading_line=line.rstrip(),
                body="",
                level=len(hashes),
            )
            continue
        if current.body:
            current.body += "\n" + line
        elif line.strip():
            current.body = line
        else:
            current.body += "\n"

    flush()
    return attach_continuation_sections(raw)


def attach_continuation_sections(sections: list[Section]) -> list[Section]:
    merged: list[Section] = []
    for section in sections:
        if merged and is_continuation_heading(section.title):
            extra = section.text()
            if extra:
                prev = merged[-1]
                prev.body = normalize_blank_lines(f"{prev.body}\n\n{extra}")
            continue
        merged.append(section)
    kept = [section for section in merged if section.text()]
    return annotate_groups(kept)


def split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text.strip())
    return [part.strip() for part in parts if part.strip()]


def iter_atomic_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    pos = 0
    for match in ATOMIC_RE.finditer(text):
        if match.start() > pos:
            blocks.extend(split_paragraphs(text[pos : match.start()]))
        block = match.group(0).strip()
        if block:
            blocks.append(block)
        pos = match.end()
    if pos < len(text):
        blocks.extend(split_paragraphs(text[pos:]))
    return blocks


def split_html_table(table: str, max_tokens: int) -> list[str]:
    rows = re.findall(r"<tr[\s\S]*?</tr>", table, flags=re.IGNORECASE)
    if len(rows) <= 1:
        return hard_split_text(table, max_tokens)

    open_tag_match = re.match(r"<table\b[^>]*>", table, flags=re.IGNORECASE)
    open_tag = open_tag_match.group(0) if open_tag_match else "<table>"
    header, data_rows = rows[0], rows[1:]

    def render(group_rows: list[str]) -> str:
        return f"{open_tag}{header}{''.join(group_rows)}</table>"

    groups: list[list[str]] = []
    current: list[str] = []
    for row in data_rows:
        candidate = current + [row]
        if current and count_tokens(render(candidate)) > max_tokens:
            groups.append(current)
            current = [row]
        else:
            current = candidate
    if current:
        groups.append(current)
    if not groups:
        return [table]

    if len(groups) >= 2 and count_tokens(render(groups[-1])) < MIN_CHUNK_TOKENS:
        prev, last = groups[-2], groups[-1]
        while prev and count_tokens(render(last)) < MIN_CHUNK_TOKENS:
            last.insert(0, prev.pop())
        if not prev:
            groups.pop(-2)
        elif count_tokens(render(prev)) < MIN_CHUNK_TOKENS:
            groups[-2] = prev + last
            groups.pop()
        else:
            groups[-2], groups[-1] = prev, last

    pieces = [render(group) for group in groups if group]
    refined: list[str] = []
    for piece in pieces:
        if count_tokens(piece) <= max_tokens:
            refined.append(piece)
        else:
            refined.extend(hard_split_text(piece, max_tokens))
    return refined


def hard_split_text(text: str, max_tokens: int) -> list[str]:
    if count_tokens(text) <= max_tokens:
        return [text]
    sentences = [part for part in SENTENCE_SPLIT_RE.split(text) if part != ""]
    if len(sentences) <= 1:
        return _split_by_char_budget(text, max_tokens)

    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = current + sentence
        if current and count_tokens(candidate) > max_tokens:
            pieces.append(current.strip())
            current = sentence
        else:
            current = candidate
    if current.strip():
        pieces.append(current.strip())

    refined: list[str] = []
    for piece in pieces:
        if count_tokens(piece) <= max_tokens:
            refined.append(piece)
        else:
            refined.extend(_split_by_char_budget(piece, max_tokens))
    return refined


def _split_by_char_budget(text: str, max_tokens: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        lo, hi = start + 1, length
        best = min(start + 1, length)
        while lo <= hi:
            mid = (lo + hi) // 2
            if count_tokens(text[start:mid]) <= max_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if best <= start:
            best = min(start + 1, length)
        pieces.append(text[start:best])
        start = best
    return [piece for piece in pieces if piece]


def split_huge_block(block: str, max_tokens: int) -> list[str]:
    stripped = block.strip()
    if stripped.lower().startswith("<table"):
        return split_html_table(stripped, max_tokens)
    return hard_split_text(stripped, max_tokens)


def pack_blocks(blocks: list[str], max_tokens: int, min_tokens: int) -> list[str]:
    if not blocks:
        return []

    packed: list[str] = []
    current: list[str] = []
    current_tokens = 0

    def join_current() -> str:
        return normalize_blank_lines("\n\n".join(current))

    for block in blocks:
        block_tokens = count_tokens(block)
        if block_tokens > max_tokens:
            if current:
                packed.append(join_current())
                current = []
                current_tokens = 0
            packed.extend(split_huge_block(block, max_tokens))
            continue

        if not current:
            current = [block]
            current_tokens = block_tokens
            continue

        if current_tokens + block_tokens <= max_tokens:
            current.append(block)
            current_tokens += block_tokens
            continue

        if current_tokens < min_tokens and current_tokens + block_tokens <= SOFT_MAX_TOKENS:
            current.append(block)
            packed.append(join_current())
            current = []
            current_tokens = 0
            continue

        packed.append(join_current())
        current = [block]
        current_tokens = block_tokens

    if current:
        packed.append(join_current())

    if len(packed) >= 2 and count_tokens(packed[-1]) < min_tokens:
        combined = normalize_blank_lines(packed[-2] + "\n\n" + packed[-1])
        if count_tokens(combined) <= SOFT_MAX_TOKENS:
            packed[-2] = combined
            packed.pop()
    return packed


def ensure_heading(text: str, heading_line: str, title: str) -> str:
    stripped = text.lstrip()
    if heading_line and stripped.startswith(heading_line):
        return normalize_blank_lines(text)
    prefix = heading_line if heading_line else (f"## {title}" if title else "")
    if not prefix:
        return normalize_blank_lines(text)
    return normalize_blank_lines(f"{prefix}\n\n{stripped}")


def split_oversized_section(section: Section) -> list[ChunkDraft]:
    text = section.text()
    tokens = count_tokens(text)
    if tokens <= MAX_CHUNK_TOKENS:
        return [ChunkDraft(titles=[section.title], content=text, group=section.group)]

    blocks = iter_atomic_blocks(text)
    pieces = pack_blocks(blocks, MAX_CHUNK_TOKENS, MIN_CHUNK_TOKENS)
    drafts: list[ChunkDraft] = []
    for piece in pieces:
        content = ensure_heading(piece, section.heading_line, section.title)
        drafts.append(ChunkDraft(titles=[section.title], content=content, group=section.group))
    return drafts or [ChunkDraft(titles=[section.title], content=text, group=section.group)]


def join_drafts(left: ChunkDraft, right: ChunkDraft) -> ChunkDraft:
    return ChunkDraft(
        titles=left.titles + right.titles,
        content=normalize_blank_lines(left.content + "\n\n" + right.content),
        group=right.group or left.group,
    )


def combined_tokens(left: ChunkDraft, right: ChunkDraft) -> int:
    return count_tokens(left.content + "\n\n" + right.content)


def can_merge(left: ChunkDraft, right: ChunkDraft) -> bool:
    combined = combined_tokens(left, right)
    if combined <= MAX_CHUNK_TOKENS:
        return True
    if left.token_count() < MIN_CHUNK_TOKENS and combined <= SOFT_MAX_TOKENS:
        return True
    return False


def donate_blocks(
    donor: ChunkDraft,
    receiver: ChunkDraft,
    *,
    take_from_end: bool,
) -> tuple[ChunkDraft, ChunkDraft] | None:
    blocks = iter_atomic_blocks(donor.content)
    expanded: list[str] = []
    for block in blocks:
        stripped = block.lstrip().lower()
        if stripped.startswith("<table") and count_tokens(block) > MIN_CHUNK_TOKENS:
            parts = split_html_table(block, MAX_CHUNK_TOKENS)
            expanded.extend(parts or [block])
        else:
            expanded.append(block)
    blocks = expanded
    if len(blocks) < 2:
        return None

    for count in range(1, len(blocks)):
        if take_from_end:
            moved, keep = blocks[-count:], blocks[:-count]
            new_receiver = normalize_blank_lines("\n\n".join(moved) + "\n\n" + receiver.content)
        else:
            moved, keep = blocks[:count], blocks[count:]
            new_receiver = normalize_blank_lines(receiver.content + "\n\n" + "\n\n".join(moved))
        new_donor = normalize_blank_lines("\n\n".join(keep))
        donor_tokens = count_tokens(new_donor)
        receiver_tokens = count_tokens(new_receiver)
        if donor_tokens < MIN_CHUNK_TOKENS:
            break
        if receiver_tokens > ABSORB_MAX_TOKENS:
            continue
        if receiver_tokens >= MIN_CHUNK_TOKENS:
            return (
                ChunkDraft(titles=donor.titles, content=new_donor, group=donor.group),
                ChunkDraft(titles=receiver.titles, content=new_receiver, group=receiver.group),
            )
    return None


def donate_to_undersized(drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    if len(drafts) <= 1:
        return drafts

    result = drafts[:]
    for index, draft in enumerate(result):
        if draft.token_count() >= MIN_CHUNK_TOKENS:
            continue
        if index > 0:
            donated = donate_blocks(result[index - 1], draft, take_from_end=True)
            if donated:
                result[index - 1], result[index] = donated
                continue
        if index + 1 < len(result):
            donated = donate_blocks(result[index + 1], draft, take_from_end=False)
            if donated:
                new_next, new_current = donated
                result[index] = new_current
                result[index + 1] = new_next
    return result


def absorb_undersized(drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    if len(drafts) <= 1:
        return drafts

    merged: list[ChunkDraft] = []
    for draft in drafts:
        if merged and draft.token_count() < MIN_CHUNK_TOKENS:
            combined = count_tokens(merged[-1].content + "\n\n" + draft.content)
            if combined <= ABSORB_MAX_TOKENS:
                merged[-1] = join_drafts(merged[-1], draft)
                continue
        merged.append(draft)

    result: list[ChunkDraft] = []
    index = 0
    while index < len(merged):
        current = merged[index]
        if current.token_count() < MIN_CHUNK_TOKENS and index + 1 < len(merged):
            combined = count_tokens(current.content + "\n\n" + merged[index + 1].content)
            if combined <= ABSORB_MAX_TOKENS:
                merged[index + 1] = join_drafts(current, merged[index + 1])
                index += 1
                continue
        result.append(current)
        index += 1
    return donate_to_undersized(result)


def merge_drafts(drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    if not drafts:
        return []

    merged: list[ChunkDraft] = []
    current = drafts[0]
    for draft in drafts[1:]:
        same_group = current.group == draft.group
        need_fill = current.token_count() < MIN_CHUNK_TOKENS
        if (same_group or need_fill) and can_merge(current, draft):
            current = join_drafts(current, draft)
            continue
        merged.append(current)
        current = draft
    merged.append(current)
    return absorb_undersized(merged)


def overlap_from_table(table: str, overlap_tokens: int) -> str:
    rows = re.findall(r"<tr[\s\S]*?</tr>", table, flags=re.IGNORECASE)
    if len(rows) <= 1:
        return ""
    open_tag_match = re.match(r"<table\b[^>]*>", table, flags=re.IGNORECASE)
    open_tag = open_tag_match.group(0) if open_tag_match else "<table>"
    header = rows[0]
    taken: list[str] = []
    rendered = ""
    for row in reversed(rows[1:]):
        candidate_rows = [header, *reversed(taken + [row])]
        candidate = f"{open_tag}{''.join(candidate_rows)}</table>"
        if taken and count_tokens(candidate) > int(overlap_tokens * 1.4):
            break
        taken.append(row)
        rendered = candidate
        if count_tokens(candidate) >= overlap_tokens:
            break
    return rendered


def take_overlap(text: str, overlap_tokens: int) -> str:
    if overlap_tokens <= 0 or not text.strip():
        return ""
    if count_tokens(text) <= overlap_tokens:
        return text.strip()

    blocks = iter_atomic_blocks(text)
    collected: list[str] = []
    total = 0
    for block in reversed(blocks):
        stripped = block.lstrip().lower()
        block_tokens = count_tokens(block)
        if stripped.startswith("<table") and block_tokens > int(overlap_tokens * 1.5):
            mini = overlap_from_table(block, overlap_tokens)
            if not collected and mini:
                return mini
            break
        if collected and total + block_tokens > int(overlap_tokens * 1.4):
            break
        collected.append(block)
        total += block_tokens
        if total >= overlap_tokens:
            break

    overlap = normalize_blank_lines("\n\n".join(reversed(collected)))
    if overlap and count_tokens(overlap) > int(overlap_tokens * 1.5):
        if "<table" in overlap.lower():
            return collected[0] if collected and count_tokens(collected[0]) <= overlap_tokens * 2 else ""
        pieces = _split_by_char_budget(overlap, overlap_tokens)
        overlap = pieces[-1] if pieces else overlap
    return overlap


def apply_overlap(drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    if not drafts:
        return []
    unique_contents = [draft.content for draft in drafts]
    overlapped: list[ChunkDraft] = []
    for index, draft in enumerate(drafts):
        if index == 0:
            overlapped.append(draft)
            continue
        overlap = take_overlap(unique_contents[index - 1], OVERLAP_TOKENS)
        content = draft.content
        if overlap:
            content = normalize_blank_lines(overlap + "\n\n" + draft.content)
        overlapped.append(ChunkDraft(titles=draft.titles, content=content, group=draft.group))
    return overlapped


def format_chunks(file_name: str, title: str, drafts: list[ChunkDraft], file_title: str = "") -> list[dict]:
    chunks: list[dict] = []
    for index, draft in enumerate(drafts, start=1):
        chunks.append(
            {
                "file_name": file_name,
                "file_title": file_title or file_name,
                "part": index,
                "title": title,
                "content": draft.content,
                "part_title": draft.part_title(),
            }
        )
    return chunks


def split_markdown(content: str, doc_title: str) -> list[ChunkDraft]:
    sections = parse_heading_sections(content)
    drafts: list[ChunkDraft] = []
    for section in sections:
        drafts.extend(split_oversized_section(section))
    drafts = merge_drafts(drafts)
    return apply_overlap(drafts)


def list_new_markdown(output_dir: Path) -> list[Path]:
    files = sorted(
        path for path in output_dir.rglob("*_new.md") if path.is_file()
    )
    logger.info("在 %s 中找到 %d 个 *_new.md 文件", output_dir, len(files))
    return files


def log_chunk_stats(file_name: str, chunks: list[dict]) -> None:
    if not chunks:
        logger.warning("%s 未生成任何 chunk", file_name)
        return
    counts = [count_tokens(chunk["content"]) for chunk in chunks]
    logger.info(
        "%s -> %d 个 chunk，token min/avg/max = %d/%.0f/%d",
        file_name,
        len(chunks),
        min(counts),
        sum(counts) / len(counts),
        max(counts),
    )
    for chunk, tokens in zip(chunks, counts):
        if tokens < MIN_CHUNK_TOKENS or tokens > ABSORB_MAX_TOKENS + OVERLAP_TOKENS:
            logger.warning(
                "chunk 大小偏离目标: file=%s part=%s tokens=%d title=%s",
                file_name,
                chunk["part"],
                tokens,
                chunk["part_title"],
            )


def write_summary_json(chunks: list[dict]) -> None:
    """回写兼容旧脚本的汇总文件。单文件结果已落盘时，汇总写入失败不中断入库。"""
    payload = json.dumps(chunks, ensure_ascii=False, indent=2)
    last_error: OSError | None = None
    for attempt in range(3):
        try:
            CHUNKS_JSON.write_text(payload, encoding="utf-8")
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    logger.warning("汇总 JSON 暂不可写，单文件切分结果已保留: %s", last_error)


def process_markdown(
    md_path: Path,
    content: str | None = None,
    file_title: str = "",
) -> list[dict]:
    if content is None:
        content = md_path.read_text(encoding="utf-8")
    doc_title = extract_doc_title(content, md_path)
    drafts = split_markdown(content, doc_title)
    chunks = format_chunks(md_path.name, doc_title, drafts, file_title=file_title)
    log_chunk_stats(md_path.name, chunks)

    per_file_path = md_path.with_name(f"{md_path.stem}_chunks.json")
    per_file_path.write_text(
        json.dumps(chunks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("已写入: %s", per_file_path.name)
    return chunks


def node_docment_split(state: ImportGraphState) -> ImportGraphState:
    """按标题切分当前 Markdown，并把 chunks / split_path 写回状态。"""
    logger.info(">>> 开始执行节点：node_docment_split，当前状态：%s", format_state(state))
    md_path = Path(str(state.get("md_path") or "")).expanduser()
    if not md_path.is_file():
        raise ValueError(f"待切分的 Markdown 不存在: {md_path}")

    content = state.get("md_content") or md_path.read_text(encoding="utf-8")
    chunks = process_markdown(
        md_path,
        content=content,
        file_title=str(state.get("file_title") or md_path.stem),
    )
    split_path = md_path.with_name(f"{md_path.stem}_chunks.json")
    write_summary_json(chunks)
    state["chunks"] = chunks
    state["split_path"] = str(split_path.resolve())
    logger.info("切分完成: %d 个 chunk，split_path=%s", len(chunks), split_path.name)
    logger.info("更新后状态：%s", format_state(state))
    return state


def main() -> None:
    if not OUTPUT_DIR.exists():
        raise SystemExit(f"启动失败：未找到 output 目录: {OUTPUT_DIR}")

    md_files = list_new_markdown(OUTPUT_DIR)
    if not md_files:
        logger.warning("output 目录中没有 *_new.md 文件")
        return

    all_chunks: list[dict] = []
    failed: list[str] = []
    for md_path in md_files:
        try:
            logger.info("开始切块: %s", md_path.name)
            all_chunks.extend(process_markdown(md_path))
        except Exception:
            logger.error("切块失败: %s\n%s", md_path, traceback.format_exc())
            failed.append(str(md_path))

    write_summary_json(all_chunks)
    logger.info("已写入汇总 JSON: %s ，共 %d 个 chunk", CHUNKS_JSON, len(all_chunks))

    if failed:
        raise SystemExit(f"有 {len(failed)} 个文件切块失败: {failed}")
    logger.info("全部切块完成，共 %d 个文件", len(md_files))


if __name__ == "__main__":
    main()
