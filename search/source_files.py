"""按引用里的文件名找到已经导入的 PDF 或 Word 原文。"""

from __future__ import annotations

from pathlib import Path

_OFFICE = {".pdf", ".doc", ".docx"}


def find_imported_file(file_name: str, root: Path | None = None) -> Path | None:
    """只返回入库目录里的 PDF 或 Word，不把 Markdown 当作下载文件。"""
    name = (file_name or "").strip()
    if not name or Path(name).name != name or name in {".", ".."}:
        return None
    if root is None:
        from web_server import TASK_ROOT

        root = TASK_ROOT
    if not root.exists():
        return None
    stem = Path(name).stem
    if stem.endswith("_new"):
        stem = stem[: -len("_new")]
    matches: list[Path] = []
    for path in root.glob("*/*/*"):
        if not path.is_file() or path.parent.name != "source":
            continue
        if path.suffix.lower() not in _OFFICE:
            continue
        if path.name == name or path.stem == stem:
            matches.append(path)
    if not matches:
        return None
    matches.sort(key=lambda path: (path.suffix.lower() != ".pdf", path.name))
    return matches[0]
