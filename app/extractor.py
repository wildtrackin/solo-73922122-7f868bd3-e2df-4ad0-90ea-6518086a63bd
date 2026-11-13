"""读取 LibreOffice 重算后的工作簿并产出只含值的 CSV。

输入必须是 LibreOffice 重新计算并另存后的 xlsx；openpyxl 以 data_only=True
读取到的是 LibreOffice 写入的新缓存值，而非上传文件携带的旧缓存。
任何公式错误、非有限校正值或结构缺失都会抛出 WorkbookError，整批失败。
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

SHEET_NAME = "测量"
REQUIRED_COLUMNS = ("sample_id", "reading", "corrected")
CSV_HEADER = ("sample_id", "reading", "corrected")


class WorkbookError(Exception):
    """工作簿结构或数据不合规（对用户可见的安全消息）。"""


def validate_structure(xlsx_path: str | Path) -> None:
    """上传后的轻量结构预校验：只检查必需表与列，不读取任何缓存值。

    数值有效性、公式错误等仍以 LibreOffice 重算后的文件为唯一依据。
    """
    try:
        wb = load_workbook(xlsx_path, read_only=True, data_only=False)
    except Exception as exc:  # noqa: BLE001
        raise WorkbookError(f"无法打开工作簿: {exc}") from exc
    try:
        if SHEET_NAME not in wb.sheetnames:
            raise WorkbookError(f"工作簿缺少“{SHEET_NAME}”表")
        ws = wb[SHEET_NAME]
        first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not first:
            raise WorkbookError(f"“{SHEET_NAME}”表为空")
        header = {str(c).strip() for c in first if c is not None}
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise WorkbookError(
                f"“{SHEET_NAME}”表缺少必需列: {', '.join(missing)}"
            )
    finally:
        wb.close()


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _find_columns(header_row: tuple[Any, ...]) -> dict[str, int]:
    index: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        if cell is None:
            continue
        name = str(cell).strip()
        if name in REQUIRED_COLUMNS and name not in index:
            index[name] = idx
    missing = [c for c in REQUIRED_COLUMNS if c not in index]
    if missing:
        raise WorkbookError(
            f"“{SHEET_NAME}”表缺少必需列: {', '.join(missing)}"
        )
    return index


def extract_to_csv(recalculated_xlsx: str | Path, csv_path: str | Path) -> int:
    """提取重算值写入 CSV，返回数据行数。失败时抛出 WorkbookError。"""
    path = Path(recalculated_xlsx)
    if not path.is_file():
        raise WorkbookError("重算输出文件不存在")

    try:
        wb = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise WorkbookError(f"无法打开重算后的工作簿: {exc}") from exc

    try:
        if SHEET_NAME not in wb.sheetnames:
            raise WorkbookError(f"工作簿缺少“{SHEET_NAME}”表")
        ws = wb[SHEET_NAME]
        rows = ws.iter_rows(values_only=False)
        try:
            header = tuple(next(rows))
        except StopIteration:
            raise WorkbookError(f"“{SHEET_NAME}”表为空")
        header_values = tuple(c.value for c in header)
        cols = _find_columns(header_values)

        result_rows: list[tuple[Any, Any, Any]] = []
        for row in ws.iter_rows(min_row=2, values_only=False):
            values = [c.value for c in row]
            if all(v is None for v in values):
                continue  # 跳过整行空白

            sample_cell = row[cols["sample_id"]]
            reading_cell = row[cols["reading"]]
            corrected_cell = row[cols["corrected"]]

            for label, cell in (
                ("sample_id", sample_cell),
                ("reading", reading_cell),
                ("corrected", corrected_cell),
            ):
                if cell.data_type == "e":
                    raise WorkbookError(
                        f"第 {cell.row} 行 {label} 列存在公式错误: {cell.value!r}，整批失败"
                    )

            sample_id = sample_cell.value
            if sample_id is None or str(sample_id).strip() == "":
                raise WorkbookError(
                    f"第 {sample_cell.row} 行 sample_id 为空，整批失败"
                )

            reading = reading_cell.value
            if not _is_finite_number(reading):
                raise WorkbookError(
                    f"第 {reading_cell.row} 行 reading 非有限数值: {reading!r}，整批失败"
                )

            corrected = corrected_cell.value
            if not _is_finite_number(corrected):
                raise WorkbookError(
                    f"第 {corrected_cell.row} 行 corrected 非有限数值: "
                    f"{corrected!r}，整批失败"
                )

            result_rows.append(
                (
                    str(sample_id).strip(),
                    float(reading),
                    float(corrected),
                )
            )
    finally:
        wb.close()

    if not result_rows:
        raise WorkbookError(f"“{SHEET_NAME}”表没有任何数据行，整批失败")

    out = Path(csv_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        writer.writerows(result_rows)
    return len(result_rows)
