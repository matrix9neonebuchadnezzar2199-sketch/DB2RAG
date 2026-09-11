"""CSV / XLSX adapter。1行を1論理ブロックにする。"""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import load_workbook

from db2rag.parsers.canonical import CanonicalDocument
from db2rag.parsers.tables import table_blocks
from db2rag.textnorm import normalize_text


def _cell(value: object) -> str:
    if value is None:
        return ""
    return normalize_text(str(value))


def parse_csv(path: Path, title: str | None = None) -> CanonicalDocument:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        rows = [[_cell(c) for c in row] for row in reader]
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
    workbook = load_workbook(path, read_only=True, data_only=True)
    blocks = []
    for sheet in workbook.worksheets:
        rows_iter = sheet.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            continue
        headers = [_cell(c) or f"列{i + 1}" for i, c in enumerate(header_row)]
        body: list[list[str]] = []
        for row in rows_iter:
            body.append([_cell(c) for c in row])
        blocks.extend(
            table_blocks(
                headers,
                body,
                section_path=f"{title or path.stem}>{sheet.title}",
                sheet_name=sheet.title,
            )
        )
    workbook.close()
    return CanonicalDocument(
        title=title or path.stem,
        language="ja",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        blocks=blocks,
    )
