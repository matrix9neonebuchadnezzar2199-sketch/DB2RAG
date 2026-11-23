"""BM25 用の日本語トークン。IDF は Qdrant の Modifier.IDF に任せ、ここでは TF 飽和だけ行う。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter

from qdrant_client.http.models import SparseVector

from db2rag.constants import BM25_K1

# 助詞・機能語を 2-gram にすると型番より強く出てしまう。語全体がこれなら捨てる。
JA_STOPWORDS = frozenset(
    {
        "の",
        "は",
        "が",
        "を",
        "に",
        "で",
        "と",
        "も",
        "や",
        "へ",
        "から",
        "まで",
        "より",
        "など",
        "ため",
        "こと",
        "もの",
        "です",
        "ます",
        "する",
        "した",
        "して",
        "いる",
        "ある",
    }
)

_ALNUM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./]*")
_KANJI = re.compile(r"[\u4e00-\u9faf々]+")
_KATAKANA = re.compile(r"[\u30a0-\u30ffー]+")
_HIRAGANA = re.compile(r"[\u3040-\u309f]+")
_CODE_SPLIT = re.compile(r"[-_/.]+")
_WHOLE_WORD_MAX = 16


def tokenize(text: str) -> list[str]:
    """NFKC のあと、英数・漢字・カタカナ・ひらがなを別々にトークン化する。

    Args:
        text: 文書またはクエリ。空文字でもよい。

    Returns:
        出現順のトークン。ひらがなの機能語は含まない。同じ語の繰り返しは残す。
    """
    normalized = unicodedata.normalize("NFKC", text)
    tokens: list[str] = []
    tokens.extend(_alnum_tokens(normalized))
    for run in _KANJI.findall(normalized):
        tokens.extend(_word_and_bigrams(run, whole_max=_WHOLE_WORD_MAX))
    for run in _KATAKANA.findall(normalized):
        tokens.extend(_word_and_bigrams(run, whole_max=_WHOLE_WORD_MAX))
    for run in _HIRAGANA.findall(normalized):
        if run in JA_STOPWORDS or len(run) < 2:
            continue
        # ひらがなは機能語を除いた残りを語として残す。16 文字上限は漢字・カタカナ側。
        tokens.extend(_word_and_bigrams(run, whole_max=None))
    return tokens


def token_index(token: str) -> int:
    """トークンを Qdrant sparse の非負インデックスへ写す。

    Args:
        token: ``tokenize`` が返す 1 語。

    Returns:
        SHA-256 先頭 4 バイトの 31 ビット符号なし整数。
    """
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def to_sparse(text: str) -> SparseVector:
    """トークン頻度を BM25 の TF 飽和重みにした sparse ベクトルを返す。

    IDF は掛けない。同じハッシュに別トークンが衝突したときは重みを加算する。

    Args:
        text: 文書またはクエリ。

    Returns:
        インデックス昇順の ``SparseVector``。トークンが無ければ空。
    """
    counts = Counter(tokenize(text))
    merged: dict[int, float] = {}
    for token, count in counts.items():
        index = token_index(token)
        merged[index] = merged.get(index, 0.0) + _saturated_tf(count)
    indices = sorted(merged)
    return SparseVector(indices=indices, values=[merged[index] for index in indices])


def _alnum_tokens(text: str) -> list[str]:
    """英数の完全トークンと、区切りで割った 2 文字以上の部品を返す。"""
    tokens: list[str] = []
    for raw in _ALNUM.findall(text):
        full = raw.lower()
        tokens.append(full)
        # 区切りが無い語は完全形と部品が同一なので、TF を二重にしない。
        for part in _CODE_SPLIT.split(full):
            if len(part) >= 2 and part != full:
                tokens.append(part)
    return tokens


def _word_and_bigrams(run: str, *, whole_max: int | None) -> list[str]:
    """2 文字以上の語全体と、長さ 2 以上の 2-gram を返す。"""
    tokens: list[str] = []
    if len(run) >= 2 and (whole_max is None or len(run) <= whole_max):
        tokens.append(run)
    if len(run) >= 2:
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _saturated_tf(term_frequency: int) -> float:
    """BM25 の TF 項 ``(k1+1)*tf/(tf+k1)``。k1 は ``BM25_K1``。"""
    return (BM25_K1 + 1.0) * term_frequency / (term_frequency + BM25_K1)
