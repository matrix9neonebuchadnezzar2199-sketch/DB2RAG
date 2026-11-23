"""表を検索可能な行テキストへ。ヘッダを各行に繰返す。"""

from __future__ import annotations

from db2rag.parsers.canonical import Block


def table_blocks(
    headers: list[str],
    rows: list[list[str]],
    *,
    section_path: str,
    sheet_name: str | None = None,
    page: int | None = None,
    max_rows: int = 20,
) -> list[Block]:
    """Render a table as searchable text blocks.

    Args:
        headers: Column names. Missing names are filled by the caller.
        rows: Body rows. Each row is a list of cell strings.
        section_path: Section label repeated on every block.
        sheet_name: Sheet name, when the source is a workbook.
        page: Page number, when the source is paginated.
        max_rows: Maximum data rows per block.

    Returns:
        One or more ``table`` blocks. Headers are repeated on each block.
    """
    blocks: list[Block] = []
    if not headers:
        headers = [f"列{i + 1}" for i in range(max((len(r) for r in rows), default=0))]
    for start in range(0, max(len(rows), 1), max_rows):
        chunk = rows[start : start + max_rows]
        lines = [f"【表: {section_path}】", "列: " + ", ".join(headers)]
        if sheet_name:
            lines.insert(1, f"シート: {sheet_name}")
        for offset, row in enumerate(chunk, start=1):
            cells = []
            for index, header in enumerate(headers):
                value = row[index] if index < len(row) else ""
                cells.append(f"{header}={value}")
            lines.append(f"行{start + offset}: " + ", ".join(cells))
        blocks.append(
            Block(
                type="table",
                text="\n".join(lines),
                page=page,
                extra={
                    "sheet_name": sheet_name,
                    "row_from": start + 1,
                    "row_to": start + len(chunk),
                },
            )
        )
    return blocks
