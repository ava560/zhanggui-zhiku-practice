"""用户明确写入的长期记忆。普通对话不会自动写入。"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path


_REMEMBER = re.compile(r"(?:请)?记住\s*[:：]?\s*(.+)", re.DOTALL)
_BOOK: LongTermMemoryBook | None = None
_SOURCES = {"user_explicit", "settings"}


def extract_explicit_memory(question: str) -> str | None:
    """只识别用户明确说记住的内容。"""
    matched = _REMEMBER.search(question or "")
    if not matched:
        return None
    text = " ".join(matched.group(1).split())
    return text or None


class LongTermMemoryBook:
    """按用户保存记忆。来源只能是用户明确要求或个性化设置。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: dict[str, list[dict[str, str]]] = {}
        if path is not None and path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._items = {
                    str(user_id): [dict(item) for item in rows if isinstance(item, dict)]
                    for user_id, rows in loaded.items()
                    if isinstance(rows, list)
                }

    def add(self, user_id: str, text: str, source: str) -> dict[str, str]:
        if source not in _SOURCES:
            raise ValueError("长期记忆只能来自用户明确要求或个性化设置")
        body = " ".join((text or "").split())
        if not body:
            raise ValueError("长期记忆内容不能为空")
        row = {
            "user_id": user_id,
            "text": body,
            "source": source,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            rows = [item for item in self._items.get(user_id, []) if item.get("text") != body]
            rows.append(row)
            self._items[user_id] = rows
            self._save()
        return row

    def delete(self, user_id: str, text: str) -> bool:
        body = " ".join((text or "").split())
        with self._lock:
            rows = self._items.get(user_id, [])
            kept = [item for item in rows if item.get("text") != body]
            removed = len(kept) != len(rows)
            self._items[user_id] = kept
            if removed:
                self._save()
        return removed

    def texts(self, user_id: str) -> list[str]:
        with self._lock:
            return [str(item.get("text") or "") for item in self._items.get(user_id, []) if item.get("text")]

    def export(self, user_id: str) -> list[dict[str, str]]:
        with self._lock:
            return [dict(item) for item in self._items.get(user_id, [])]

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8")


def get_memory_book() -> LongTermMemoryBook:
    """进程内一份记忆簿，落在现有 output 目录。"""
    global _BOOK
    if _BOOK is None:
        from state import OUTPUT_DIR

        _BOOK = LongTermMemoryBook(Path(OUTPUT_DIR) / "long_term_memory.json")
    return _BOOK
