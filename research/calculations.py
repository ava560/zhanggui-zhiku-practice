"""财务换算只使用明确单位和 Decimal，零基数或负基数不输出增长率。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


_SCALE = {
    "元": Decimal("1"),
    "万元": Decimal("10000"),
    "亿元": Decimal("100000000"),
}


@dataclass(frozen=True)
class Amount:
    raw_value: str
    normalized: Decimal
    unit: str


@dataclass(frozen=True)
class YearOverYear:
    status: str
    ratio: Decimal | None
    warning: str


def convert_amount(raw_value: str, from_unit: str, to_unit: str) -> Amount:
    if from_unit not in _SCALE or to_unit not in _SCALE:
        raise ValueError("只接受元、万元、亿元之间的换算")
    normalized = (Decimal(raw_value) * _SCALE[from_unit] / _SCALE[to_unit])
    return Amount(str(raw_value), normalized, to_unit)


def year_over_year(current: Decimal, previous: Decimal) -> YearOverYear:
    if previous == 0:
        return YearOverYear("unresolved", None, "零基数不能计算同比")
    if previous < 0:
        return YearOverYear("unresolved", None, "负基数不直接输出增长率")
    return YearOverYear("verified", (current - previous) / abs(previous), "")
