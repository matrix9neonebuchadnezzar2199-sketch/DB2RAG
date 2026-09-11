"""TXT / Markdown 構造パーサ。"""

from __future__ import annotations

import re
from pathlib import Path

from markdown_it import MarkdownIt

from db2rag.parsers.canonical import Block, CanonicalDocument
from db2rag.textnorm import normalize_text

_MD = MarkdownIt("commonmark")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_txt(path: Path, title: str | None = None) -> CanonicalDocument:
    text = path.read_text(encoding="utf-8-sig")
    return parse_txt_text(text, title=title or path.stem, mime="text/plain")


def parse_txt_text(text: str, *, title: str, mime: str) -> CanonicalDocument:
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
    raw = path.read_text(encoding="utf-8-sig")
    tokens = _MD.parse(raw)
    blocks: list[Block] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open":
            level = int(token.tag[1])
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            text = inline.content if inline is not None else ""
            blocks.append(Block(type="heading", text=text.strip(), level=level))
            index += 3
            continue
        if token.type == "paragraph_open":
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            text = inline.content if inline is not None else ""
            if text.strip():
                blocks.append(Block(type="paragraph", text=text.strip()))
            index += 3
            continue
        if token.type == "bullet_list_open" or token.type == "ordered_list_open":
            items: list[str] = []
            depth = 1
            index += 1
            while index < len(tokens) and depth > 0:
                current = tokens[index]
                if current.type in {"bullet_list_open", "ordered_list_open"}:
                    depth += 1
                elif current.type in {"bullet_list_close", "ordered_list_close"}:
                    depth -= 1
                elif current.type == "inline" and current.content.strip():
                    items.append(current.content.strip())
                index += 1
            if items:
                blocks.append(Block(type="list", text="\n".join(f"- {item}" for item in items)))
            continue
        index += 1
    if not blocks:
        return parse_txt_text(raw, title=title or path.stem, mime="text/markdown")
    return CanonicalDocument(
        title=title or path.stem,
        language="ja",
        mime="text/markdown",
        blocks=blocks,
    )
