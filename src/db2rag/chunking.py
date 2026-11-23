"""Parent-Child chunker。文書種別パラメータは設計書 05。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from db2rag.constants import MAX_CHILD_CHARS, TOKEN_CHAR_RATIO
from db2rag.ids import new_id, point_id_for_child
from db2rag.parsers.canonical import CanonicalDocument
from db2rag.textnorm import normalize_section_path, normalize_text

# PDF の章・条分割は parsers/office.py。この式は既存参照のために残す。
HEADING_RE = re.compile(r"^(第[0-9一二三四五六七八九十百]+[章節款条]|#{1,6}\s+)")
_OVERLAP_BOUNDARY = frozenset("\n。．！？")


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


def infer_doc_kind(mime: str, extra: dict[str, Any] | None = None) -> str:
    """Choose chunk parameters for a document.

    Args:
        mime: MIME type of the source document.
        extra: Optional hints. ``kind`` forces a parameter set. A
            ``source_type`` of ``mysql`` is resolved before any MIME rule.

    Returns:
        Key of ``PARAMS``.
    """
    extra = extra or {}
    if extra.get("kind"):
        return str(extra["kind"])
    # MIME より先。text/plain や csv の MySQL 行を txtmd / table にしない。
    if extra.get("source_type") == "mysql":
        return "mysql"
    if mime in {"text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}:
        return "table"
    if mime == "application/pdf":
        return "regulation"
    if mime.endswith("wordprocessingml.document"):
        return "docx"
    if mime in {"text/markdown", "text/plain"}:
        return "txtmd"
    return "txtmd"


def chunk_document(document: CanonicalDocument, kind: str | None = None) -> ChunkResult:
    """Split a canonical document into parent and child chunks.

    Args:
        document: Parsed document.
        kind: Parameter key. When omitted, ``infer_doc_kind`` chooses it.

    Returns:
        Parent chunks and their child chunks. Children after the first in a
        split parent are prefixed with the section path when it fits.
    """
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
            child_texts = _prefix_later_children(_split_child(part_text, params), section)
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
        merged = _apply_overlap(merged, params.overlap)
    else:
        merged = [_limit_child(item) for item in merged]
    return [item for item in merged if item.strip()]


def _limit_child(text: str) -> str:
    # 本体だけで上限を超えるときだけ切る。重なりでは使わない。
    if len(text) <= MAX_CHILD_CHARS:
        return text
    return text[:MAX_CHILD_CHARS]


def _overlap_tail(prev: str, overlap: int) -> str:
    # 窓内の最初の改行・句点の直後から取る。途中の文を先頭にしない。
    if overlap <= 0 or not prev:
        return ""
    tail = prev[-overlap:]
    for index, char in enumerate(tail):
        if char not in _OVERLAP_BOUNDARY:
            continue
        if index + 1 >= len(tail):
            return tail
        trimmed = tail[index + 1 :].lstrip(" \t")
        return trimmed or tail
    return tail


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    # はみ出す分は接頭辞だけ縮める。新しい本体の末尾は残す。
    bodies = [_limit_child(chunk) for chunk in chunks]
    if overlap <= 0 or len(bodies) <= 1:
        return bodies
    overlapped = [bodies[0]]
    for index in range(1, len(bodies)):
        body = bodies[index]
        if len(body) >= MAX_CHILD_CHARS:
            overlapped.append(body)
            continue
        available = MAX_CHILD_CHARS - len(body)
        prefix = _overlap_tail(bodies[index - 1], min(overlap, available))
        if len(prefix) > available:
            prefix = prefix[-available:]
        overlapped.append(prefix + body)
    return overlapped


def _prefix_later_children(child_texts: list[str], section: str) -> list[str]:
    # 2 件目以降だけ節パスを足す。入らないときは本体を切らずに諦める。
    if len(child_texts) <= 1 or not section:
        return child_texts
    label = f"【{section}】\n"
    prefixed = [child_texts[0]]
    for child_text in child_texts[1:]:
        head = child_text.lstrip()
        probe = head[: len(label) + len(section)]
        if section in probe or len(label) + len(child_text) > MAX_CHILD_CHARS:
            prefixed.append(child_text)
            continue
        prefixed.append(label + child_text)
    return prefixed


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
