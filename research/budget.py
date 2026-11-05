"""调用前预留额度。结果不明的尝试不退回已预留的 token。"""

from __future__ import annotations

import threading
from decimal import Decimal


class BudgetLedger:
    def __init__(self, token_limit: int, closing_reserve_ratio: float = 0.2) -> None:
        self.token_limit = token_limit
        self.closing_reserve_ratio = closing_reserve_ratio
        self._reserved = 0
        self._attempts: dict[tuple[str, str], dict[str, int | str]] = {}
        self._lock = threading.Lock()

    @property
    def reserved(self) -> int:
        return self._reserved

    def usable_tokens(self) -> int:
        reserve = int(Decimal(self.token_limit) * Decimal(str(self.closing_reserve_ratio)))
        return self.token_limit - reserve

    def reserve(self, call_id: str, attempt_id: str, tokens: int) -> bool:
        return self.reserve_many([(call_id, attempt_id, tokens)])[0]

    def reserve_many(self, requests: list[tuple[str, str, int]]) -> list[bool]:
        granted: list[bool] = []
        with self._lock:
            for call_id, attempt_id, tokens in requests:
                if tokens < 0 or self._reserved + tokens > self.usable_tokens():
                    granted.append(False)
                    continue
                self._reserved += tokens
                self._attempts[(call_id, attempt_id)] = {"tokens": tokens, "status": "reserved"}
                granted.append(True)
        return granted

    def mark_unknown(self, call_id: str, attempt_id: str) -> None:
        attempt = self._attempts.get((call_id, attempt_id))
        if attempt is not None:
            attempt["status"] = "outcome_unknown"
