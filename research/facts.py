"""从已标记可核验的片段抽取数值。解析降级的文本不产生事实。"""

from __future__ import annotations

import re
from typing import Any


_FACT = re.compile(
    r"(平安银行|招商银行|中国平安|华夏债券C|华夏债券)\s*"
    r"(20\d{2})年\s*"
    r"(营业收入|净利润|净值)\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*"
    r"(亿元|万元|元|%)"
)


def extract_verified_facts(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    facts = []
    for chunk in chunks:
        if chunk.get("parse_quality") == "degraded" or chunk.get("numeric_verified") is False:
            continue
        text = str(chunk.get("content") or "")
        for match in _FACT.finditer(text):
            entity, year, metric, value, unit = match.groups()
            facts.append({
                "entity": entity,
                "year": int(year),
                "metric": metric,
                "value": value,
                "unit": unit,
                "chunk": chunk,
            })
    return facts
