"""BM25 相当の日本語トークン → Qdrant sparse。IDF は collection 側に任せ、ここでは log-TF。"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter

from qdrant_client.http.models import SparseVector


_ALNUM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./]*")
_JP = re.compile(r"[\u3040-\u30ff\u4e00-\u9faf]+")


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text)
    tokens: list[str] = []
    tokens.extend(tok.lower() for tok in _ALNUM.findall(text))
    for run in _JP.findall(text):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        if 2 <= len(run) <= 12:
            tokens.append(run)
    return tokens


def token_index(token: str) -> int:
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def to_sparse(text: str) -> SparseVector:
    counts = Counter(tokenize(text))
    merged: dict[int, float] = {}
    for token, count in counts.items():
        index = token_index(token)
        weight = 1.0 + math.log(count)
        merged[index] = merged.get(index, 0.0) + weight
    indices = sorted(merged)
    return SparseVector(indices=indices, values=[merged[i] for i in indices])
