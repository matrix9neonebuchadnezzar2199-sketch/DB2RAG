"""Parent-Child chunker。文書種別パラメータは設計書 05。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from db2rag.constants import MAX_CHILD_CHARS, TOKEN_CHAR_RATIO
from db2rag.ids import new_id, point_id_for_child
from db2rag.parsers.canonical import CanonicalDocument
from db2rag.textnorm import normalize_section_path, normalize_text

HEADING_RE = re.compile(r"^(第[0-9一二三四五六七八九十百]+[章節款条]|#{1,6}\s+)")


@dataclass
class ChunkParams:
    child_target: int
    child_min: int
    child_max: int
    overlap: int
    parent_max: int


PARAMS = {
    "regulation": ChunkParams(400, 200, 600, 80, 8000),
    "manual": ChunkParams(450, 250, 700, 90, 10000),
    "minutes": ChunkParams(350, 150, 550, 60, 6000),
    "faq": ChunkParams(300, 100, 450, 40, 2000),
    "mysql": ChunkParams(380, 150, 550, 0, 4000),
    "table": ChunkParams(250, 80, 400, 0, 1500),
    "pdf_table": ChunkParams(350, 150, 500, 50, 5000),
    "docx": ChunkParams(420, 200, 650, 85, 9000),
    "txtmd": ChunkParams(400, 200, 600, 80, 8000),
}


@dataclass
class ParentChunk:
    parent_chunk_id: str
    parent_index: int
    text: str
    section_path: str
    page_from: int | None
    page_to: int | None
    token_estimate: int


@dataclass
class ChildChunk:
    child_chunk_id: str
    parent_chunk_id: str
    child_index: int
    text: str
    char_count: int
    qdrant_point_id: str


@dataclass
class ChunkResult:
    parents: list[ParentChunk]
    children: list[ChildChunk]


def infer_doc_kind(mime: str, extra: dict | None = None) -> str:
    extra = extra or {}
    if extra.get("kind"):
        return str(extra["kind"])
    if mime in {"text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}:
        return "table"
    if mime == "application/pdf":
        return "regulation"
    if mime.endswith("wordprocessingml.document"):
        return "docx"
    if mime in {"text/markdown", "text/plain"}:
        return "txtmd"
    if extra.get("source_type") == "mysql":
        return "mysql"
    return "txtmd"


def chunk_document(document: CanonicalDocument, kind: str | None = None) -> ChunkResult:
    params = PARAMS[kind or infer_doc_kind(document.mime, document.extra)]
    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []
    section_parts: list[str] = [document.title]
    current_texts: list[str] = []
    pages: list[int] = []
    parent_index = 0

    def flush_parent() -> None:
        nonlocal parent_index, current_texts, pages
        text = normalize_text("\n\n".join(current_texts))
        if not text:
            current_texts = []
            pages = []
            return
        for part_text, suffix in _split_parent(text, params.parent_max):
            parent_id = new_id("par")
            section = normalize_section_path(">".join(section_parts))
            if suffix:
                section = f"{section}{suffix}"
            page_from = min(pages) if pages else None
            page_to = max(pages) if pages else None
            parent = ParentChunk(
                parent_chunk_id=parent_id,
                parent_index=parent_index,
                text=part_text,
                section_path=section,
                page_from=page_from,
                page_to=page_to,
                token_estimate=max(1, int(len(part_text) / TOKEN_CHAR_RATIO)),
            )
            parents.append(parent)
            child_texts = _split_child(part_text, params)
            for child_index, child_text in enumerate(child_texts):
                child_id = new_id("chk")
                children.append(
                    ChildChunk(
                        child_chunk_id=child_id,
                        parent_chunk_id=parent_id,
                        child_index=child_index,
                        text=child_text,
                        char_count=len(child_text),
                        qdrant_point_id=point_id_for_child(child_id),
                    )
                )
            parent_index += 1
        current_texts = []
        pages = []

    for block in document.blocks:
        if block.type == "heading" and block.text.strip():
            flush_parent()
            level = max(1, min(block.level or 1, 6))
            section_parts[:] = section_parts[:level]
            if len(section_parts) < level:
                section_parts.extend([document.title] * (level - len(section_parts)))
            if len(section_parts) == level:
                section_parts.append(block.text.strip())
            else:
                section_parts[level] = block.text.strip()
                del section_parts[level + 1 :]
            current_texts.append(block.text.strip())
            if block.page:
                pages.append(block.page)
            continue
        current_texts.append(block.text)
        if block.page:
            pages.append(block.page)
        joined = "\n\n".join(current_texts)
        if len(joined) >= params.parent_max:
            flush_parent()

    flush_parent()
    if not parents:
        text = normalize_text(document.plain_text())
        if text:
            current_texts = [text]
            flush_parent()
    return ChunkResult(parents=parents, children=children)


def _split_parent(text: str, parent_max: int) -> list[tuple[str, str]]:
    if len(text) <= parent_max:
        return [(text, "")]
    parts: list[tuple[str, str]] = []
    start = 0
    index = 1
    while start < len(text):
        end = min(len(text), start + parent_max)
        parts.append((text[start:end], "" if index == 1 else f"-part{index}"))
        start = end
        index += 1
    return parts


def _split_child(text: str, params: ChunkParams) -> list[str]:
    if len(text) <= params.child_max:
        if len(text) < params.child_min and params.overlap == 0:
            return [text]
        if len(text) <= params.child_target:
            return [text]
    segments = [seg.strip() for seg in re.split(r"\n\s*\n", text) if seg.strip()]
    if not segments:
        segments = [text]
    chunks: list[str] = []
    current = ""
    for segment in segments:
        candidate = f"{current}\n\n{segment}".strip() if current else segment
        if len(candidate) <= params.child_target:
            current = candidate
            continue
        if current:
            chunks.extend(_cut_overflow(current, params))
            current = segment
        else:
            chunks.extend(_cut_overflow(segment, params))
            current = ""
    if current:
        chunks.extend(_cut_overflow(current, params))
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < params.child_min and params.overlap > 0:
            merged[-1] = f"{merged[-1]}\n{chunk}"
        else:
            merged.append(chunk)
    if params.overlap > 0 and len(merged) > 1:
        overlapped = [merged[0]]
        for index in range(1, len(merged)):
            prev_tail = merged[index - 1][-params.overlap :]
            overlapped.append((prev_tail + merged[index])[:MAX_CHILD_CHARS])
        merged = overlapped
    return [item[:MAX_CHILD_CHARS] for item in merged if item.strip()]


def _cut_overflow(text: str, params: ChunkParams) -> list[str]:
    if len(text) <= params.child_max:
        return [text]
    pieces: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= params.child_max:
            pieces.append(remaining)
            break
        window = remaining[: params.child_max]
        split_at = _boundary(window)
        pieces.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return [p for p in pieces if p]


def _boundary(window: str) -> int:
    for mark in ("。", "．", "！", "？", ".", "!", "?"):
        pos = window.rfind(mark)
        if pos >= 80:
            return pos + 1
    for mark in ("、", "，", ","):
        pos = window.rfind(mark)
        if pos >= 80:
            return pos + 1
    return len(window)
