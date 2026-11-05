"""长期偏好只接受明确指令；删除后保留抑制标记，避免从旧消息学回。"""

from __future__ import annotations

import re
from typing import Any


_UNIT_REQUEST = re.compile(r"以后.+按(?P<unit>亿元|万元|元)显示")


class PreferenceBook:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._sources: dict[str, str] = {}
        self._suppressed: dict[str, set[str]] = {}

    def remember(self, key: str, value: str, source_message_id: str, explicit: bool = False) -> bool:
        if not explicit:
            return False
        if source_message_id in self._suppressed.get(key, set()):
            return False
        self._values[key] = value
        self._sources[key] = source_message_id
        return True

    def forget(self, key: str) -> None:
        source = self._sources.get(key)
        if source:
            self._suppressed.setdefault(key, set()).add(source)
        self._values.pop(key, None)

    def learn_from_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        learned = []
        for message in messages:
            matched = _UNIT_REQUEST.search(str(message.get("text") or ""))
            if not matched:
                continue
            source = str(message.get("id") or "")
            if source in self._suppressed.get("display_unit", set()):
                continue
            value = matched.group("unit")
            self._values["display_unit"] = value
            self._sources["display_unit"] = source
            learned.append({"key": "display_unit", "value": value, "source_message_id": source})
        return learned

    def get(self, key: str) -> str | None:
        return self._values.get(key)
