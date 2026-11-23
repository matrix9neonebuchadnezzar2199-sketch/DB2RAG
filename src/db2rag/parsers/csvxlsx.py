"""CSV / XLSX adapter。1行を1論理ブロックにする。"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from db2rag.parsers.canonical import CanonicalDocument
from db2rag.parsers.tables import table_blocks
from db2rag.textnorm import normalize_text

logger = logging.getLogger(__name__)
# これ以下だけ通常モードで開き、結合セルを左上の値で埋める。
MERGED_CELL_MAX_BYTES = 20 * 1024 * 1024


def _cell(value: object) -> str:
    if value is None:
        return ""
    return normalize_text(str(value))


def _is_blank_row(cells: list[str]) -> bool:
    return not any(cell.strip() for cell in cells)


def parse_csv(path: Path, title: str | None = None) -> CanonicalDocument:
    """Parse a CSV file into table blocks.

    Args:
        path: CSV path. Encoding is detected with fallbacks.
        title: Document title. Defaults to the file stem.

    Returns:
        Canonical document. Rows whose cells are all empty are omitted.

    Raises:
        ValueError: If the file cannot be decoded.
    """
    from db2rag.parsers import read_text_with_fallback

    text = read_text_with_fallback(path)
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(text)):
        cells = [_cell(cell) for cell in row]
        if _is_blank_row(cells):
            continue
        rows.append(cells)
    headers = rows[0] if rows else []
    body = rows[1:] if len(rows) > 1 else []
    blocks = table_blocks(headers, body, section_path=title or path.stem, sheet_name=path.stem)
    return CanonicalDocument(
        title=title or path.stem,
        language="ja",
        mime="text/csv",
        blocks=blocks,
        extra={"row_count": len(body)},
    )


def parse_xlsx(path: Path, title: str | None = None) -> CanonicalDocument:
    """Parse a workbook into table blocks.

    Files up to 20 MB are opened with merged cells expanded from the top-left
    value. Larger files stay in read-only mode and a warning is logged.

    Args:
        path: XLSX path.
        title: Document title. Defaults to the file stem.

    Returns:
        Canonical document. Blank rows and sheets with no data rows are omitted.
    """
    expand_merged = path.stat().st_size <= MERGED_CELL_MAX_BYTES
    if expand_merged:
        workbook = load_workbook(path, read_only=False, data_only=True)
    else:
        logger.warning(
            "XLSX が %s バイトを超えたため結合セルは展開しません: %s",
            MERGED_CELL_MAX_BYTES,
            path.name,
        )
        workbook = load_workbook(path, read_only=True, data_only=True)
    blocks: list[Any] = []
    try:
        doc_title = title or path.stem
        for sheet in workbook.worksheets:
            rows = _sheet_rows(sheet, expand_merged=expand_merged)
            # ヘッダのみ、または有効なデータ行が無いシートは捨てる。
            if len(rows) < 2:
                continue
            headers = [cell or f"列{index + 1}" for index, cell in enumerate(rows[0])]
            body = rows[1:]
            blocks.extend(
                table_blocks(
                    headers,
                    body,
                    section_path=f"{doc_title}>{sheet.title}",
                    sheet_name=sheet.title,
                )
            )
    finally:
        workbook.close()
    return CanonicalDocument(
        title=title or path.stem,
        language="ja",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        blocks=blocks,
    )


def _sheet_rows(sheet: Any, *, expand_merged: bool) -> list[list[str]]:
    if expand_merged:
        return _rows_with_merged_cells(sheet)
    rows: list[list[str]] = []
    for row in sheet.iter_rows(values_only=True):
        cells = [_cell(value) for value in row]
        if _is_blank_row(cells):
            continue
        rows.append(cells)
    return rows


def _merged_value_map(sheet: Any) -> dict[tuple[int, int], object]:
    # 結合範囲の全座標へ、左上セルの値をコピーする。
    mapped: dict[tuple[int, int], object] = {}
    ranges = getattr(sheet.merged_cells, "ranges", ())
    for cell_range in ranges:
        value = sheet.cell(cell_range.min_row, cell_range.min_col).value
        for row_idx in range(cell_range.min_row, cell_range.max_row + 1):
            for col_idx in range(cell_range.min_col, cell_range.max_col + 1):
                mapped[(row_idx, col_idx)] = value
    return mapped


def _rows_with_merged_cells(sheet: Any) -> list[list[str]]:
    merged = _merged_value_map(sheet)
    max_row = int(sheet.max_row or 0)
    max_col = int(sheet.max_column or 0)
    rows: list[list[str]] = []
    for row_idx in range(1, max_row + 1):
        cells: list[str] = []
        for col_idx in range(1, max_col + 1):
            if (row_idx, col_idx) in merged:
                raw = merged[(row_idx, col_idx)]
            else:
                raw = sheet.cell(row_idx, col_idx).value
            cells.append(_cell(raw))
        if _is_blank_row(cells):
            continue
        rows.append(cells)
    return rows
