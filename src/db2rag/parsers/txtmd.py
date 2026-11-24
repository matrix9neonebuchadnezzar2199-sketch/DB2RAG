"""TXT / Markdown 構造パーサ。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from db2rag.parsers.canonical import Block, CanonicalDocument
from db2rag.parsers.tables import table_blocks
from db2rag.textnorm import normalize_text

# commonmark に GFM 表だけ足す。表とコードは捨てない。
_MD = MarkdownIt("commonmark").enable("table")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_txt(path: Path, title: str | None = None) -> CanonicalDocument:
    """Read a plain-text file and split it into blocks.

    Args:
        path: Text file path. Encoding is detected with fallbacks.
        title: Document title. Defaults to the file stem.

    Returns:
        Canonical document with heading and paragraph blocks.

    Raises:
        ValueError: If the file cannot be decoded.
    """
    # parsers/__init__ からの循環 import を避ける。
    from db2rag.parsers import read_text_with_fallback

    text = read_text_with_fallback(path)
    return parse_txt_text(text, title=title or path.stem, mime="text/plain")


def parse_txt_text(text: str, *, title: str, mime: str) -> CanonicalDocument:
    """Split already-decoded text into heading and paragraph blocks.

    Args:
        text: Document body.
        title: Document title.
        mime: MIME type stored on the document.

    Returns:
        Canonical document. Blank paragraphs are omitted.
    """
    text = normalize_text(text)
    blocks: list[Block] = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for para in paragraphs:
        heading = _HEADING.match(para.split("\n", 1)[0])
        if heading and "\n" not in para:
            blocks.append(Block(type="heading", text=heading.group(2).strip(), level=len(heading.group(1))))
            continue
        first, _, rest = para.partition("\n")
        heading = _HEADING.match(first)
        if heading:
            blocks.append(Block(type="heading", text=heading.group(2).strip(), level=len(heading.group(1))))
            if rest.strip():
                blocks.append(Block(type="paragraph", text=rest.strip()))
        else:
            blocks.append(Block(type="paragraph", text=para))
    return CanonicalDocument(title=title, language="ja", mime=mime, blocks=blocks)


def parse_md(path: Path, title: str | None = None) -> CanonicalDocument:
    """Parse Markdown, keeping tables and fenced or indented code.

    Args:
        path: Markdown file path. Encoding falls back the same way as plain text.
        title: Document title. Defaults to the file stem.

    Returns:
        Canonical document. Tables are ``table`` blocks and code is ``code``.
    """
    from db2rag.parsers import read_text_with_fallback

    raw = read_text_with_fallback(path)
    doc_title = title or path.stem
    tokens = _MD.parse(raw)
    blocks: list[Block] = []
    section: list[tuple[int, str]] = []
    index = 0
    end = len(tokens)
    while index < end:
        token = tokens[index]
        if token.type == "heading_open":
            index = _append_heading(tokens, index, end, blocks, section)
            continue
        if token.type == "paragraph_open":
            index = _append_paragraph(tokens, index, end, blocks)
            continue
        if token.type in {"fence", "code_block"}:
            text = _code_text(token)
            if text.strip():
                blocks.append(Block(type="code", text=text))
            index += 1
            continue
        if token.type == "table_open":
            index = _append_table(tokens, index, end, blocks, section, doc_title)
            continue
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            index = _consume_list(tokens, index, end, blocks, section, doc_title)
            continue
        index += 1
    if not blocks:
        return parse_txt_text(raw, title=doc_title, mime="text/markdown")
    return CanonicalDocument(
        title=doc_title,
        language="ja",
        mime="text/markdown",
        blocks=blocks,
    )


def _append_heading(
    tokens: list[Any],
    index: int,
    end: int,
    blocks: list[Block],
    section: list[tuple[int, str]],
) -> int:
    token = tokens[index]
    tag = str(token.tag or "")
    level = int(tag[1]) if len(tag) > 1 and tag[1].isdigit() else 1
    text = ""
    cursor = index + 1
    while cursor < end and tokens[cursor].type != "heading_close":
        if tokens[cursor].type == "inline":
            text = str(tokens[cursor].content).strip()
        cursor += 1
    if cursor < end and tokens[cursor].type == "heading_close":
        cursor += 1
    if text:
        kept = [item for item in section if item[0] < level]
        section[:] = kept
        section.append((level, text))
        blocks.append(Block(type="heading", text=text, level=level))
    return cursor


def _append_paragraph(tokens: list[Any], index: int, end: int, blocks: list[Block]) -> int:
    text = ""
    cursor = index + 1
    while cursor < end and tokens[cursor].type != "paragraph_close":
        if tokens[cursor].type == "inline":
            text = str(tokens[cursor].content).strip()
        cursor += 1
    if cursor < end and tokens[cursor].type == "paragraph_close":
        cursor += 1
    if text:
        blocks.append(Block(type="paragraph", text=text))
    return cursor


def _code_text(token: Any) -> str:
    # 言語名があるフェンスだけ [lang] を前置する。
    info = str(getattr(token, "info", "") or "").strip()
    content = str(getattr(token, "content", "") or "")
    if content.endswith("\n"):
        content = content[:-1]
    if info:
        return f"[{info}]\n{content}"
    return content


def _section_path(section: list[tuple[int, str]], doc_title: str) -> str:
    joined = ">".join(text for _, text in section if text)
    return joined or doc_title


def _append_table(
    tokens: list[Any],
    index: int,
    end: int,
    blocks: list[Block],
    section: list[tuple[int, str]],
    doc_title: str,
) -> int:
    headers: list[str] = []
    rows: list[list[str]] = []
    current: list[str] | None = None
    in_header = False
    cell: str | None = None
    index += 1
    while index < end:
        token = tokens[index]
        kind = token.type
        if kind == "table_close":
            index += 1
            break
        if kind == "thead_open":
            in_header = True
        elif kind == "thead_close":
            in_header = False
        elif kind == "tr_open":
            current = []
            cell = None
        elif kind in {"th_open", "td_open"}:
            cell = ""
        elif kind == "inline" and cell is not None:
            cell = str(token.content).strip()
        elif kind in {"th_close", "td_close"}:
            if current is not None:
                current.append(cell or "")
            cell = None
        elif kind == "tr_close" and current is not None:
            if in_header and not headers:
                headers = current
            else:
                rows.append(current)
            current = None
        index += 1
    if headers or rows:
        blocks.extend(
            table_blocks(headers, rows, section_path=_section_path(section, doc_title))
        )
    return index


def _consume_list(
    tokens: list[Any],
    index: int,
    end: int,
    blocks: list[Block],
    section: list[tuple[int, str]],
    doc_title: str,
) -> int:
    # リストの中の表とコードもトップレベルと同じように残す。
    items: list[str] = []
    depth = 1
    index += 1
    while index < end and depth > 0:
        current = tokens[index]
        if current.type in {"bullet_list_open", "ordered_list_open"}:
            depth += 1
            index += 1
            continue
        if current.type in {"bullet_list_close", "ordered_list_close"}:
            depth -= 1
            index += 1
            continue
        if current.type in {"fence", "code_block"}:
            _flush_list_items(blocks, items)
            items = []
            text = _code_text(current)
            if text.strip():
                blocks.append(Block(type="code", text=text))
            index += 1
            continue
        if current.type == "table_open":
            _flush_list_items(blocks, items)
            items = []
            index = _append_table(tokens, index, end, blocks, section, doc_title)
            continue
        if current.type == "inline" and str(current.content).strip():
            items.append(str(current.content).strip())
        index += 1
    _flush_list_items(blocks, items)
    return index


def _flush_list_items(blocks: list[Block], items: list[str]) -> None:
    if not items:
        return
    blocks.append(Block(type="list", text="\n".join(f"- {item}" for item in items)))
