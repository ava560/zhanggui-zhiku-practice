"""表工具只返回证据。自由 SQL 不执行，未确认指标不计算。"""

from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import sql

from structured.catalog import CatalogError, prepare, ready_metadata
from structured.db import DatabaseConfigError, connect, ensure_database
from structured.formula import FormulaError, compile_formula

QUERY_FAILED = "query_failed"
NO_RECORDS = "no_records"
METRIC_UNCONFIRMED = "metric_unconfirmed"
METADATA_MISSING = "metadata_missing"
_FILTERS = {"eq", "neq", "contains", "gte", "lte", "in"}
_TOKEN = re.compile(r"[A-Za-z0-9_\-\.]+|[\u4e00-\u9fff]{2,}")


def query_table_data(
    question: str,
    *,
    conn=None,
    indexes: Any = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    opened = None
    try:
        if conn is None:
            ensure_database()
            opened = connect()
            conn = opened
            prepare(conn)
        return _query(conn, question, indexes=indexes, plan=plan)
    except (DatabaseConfigError, psycopg.OperationalError, psycopg.InterfaceError) as exc:
        return _reason(QUERY_FAILED, type(exc).__name__)
    finally:
        if opened is not None and not opened.closed:
            opened.rollback()
            opened.close()


def _query(conn, question: str, *, indexes: Any, plan: dict[str, Any] | None) -> dict[str, Any]:
    if plan and plan.get("sql"):
        return _reason(METRIC_UNCONFIRMED, "不执行自由 SQL")
    catalog = ready_metadata(conn)
    if not catalog:
        return _reason(METADATA_MISSING)
    chosen, relation_gap = _choose_tables(question, catalog, indexes)
    if plan is not None:
        chosen = [item for item in catalog if item["table_id"] == plan.get("table_id")]
        relation_gap = False
    if not chosen:
        return _reason(METADATA_MISSING)
    active = plan or _heuristic_plan(question, chosen[0], indexes, catalog)
    if active.get("metric_unconfirmed"):
        return _reason(METRIC_UNCONFIRMED)
    result = _run_plan(conn, chosen[0], active)
    if relation_gap:
        result["gaps"] = ["没有登记表间关联，只返回能够单独确定的部分"]
    return result


def _run_plan(conn, table: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("sql") or plan.get("join"):
        return _reason(METRIC_UNCONFIRMED, "不执行自由 SQL，也不自动连接")
    columns = {column["column_id"]: column for column in table["columns"]}
    by_name = {column["column_name"]: column for column in table["columns"]}
    metric = None
    if plan.get("metric_id"):
        metric = next((item for item in table["metrics"] if item["metric_id"] == plan["metric_id"]), None)
        if metric is None or metric["status"] != "approved" or metric["review_status"] != "confirmed":
            return _reason(METRIC_UNCONFIRMED)
        if metric["table_id"] != table["table_id"]:
            return _reason(METRIC_UNCONFIRMED, "不自动跨表")
    try:
        statement, params, selected, zero = _compile(table, plan, columns, by_name, metric)
    except (CatalogError, FormulaError) as exc:
        return _reason(METRIC_UNCONFIRMED, str(exc))
    try:
        from psycopg.pq import TransactionStatus

        own_transaction = conn.info.transaction_status == TransactionStatus.IDLE
        with conn.transaction():
            if own_transaction:
                conn.execute("SET LOCAL transaction_read_only = on")
            rows = conn.execute(statement, params).fetchall()
    except psycopg.Error as exc:
        return _reason(QUERY_FAILED, type(exc).__name__)
    if not rows or (zero and rows and all(row.get("formula_div_zero") for row in rows)):
        if zero and rows and any(row.get("formula_div_zero") for row in rows):
            return _reason(NO_RECORDS, "除数为零，结果为空")
        if not rows:
            return _reason(NO_RECORDS)
    visible = []
    for row in rows:
        item = {key: row[key] for key in selected if key in row}
        if zero and row.get("formula_div_zero"):
            item["div_zero"] = True
            item["value"] = None
        visible.append(_plain(item))
    if visible and all(item.get("div_zero") for item in visible):
        return {
            **_reason(NO_RECORDS, "除数为零，结果为空"),
            "table_results": [_envelope(table, selected, [], metric)],
        }
    return {
        "table_results": [_envelope(table, selected, visible, metric)],
        "table_reason": "",
        "gaps": [],
    }


def _compile(table, plan, columns, by_name, metric):
    filters, params = _filters(plan.get("filters") or [], by_name)
    group_names = []
    for name in plan.get("group_by") or []:
        if name not in by_name:
            raise CatalogError("分组字段未登记")
        group_names.append(name)
    limit = min(max(int(plan.get("limit") or 50), 1), 200)
    select_parts = []
    selected = []
    zero = sql.SQL("FALSE")
    if metric is not None:
        expression, zero = compile_formula(metric["formula_tree"], columns)
        select_parts.append(sql.SQL("{} AS {}").format(expression, sql.Identifier("value")))
        selected.append("value")
    for item in plan.get("aggregates") or []:
        column = columns.get(item.get("column_id")) or by_name.get(item.get("column"))
        if column is None:
            raise CatalogError("聚合字段未登记")
        agg = str(item.get("agg") or "")
        if agg in {"sum", "avg"} and column["role"] != "度量":
            raise CatalogError("只有度量可以做 SUM 或 AVG")
        if agg not in {"sum", "avg", "count", "count_distinct"}:
            raise CatalogError("缺少可用口径")
        target = sql.Identifier(column["column_name"])
        alias = item.get("as") or f"{agg}_{column['column_name']}"
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", str(alias)):
            raise CatalogError("结果字段名不合法")
        if agg == "count_distinct":
            expression = sql.SQL("COUNT(DISTINCT {})").format(target)
        else:
            expression = sql.SQL("{}({})").format(sql.SQL(agg.upper()), target)
        select_parts.append(sql.SQL("{} AS {}").format(expression, sql.Identifier(alias)))
        selected.append(alias)
    for name in plan.get("columns") or []:
        if name not in by_name:
            raise CatalogError("字段未登记")
        select_parts.append(sql.Identifier(name))
        selected.append(name)
    for name in group_names:
        if name not in selected:
            select_parts.insert(0, sql.Identifier(name))
            selected.insert(0, name)
    if not select_parts:
        for column in table["columns"]:
            select_parts.append(sql.Identifier(column["column_name"]))
            selected.append(column["column_name"])
    where = sql.SQL("import_version_id = %s AND data_version_id = %s")
    where_params = [table["import_version_id"], table["data_version_id"]]
    if filters is not None:
        where = sql.SQL("{} AND {}").format(where, filters)
        where_params.extend(params)
    group_sql = sql.SQL("")
    if group_names:
        group_sql = sql.SQL(" GROUP BY {}").format(sql.SQL(", ").join(sql.Identifier(name) for name in group_names))
    statement = sql.SQL("SELECT {}, {} AS formula_div_zero FROM {} WHERE {}{} LIMIT %s").format(
        sql.SQL(", ").join(select_parts),
        zero,
        sql.Identifier(table["table_name"]),
        where,
        group_sql,
    )
    return statement, [*where_params, limit], selected, metric is not None and _has_div(metric["formula_tree"])


def _filters(filters, by_name):
    if not filters:
        return None, []
    parts = []
    params = []
    for item in filters:
        column = by_name.get(item.get("column"))
        op = item.get("op")
        if column is None or op not in _FILTERS:
            raise CatalogError("过滤字段未登记")
        ident = sql.Identifier(column["column_name"])
        value = item.get("value")
        if op == "eq":
            parts.append(sql.SQL("{} = %s").format(ident))
            params.append(value)
        elif op == "neq":
            parts.append(sql.SQL("{} <> %s").format(ident))
            params.append(value)
        elif op == "contains":
            parts.append(sql.SQL("{} LIKE %s ESCAPE '\\'").format(ident))
            params.append("%" + str(value).replace("%", "\\%").replace("_", "\\_") + "%")
        elif op == "gte":
            parts.append(sql.SQL("{} >= %s").format(ident))
            params.append(value)
        elif op == "lte":
            parts.append(sql.SQL("{} <= %s").format(ident))
            params.append(value)
        else:
            if not isinstance(value, list) or not value:
                raise CatalogError("in 过滤需要列表")
            parts.append(sql.SQL("{} = ANY(%s)").format(ident))
            params.append(value)
    return sql.SQL(" AND ").join(parts), params


def _choose_tables(question: str, catalog: list[dict[str, Any]], indexes: Any) -> tuple[list[dict[str, Any]], bool]:
    tokens = _tokens(question)
    scored = []
    for table in catalog:
        score = _score(question, tokens, table)
        if score > 0:
            scored.append((score, table))
    if indexes is not None:
        try:
            hits = indexes.search_values(tokens, [table["data_version_id"] for table in catalog if table.get("data_version_id")])
        except Exception:
            hits = []
        hit_tables = {hit.get("table_id") for hit in hits}
        for table in catalog:
            if table["table_id"] in hit_tables and all(table["table_id"] != item["table_id"] for _score, item in scored):
                scored.append((1, table))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return [], False
    best = scored[0][0]
    chosen = [table for score, table in scored if score == best or score >= best - 1]
    chosen = chosen[:2]
    relation = len({table["table_id"] for table in chosen}) > 1 and any(
        word in question for word in ("合并", "关联", "连接", "对照", "一起")
    )
    if relation:
        return [scored[0][1]], True
    return [scored[0][1]], False


def _heuristic_plan(question: str, table: dict[str, Any], indexes: Any, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = set(_tokens(question))
    metric = _mentioned_metric(question, table)
    if metric is not None and metric["status"] != "approved":
        return {"metric_unconfirmed": True, "table_id": table["table_id"]}
    filters = _value_filters(tokens, table, indexes, catalog)
    group = []
    if "按" in question:
        for column in table["columns"]:
            labels = [column["column_name"], *column["aliases"]]
            if any(label and f"按{label}" in question for label in labels):
                group.append(column["column_name"])
                break
    if metric is not None and metric["status"] == "approved":
        return {"table_id": table["table_id"], "metric_id": metric["metric_id"], "filters": filters, "group_by": group}
    aggregate = _mentioned_aggregate(question, table)
    if aggregate is not None:
        return {"table_id": table["table_id"], "aggregates": [aggregate], "filters": filters, "group_by": group}
    columns = [column["column_name"] for column in table["columns"]]
    return {"table_id": table["table_id"], "columns": columns, "filters": filters, "limit": 50}


def _mentioned_metric(question: str, table: dict[str, Any]) -> dict[str, Any] | None:
    for metric in table["metrics"]:
        names = [metric["name"], *(metric.get("aliases") or [])]
        if any(name and name in question for name in names):
            return metric
    return None


def _mentioned_aggregate(question: str, table: dict[str, Any]) -> dict[str, Any] | None:
    measures = [column for column in table["columns"] if column["role"] == "度量"]
    named = [
        column for column in measures
        if column["column_name"] in question or any(alias and alias in question for alias in column["aliases"])
    ]
    target = (named or measures[:1] or [None])[0]
    if target is None:
        return None
    if any(word in question for word in ("平均", "均值")):
        return {"agg": "avg", "column_id": target["column_id"], "as": "avg_value"}
    if any(word in question for word in ("合计", "总和", "汇总", "一共")):
        return {"agg": "sum", "column_id": target["column_id"], "as": "sum_value"}
    if any(word in question for word in ("多少", "几条", "数量", "个数")):
        return {"agg": "count", "column_id": target["column_id"], "as": "count_value"}
    return None


def _value_filters(tokens: set[str], table: dict[str, Any], indexes: Any, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if indexes is None:
        return []
    try:
        hits = indexes.search_values(list(tokens), [item["data_version_id"] for item in catalog if item.get("data_version_id")])
    except Exception:
        return []
    filters = []
    columns = {column["column_id"]: column for column in table["columns"]}
    for hit in hits:
        if hit.get("table_id") != table["table_id"]:
            continue
        column = columns.get(hit.get("column_id"))
        if column is None:
            continue
        filters.append({"column": column["column_name"], "op": "eq", "value": hit.get("value")})
    return filters


def _score(question: str, tokens: list[str], table: dict[str, Any]) -> int:
    score = 0
    blob = " ".join([
        table.get("source_file") or "",
        table.get("table_summary") or "",
        table.get("summary") or "",
        table.get("table_name") or "",
    ])
    if table.get("source_file") and table["source_file"] in question:
        score += 5
    for column in table["columns"]:
        labels = [column["column_name"], *(column.get("aliases") or [])]
        if any(label and label in question for label in labels):
            score += 3
        if any(token and token in (column.get("description") or "") for token in tokens):
            score += 1
    for metric in table["metrics"]:
        if metric["name"] and metric["name"] in question:
            score += 4
    if any(token and token in blob for token in tokens):
        score += 1
    return score


def _tokens(question: str) -> list[str]:
    return [token for token in _TOKEN.findall(question) if len(token) >= 2]


def _has_div(node: object) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("op") == "div":
        return True
    return _has_div(node.get("left")) or _has_div(node.get("right"))


def _envelope(table, selected, rows, metric) -> dict[str, Any]:
    fields = [name for name in selected if name != "div_zero"]
    return {
        "source_file": table["source_file"],
        "table_name": table["table_name"],
        "table_id": table["table_id"],
        "columns": fields,
        "import_version_id": table["import_version_id"],
        "data_version_id": table["data_version_id"],
        "metric": None if metric is None else {
            "name": metric["name"],
            "formula_text": metric.get("formula_text") or "",
            "status": metric["status"],
        },
        "rows": rows,
    }


def _reason(reason: str, detail: str = "") -> dict[str, Any]:
    return {"table_results": [], "table_reason": reason, "detail": detail, "gaps": []}


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    output = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            output[key] = value.isoformat()
        else:
            output[key] = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
    return output
