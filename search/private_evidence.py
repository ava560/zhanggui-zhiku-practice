"""私有库同时取表证据和文档证据。表行不进入文档 RRF。"""

from __future__ import annotations

from typing import Any

from search.shared_query import report_status, run_shared_query


def run_private_evidence(state: dict[str, Any], ports: dict[str, Any]) -> dict[str, Any]:
    question = str(state.get("current_question") or "")
    table = {"table_results": [], "table_reason": "", "gaps": []}
    query_table = ports.get("query_table")
    if callable(query_table):
        report_status(state, "正在查询表数据")
        try:
            found = query_table(question) or {}
            table = {
                "table_results": list(found.get("table_results") or []),
                "table_reason": str(found.get("table_reason") or ""),
                "gaps": list(found.get("gaps") or []),
            }
        except Exception as exc:
            table = {"table_results": [], "table_reason": "query_failed", "gaps": [], "detail": type(exc).__name__}
    document = run_shared_query(state, ports)
    document["table_results"] = table["table_results"]
    document["table_reason"] = table["table_reason"]
    gaps = list(document.get("gaps") or [])
    for gap in table["gaps"]:
        if gap not in gaps:
            gaps.append(gap)
    if table["table_results"] and document.get("status") == "unsupported":
        document["status"] = "supported"
        gaps = [gap for gap in gaps if gap != "未检索到足够信息"]
    if table["table_reason"] == "no_records" and "表中没有对应记录" not in gaps:
        gaps.append("表中没有对应记录")
    if table["table_reason"] == "metric_unconfirmed" and "缺少已确认的指标口径，不能计算" not in gaps:
        gaps.append("缺少已确认的指标口径，不能计算")
    if table["table_reason"] == "query_failed" and "结构化数据查询失败，不能据此判断表里是否有记录。" not in gaps:
        gaps.append("结构化数据查询失败，不能据此判断表里是否有记录。")
    document["gaps"] = gaps
    return document


def table_display_chunks(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chunks = []
    for result in results:
        metric = result.get("metric") or {}
        prefix = f"口径 {metric['formula_text']}。" if metric.get("formula_text") else ""
        for row in result.get("rows") or []:
            body = "；".join(f"{key}={value}" for key, value in row.items() if key != "div_zero" and value is not None)
            chunks.append({
                "file_name": result.get("source_file") or "",
                "table_name": result.get("table_name") or "",
                "content": (
                    f"表 {result.get('table_name')}，字段 {', '.join(result.get('columns') or [])}，"
                    f"版本 {result.get('data_version_id')}：{prefix}{body}"
                ),
                "source": "table",
            })
    return chunks
