"""PDF / DOCX / PPTX / HTML。Docling は任意。無いときは専用ライブラリ。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from pptx import Presentation
from pypdf import PdfReader

from db2rag.parsers.canonical import Block, CanonicalDocument
from db2rag.parsers.tables import table_blocks
from db2rag.parsers.txtmd import parse_txt_text
from db2rag.textnorm import normalize_text

# 章・条・Markdown 見出し・節番号。行全体が 60 文字以内のときだけ見出しにする。
_PDF_HEADING = re.compile(
    r"^(第[0-9一二三四五六七八九十百]+[章節款条]|#{1,6}\s+|[0-9]+(\.[0-9]+){1,2}\s+\S)"
)
_PDF_HEADING_MAX_CHARS = 60
_CHAPTER_MARK = re.compile(r"^第[0-9一二三四五六七八九十百]+([章節款条])")
_HASH_HEADING = re.compile(r"^(#{1,6})\s+")
_NUMBERED_HEADING = re.compile(r"^([0-9]+(?:\.[0-9]+){1,2})\s+\S")
# 款は条より上だが、条は指定どおり level 3 に固定する。
_CHAPTER_LEVEL = {"章": 1, "節": 2, "款": 2, "条": 3}
_HTML_FLOW = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table"]
_HTML_HEADING = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# pathlib が https:// を https:/ や https:\ に潰しても URL と分かるようにする。
_REMOTE_HTML_PREFIXES = ("https://", "http://", "https:/", "http:/")


def parse_pdf(path: Path, title: str | None = None) -> CanonicalDocument:
    """Extract PDF text and split short heading lines into heading blocks.

    Args:
        path: PDF path.
        title: Document title. Defaults to the file stem.

    Returns:
        Canonical document. Consecutive body lines share a paragraph block.

    Raises:
        ValueError: If no text block can be extracted (``ERR_PDF_NO_TEXT``).
    """
    reader = PdfReader(str(path))
    blocks: list[Block] = []
    low_quality_pages = 0
    for number, page in enumerate(reader.pages, start=1):
        extracted = page.extract_text() or ""
        text = normalize_text(extracted)
        if len(text) < 50:
            low_quality_pages += 1
        blocks.extend(_pdf_page_blocks(text, number))
    if not blocks:
        raise ValueError("ERR_PDF_NO_TEXT")
    return CanonicalDocument(
        title=title or path.stem,
        language="ja",
        mime="application/pdf",
        blocks=blocks,
        extra={"page_count": len(reader.pages), "low_quality_pages": low_quality_pages},
    )


def _pdf_heading_level(line: str) -> int | None:
    if len(line) > _PDF_HEADING_MAX_CHARS or _PDF_HEADING.match(line) is None:
        return None
    chapter = _CHAPTER_MARK.match(line)
    if chapter is not None:
        return _CHAPTER_LEVEL[chapter.group(1)]
    hashed = _HASH_HEADING.match(line)
    if hashed is not None:
        return len(hashed.group(1))
    numbered = _NUMBERED_HEADING.match(line)
    if numbered is not None:
        # 1.1 は level 2、1.1.1 は level 3。
        return numbered.group(1).count(".") + 1
    return None


def _pdf_page_blocks(text: str, page: int) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush() -> None:
        if not paragraph:
            return
        joined = normalize_text("\n".join(paragraph))
        paragraph.clear()
        if joined:
            blocks.append(Block(type="paragraph", text=joined, page=page))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        level = _pdf_heading_level(line)
        if level is None:
            paragraph.append(line)
            continue
        flush()
        blocks.append(Block(type="heading", text=line, level=level, page=page))
    flush()
    return blocks


def parse_docx(path: Path, title: str | None = None) -> CanonicalDocument:
    """Parse a DOCX file into headings, paragraphs, and tables.

    Args:
        path: DOCX path.
        title: Document title. Defaults to the file stem.

    Returns:
        Canonical document.
    """
    document = DocxDocument(str(path))
    blocks: list[Block] = []
    for para in document.paragraphs:
        text = normalize_text(para.text)
        if not text:
            continue
        style = (para.style.name or "") if para.style else ""
        if style.startswith("Heading"):
            try:
                level = int(style.replace("Heading", "").strip() or "1")
            except ValueError:
                level = 1
            blocks.append(Block(type="heading", text=text, level=level))
        else:
            blocks.append(Block(type="paragraph", text=text))
    for table in document.tables:
        headers = [_cell_text(cell) for cell in table.rows[0].cells] if table.rows else []
        body = []
        for row in table.rows[1:]:
            body.append([_cell_text(cell) for cell in row.cells])
        blocks.extend(table_blocks(headers, body, section_path=title or path.stem))
    return CanonicalDocument(
        title=title or path.stem,
        language="ja",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        blocks=blocks,
    )


def _cell_text(cell: object) -> str:
    return normalize_text(getattr(cell, "text", "") or "")


def parse_pptx(path: Path, title: str | None = None) -> CanonicalDocument:
    """Parse slide text into one paragraph block per non-empty slide.

    Args:
        path: PPTX path.
        title: Document title. Defaults to the file stem.

    Returns:
        Canonical document. ``page`` is the 1-based slide number.
    """
    presentation = Presentation(str(path))
    blocks: list[Block] = []
    for index, slide in enumerate(presentation.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text:
                texts.append(normalize_text(shape.text))
        if texts:
            blocks.append(Block(type="paragraph", text="\n".join(texts), page=index))
    return CanonicalDocument(
        title=title or path.stem,
        language="ja",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        blocks=blocks,
        extra={"slide_count": len(presentation.slides)},
    )


def parse_html(path: Path, title: str | None = None) -> CanonicalDocument:
    """Parse a local HTML file into headings, paragraphs, and tables.

    A remote URL is rejected. A local file whose text merely contains
    ``http://`` or ``https://`` is kept.

    Args:
        path: Local HTML path.
        title: Document title. Defaults to the file stem.

    Returns:
        Canonical document. Each ``table`` element becomes table blocks and
        its cells are not repeated as paragraphs.

    Raises:
        ValueError: If ``path`` itself is an ``http`` or ``https`` URL
            (``ERR_FILE_REMOTE_HTML``), or if the file cannot be decoded.
    """
    if _is_remote_html_path(path):
        raise ValueError("ERR_FILE_REMOTE_HTML")
    from db2rag.parsers import read_text_with_fallback

    raw = read_text_with_fallback(path)
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    doc_title = title or path.stem
    blocks: list[Block] = []
    section: list[str] = []
    for node in soup.find_all(_HTML_FLOW):
        name = str(node.name or "")
        # 表セルの p / li は表ブロックと二重にしない。
        if name != "table" and node.find_parent("table") is not None:
            continue
        if name == "table":
            if node.find_parent("table") is not None:
                continue
            headers, rows = _html_table(node)
            if not headers and not rows:
                continue
            blocks.extend(
                table_blocks(
                    headers,
                    rows,
                    section_path=">".join(section) or doc_title,
                )
            )
            continue
        text = normalize_text(node.get_text(" ", strip=True))
        if not text:
            continue
        if name in _HTML_HEADING:
            level = _HTML_HEADING[name]
            section[:] = section[: level - 1]
            section.append(text)
            blocks.append(Block(type="heading", text=text, level=level))
            continue
        blocks.append(Block(type="paragraph", text=text))
    if not blocks:
        return parse_txt_text(soup.get_text("\n"), title=doc_title, mime="text/html")
    return CanonicalDocument(title=doc_title, language="ja", mime="text/html", blocks=blocks)


def _is_remote_html_path(path: Path) -> bool:
    # 本文ではなくパス文字列だけを見る。Windows は // を崩す。
    normalized = str(path).strip().lower().replace("\\", "/")
    return normalized.startswith(_REMOTE_HTML_PREFIXES)


def _html_table(table: Any) -> tuple[list[str], list[list[str]]]:
    grid: list[list[str]] = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells: list[str] = []
        for cell in tr.find_all(["th", "td"]):
            if cell.find_parent("tr") is not tr:
                continue
            cells.append(normalize_text(cell.get_text(" ", strip=True)))
        if not any(cell.strip() for cell in cells):
            continue
        grid.append(cells)
    if not grid:
        return [], []
    return grid[0], grid[1:]
