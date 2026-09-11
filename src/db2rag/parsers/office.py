"""PDF / DOCX / PPTX / HTML。Docling は任意。無いときは専用ライブラリ。"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from pptx import Presentation
from pypdf import PdfReader

from db2rag.parsers.canonical import Block, CanonicalDocument
from db2rag.parsers.txtmd import parse_txt_text
from db2rag.textnorm import normalize_text


def parse_pdf(path: Path, title: str | None = None) -> CanonicalDocument:
    reader = PdfReader(str(path))
    blocks: list[Block] = []
    low_quality_pages = 0
    for number, page in enumerate(reader.pages, start=1):
        extracted = page.extract_text() or ""
        text = normalize_text(extracted)
        if len(text) < 50:
            low_quality_pages += 1
        if text:
            blocks.append(Block(type="paragraph", text=text, page=number))
    if not blocks:
        raise ValueError("ERR_PDF_NO_TEXT")
    return CanonicalDocument(
        title=title or path.stem,
        language="ja",
        mime="application/pdf",
        blocks=blocks,
        extra={"page_count": len(reader.pages), "low_quality_pages": low_quality_pages},
    )


def parse_docx(path: Path, title: str | None = None) -> CanonicalDocument:
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
        from db2rag.parsers.tables import table_blocks

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
    raw = path.read_text(encoding="utf-8-sig")
    if re_remote_ref(raw):
        raise ValueError("ERR_HTML_REMOTE")
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    heading_tags = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    blocks: list[Block] = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]):
        text = normalize_text(node.get_text(" ", strip=True))
        if not text:
            continue
        if node.name in heading_tags:
            blocks.append(Block(type="heading", text=text, level=heading_tags[node.name]))
        else:
            blocks.append(Block(type="paragraph", text=text))
    if not blocks:
        return parse_txt_text(soup.get_text("\n"), title=title or path.stem, mime="text/html")
    return CanonicalDocument(title=title or path.stem, language="ja", mime="text/html", blocks=blocks)


def re_remote_ref(raw: str) -> bool:
    lowered = raw.lower()
    return "http://" in lowered or "https://" in lowered
