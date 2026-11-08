"""指标只编译受限公式树。自由 SQL、跨表引用和未确认口径都不执行。"""

from __future__ import annotations

from psycopg import sql

AGGREGATES = {"sum": "SUM", "count": "COUNT", "count_distinct": "COUNT", "avg": "AVG"}
ARITHMETIC = {"add": "+", "sub": "-", "mul": "*", "div": "/"}


class FormulaError(ValueError):
    def __init__(self, message: str) -> None:
        self.reason = "metric_unconfirmed"
        super().__init__(message)


def compile_formula(tree: object, columns: dict[str, dict]) -> tuple[sql.Composable, sql.Composable]:
    """返回表达式，以及是否出现零分母的布尔表达式。"""
    expression, zero_flag = _compile(tree, columns)
    return expression, zero_flag


def _compile(node: object, columns: dict[str, dict]) -> tuple[sql.Composable, sql.Composable]:
    if not isinstance(node, dict) or "op" not in node:
        raise FormulaError("缺少可用口径")
    op = str(node.get("op") or "")
    if op in {"where", "group", "group_by", "sql"} or "sql" in node:
        raise FormulaError("分组和过滤不能写进指标公式")
    if op == "column":
        return _column(node, columns), sql.SQL("FALSE")
    if op == "const":
        value = node.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FormulaError("常量只能是数值")
        return sql.Literal(value), sql.SQL("FALSE")
    if op in AGGREGATES:
        target = _column(node, columns)
        role = columns[str(node.get("column_id"))]["role"]
        if op in {"sum", "avg"} and role != "度量":
            raise FormulaError("只有度量可以做 SUM 或 AVG")
        if op == "count_distinct":
            return sql.SQL("COUNT(DISTINCT {})").format(target), sql.SQL("FALSE")
        return sql.SQL("{}({})").format(sql.SQL(AGGREGATES[op]), target), sql.SQL("FALSE")
    if op in ARITHMETIC:
        left, left_zero = _compile(node.get("left"), columns)
        right, right_zero = _compile(node.get("right"), columns)
        if op == "div":
            expression = sql.SQL("CASE WHEN ({}) = 0 THEN NULL ELSE ({}) / ({}) END").format(right, left, right)
            zero = sql.SQL("(({}) = 0) OR ({}) OR ({})").format(right, left_zero, right_zero)
            return expression, zero
        return sql.SQL("({}) {} ({})").format(left, sql.SQL(ARITHMETIC[op]), right), sql.SQL("({}) OR ({})").format(left_zero, right_zero)
    raise FormulaError("缺少可用口径")


def _column(node: dict, columns: dict[str, dict]) -> sql.Composable:
    column_id = str(node.get("column_id") or "")
    column = columns.get(column_id)
    if column is None:
        raise FormulaError("公式引用了未登记字段")
    return sql.Identifier(column["column_name"])
