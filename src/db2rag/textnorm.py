"""テキスト正規化とハッシュ。payload の content_hash と一致させる。"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_section_path(path: str) -> str:
    path = unicodedata.normalize("NFKC", path)
    path = path.replace("＞", ">")
    parts = [part.strip() for part in path.split(">") if part.strip()]
    return ">".join(parts)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | bytes) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def highlight_html(text: str, query: str) -> str:
    """Escape snippet text then wrap the first query token in <em>."""
    if not text:
        return ""
    clip = text if len(text) <= 400 else text[:400]
    escaped = html.escape(clip)
    tokens = [query] + [part for part in query.replace("？", "").replace("?", "").split() if len(part) >= 2]
    for token in tokens:
        marked = html.escape(token)
        if marked and marked in escaped:
            return escaped.replace(marked, f"<em>{marked}</em>", 1)
    return escaped
