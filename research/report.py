"""比较和行业结论只陈述已对齐的事实，缺年或缺样本保持缺口。"""

from __future__ import annotations

from typing import Any


def render_industry(companies: list[str], *, universe_known: bool) -> str:
    sample = "、".join(companies) if companies else "无"
    if universe_known:
        return f"行业分析样本：{sample}。"
    return f"已收录企业样本分析：样本包括{sample}。覆盖范围未知。不能把这几家公司的变化写成全行业趋势。"


def render_comparison(rows: list[dict[str, Any]], *, requested_year: int) -> str:
    entities = list(dict.fromkeys(str(row.get("entity")) for row in rows))
    metrics = list(dict.fromkeys(str(row.get("metric")) for row in rows))
    lines = []
    for entity in entities:
        for metric in metrics:
            matched = [
                row for row in rows
                if row.get("entity") == entity
                and row.get("metric") == metric
                and row.get("year") == requested_year
            ]
            if not matched:
                lines.append(f"{entity} 缺少 {requested_year} 年{metric}，不能用其他年度的数值代替。")
                continue
            row = matched[0]
            lines.append(f"{entity} {requested_year}年 {metric} {row.get('value')}{row.get('unit') or ''}")
    return "\n".join(lines)
