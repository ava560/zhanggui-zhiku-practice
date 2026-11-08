"""读取表格文件的一个工作表。人员没选定且有多张表时，不替用户挑。"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

TABLE_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls"}
_IDENTIFIER = __import__("re").compile(r"^[A-Za-z][A-Za-z0-9_]*$")
RESERVED = {"import_version_id", "data_version_id", "source_file", "loaded_at"}


@dataclass(frozen=True)
class ParsedSheet:
    sheet_name: str
    headers: list[str]
    rows: list[dict[str, object]]
    sheets: list[str]


class SheetChoiceNeeded(Exception):
    def __init__(self, sheets: list[str]) -> None:
        self.sheets = sheets
        super().__init__("请先选定一个工作表")


class SheetError(ValueError):
    pass


def extension_of(filename: str) -> str:
    return Path(filename).suffix.lower()


def is_table_file(filename: str) -> bool:
    return extension_of(filename) in TABLE_EXTENSIONS


def read_table(filename: str, data: bytes, sheet_name: str | None = None) -> ParsedSheet:
    suffix = extension_of(filename)
    if suffix == ".csv":
        return _read_csv(filename, data)
    if suffix in {".xlsx", ".xlsm"}:
        return _read_openpyxl(data, sheet_name)
    if suffix == ".xls":
        return _read_xls(data, sheet_name)
    raise SheetError("不是表格文件")


def physical_name(header: str, position: int, used: set[str]) -> str:
    candidate = header.strip()
    if _IDENTIFIER.match(candidate) and candidate.lower() not in RESERVED and candidate not in used:
        used.add(candidate)
        return candidate
    fallback = f"c{position}"
    while fallback in used:
        fallback += "x"
    used.add(fallback)
    return fallback


def infer_type(values: list[object]) -> str:
    present = [value for value in values if value not in (None, "")]
    if not present:
        return "text"
    if all(isinstance(value, bool) for value in present):
        return "boolean"
    if all(isinstance(value, datetime) for value in present):
        return "timestamptz"
    if all(isinstance(value, date) and not isinstance(value, datetime) for value in present):
        return "date"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return "bigint"
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        return "numeric"
    if all(_is_int_text(value) for value in present):
        return "bigint"
    if all(_is_number_text(value) for value in present):
        return "numeric"
    return "text"


def _is_int_text(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and (text.isdigit() or (text.startswith("-") and text[1:].isdigit()))


def _is_number_text(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    try:
        float(value.strip())
    except ValueError:
        return False
    return True


def _clean_headers(raw: list[object]) -> list[str]:
    headers = []
    for index, value in enumerate(raw, start=1):
        text = "" if value is None else str(value).strip()
        if not text:
            raise SheetError(f"第 {index} 列表头为空")
        if text in headers:
            raise SheetError(f"表头重复：{text}")
        headers.append(text)
    if not headers:
        raise SheetError("表格没有表头")
    return headers


def _records(headers: list[str], matrix: list[list[object]]) -> list[dict[str, object]]:
    rows = []
    width = len(headers)
    for matrix_row in matrix:
        values = list(matrix_row[:width])
        if len(values) < width:
            values.extend([None] * (width - len(values)))
        if all(value in (None, "") for value in values):
            continue
        rows.append(dict(zip(headers, values)))
    return rows


def _read_csv(filename: str, data: bytes) -> ParsedSheet:
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration as exc:
        raise SheetError("表格没有表头") from exc
    headers = _clean_headers(header_row)
    body = [list(row) for row in reader]
    sheet = Path(filename).stem or "csv"
    return ParsedSheet(sheet, headers, _records(headers, body), [sheet])


def _read_openpyxl(data: bytes, sheet_name: str | None) -> ParsedSheet:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        names = list(workbook.sheetnames)
        chosen = _choose(names, sheet_name)
        sheet = workbook[chosen]
        cursor = sheet.iter_rows(values_only=True)
        try:
            header_row = next(cursor)
        except StopIteration as exc:
            raise SheetError("表格没有表头") from exc
        headers = _clean_headers(list(header_row or []))
        body = [list(row) for row in cursor]
        return ParsedSheet(chosen, headers, _records(headers, body), names)
    finally:
        workbook.close()


def _read_xls(data: bytes, sheet_name: str | None) -> ParsedSheet:
    try:
        import xlrd
    except ImportError as exc:
        raise SheetError("读取 xls 需要 xlrd") from exc
    workbook = xlrd.open_workbook(file_contents=data)
    names = workbook.sheet_names()
    chosen = _choose(names, sheet_name)
    sheet = workbook.sheet_by_name(chosen)
    if sheet.nrows < 1:
        raise SheetError("表格没有表头")
    headers = _clean_headers(sheet.row_values(0))
    body = [sheet.row_values(index) for index in range(1, sheet.nrows)]
    return ParsedSheet(chosen, headers, _records(headers, body), names)


def _choose(names: list[str], sheet_name: str | None) -> str:
    if sheet_name:
        if sheet_name not in names:
            raise SheetError(f"工作表不存在：{sheet_name}")
        return sheet_name
    if len(names) == 1:
        return names[0]
    raise SheetChoiceNeeded(names)
