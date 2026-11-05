"""压缩只渲染事实账本。叙述里的改写数字不能进入摘要。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompressedContext:
    text: str
    kept_conflict: bool


def compress_context(
    *,
    corrections: list[str],
    conflicts: list[dict[str, Any]],
    missing: list[str],
    facts: list[dict[str, str]],
    narrative: str = "",
) -> CompressedContext:
    del narrative
    lines = []
    for fact in facts:
        lines.append(
            f"{fact.get('entity', '')} {fact.get('metric', '')} "
            f"{fact.get('value', '')}{fact.get('unit', '')} [{fact.get('fact_id', '')}]"
        )
    for correction in corrections:
        lines.append(f"有效纠正：{correction}")
    for item in missing:
        lines.append(f"缺项：{item}")
    if conflicts:
        identifiers = []
        for conflict in conflicts:
            identifiers.extend(str(item) for item in conflict.get("fact_ids") or [])
        lines.append("未消解冲突：" + ",".join(identifiers))
    return CompressedContext("\n".join(lines), kept_conflict=bool(conflicts))
