"""研究事件按序号提交。快照和后续游标来自同一次读取。"""

from __future__ import annotations

import threading
from typing import Any


class EventExpired(LookupError):
    """游标已不能续订，调用方需要重新读取快照。"""


class EventLog:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def append(self, event_type: str, payload: dict[str, Any]) -> int:
        with self._lock:
            sequence = len(self._events) + 1
            self._events.append({
                "seq": sequence,
                "event_type": event_type,
                "payload": dict(payload),
            })
            return sequence

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sequence = len(self._events)
            return {
                "snapshot_seq": sequence,
                "events": [dict(event) for event in self._events],
            }

    def replay_after(self, sequence: int) -> list[dict[str, Any]]:
        if sequence < 0:
            raise EventExpired("事件游标已过期，需要重新读取快照")
        with self._lock:
            if sequence > len(self._events):
                raise EventExpired("事件游标已过期，需要重新读取快照")
            return [dict(event) for event in self._events if event["seq"] > sequence]
