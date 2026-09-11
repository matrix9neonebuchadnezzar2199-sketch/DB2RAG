"""検索プレビュー。Reranker なし、生成なし。"""

from __future__ import annotations

import sqlite3
from typing import Any

from qdrant_client.http import models as qm

from db2rag.constants import DEFAULT_SCORE_THRESHOLD, DEFAULT_TOP_K
from db2rag.db import row_to_dict
from db2rag.embedding import Embedder
from db2rag.qdrant_store import QdrantStore
from db2rag.textnorm import highlight_html, normalize_text


def search(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    *,
    rag_id: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    acl_tags: list[str] | None = None,
    source_types: list[str] | None = None,
    expand_parent: bool = True,
    hybrid: bool = True,
) -> dict[str, Any]:
    rag = row_to_dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    if rag is None:
        raise ValueError("ERR_RAG_NOT_FOUND")
    alias = rag["qdrant_alias"]
    query_text = normalize_text(query)
    dense = embedder.encode_queries([query_text])[0].tolist()
    must: list[qm.FieldCondition] = [
        qm.FieldCondition(key="rag_id", match=qm.MatchValue(value=rag_id)),
    ]
    if acl_tags:
        must.append(qm.FieldCondition(key="acl_tag", match=qm.MatchAny(any=acl_tags)))
    if source_types:
        must.append(qm.FieldCondition(key="source_type", match=qm.MatchAny(any=source_types)))
    query_filter = qm.Filter(must=must)
    raw = store.hybrid_search(
        collection=alias,
        dense=dense,
        query_text=query_text,
        limit=top_k,
        query_filter=query_filter,
        hybrid=hybrid,
    )
    hits = []
    for hit in raw:
        payload = hit["payload"]
        dense_score = float(hit.get("dense_score") or 0.0)
        # RRF 正規化スコアと dense cosine の両方を見る。threshold は設計どおり cosine 下限
        if dense_score < score_threshold:
            continue
        parent_text = None
        if expand_parent:
            parent = conn.execute(
                "SELECT text FROM parent_chunks WHERE parent_chunk_id=?",
                (payload.get("parent_chunk_id"),),
            ).fetchone()
            parent_text = parent["text"] if parent else None
        snippet = payload.get("section_path") or ""
        child_text = _child_text(conn, payload.get("child_chunk_id"))
        hits.append(
            {
                "rank": len(hits) + 1,
                "score": round(float(hit["score"]), 4),
                "dense_score": round(dense_score, 4),
                "title": payload.get("title"),
                "section_path": payload.get("section_path"),
                "source_type": payload.get("source_type"),
                "acl_tag": payload.get("acl_tag"),
                "child_chunk_id": payload.get("child_chunk_id"),
                "parent_chunk_id": payload.get("parent_chunk_id"),
                "snippet": child_text,
                "highlight": highlight_html(child_text or "", query_text),
                "parent_text": parent_text if expand_parent else None,
                "record_id": payload.get("record_id"),
            }
        )
        if len(hits) >= top_k:
            break
    return {
        "rag_id": rag_id,
        "query": query_text,
        "hits": hits,
        "note": "score は類似度であり確率ではありません",
        "hybrid": hybrid,
        "alias": alias,
    }


def _child_text(conn: sqlite3.Connection, child_id: str | None) -> str:
    if not child_id:
        return ""
    row = conn.execute("SELECT text FROM child_chunks WHERE child_chunk_id=?", (child_id,)).fetchone()
    return row["text"] if row else ""
