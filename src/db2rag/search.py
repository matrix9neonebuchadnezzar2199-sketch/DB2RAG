"""検索プレビュー。生成はしない。Reranker は渡されたときだけ使う。"""

from __future__ import annotations

import sqlite3
from typing import Any

from qdrant_client.http import models as qm

from db2rag.constants import DEFAULT_SCORE_THRESHOLD, DEFAULT_TOP_K, SPARSE_MIN_RRF_NORM
from db2rag.db import row_to_dict
from db2rag.embedding import Embedder
from db2rag.qdrant_store import QdrantStore
from db2rag.reranker import Reranker
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
    dedup_parent: bool = True,
    use_reranker: bool | None = None,
    reranker: Reranker | None = None,
) -> dict[str, Any]:
    """Hybrid 候補を閾値で残し、必要なら再順位付けと親の重複集約をする。

    Args:
        conn: SQLite 接続。
        store: Qdrant ストア。
        embedder: クエリ dense 化。
        rag_id: 対象 RAG。
        query: 検索文。
        top_k: 返す代表ヒット数。
        score_threshold: hybrid では dense 下限。再順位付け時は sigmoid スコアの下限。
        acl_tags: 指定時だけ acl を絞る。
        source_types: 指定時だけ source を絞る。
        expand_parent: True なら親本文を付ける。
        hybrid: False なら dense のみ。
        dedup_parent: True なら同じ ``parent_chunk_id`` を 1 件にまとめる。
        use_reranker: None は ``reranker.enabled`` に従う。False は再順位付けしない。
        reranker: 無効、または未指定ならモデルをロードしない。

    Returns:
        ``hits`` を含む検索結果。``score`` は RRF 正規化値、再順位付け時は sigmoid 値。

    Raises:
        ValueError: RAG が無いとき。コードは ``ERR_RAG_NOT_FOUND``。
        RuntimeError: Reranker のスコア件数が候補と違うとき。
    """
    rag = row_to_dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    if rag is None:
        raise ValueError("ERR_RAG_NOT_FOUND")
    alias = str(rag["qdrant_alias"])
    query_text = normalize_text(query)
    dense = embedder.encode_queries([query_text])[0].tolist()
    query_filter = _query_filter(rag_id, acl_tags, source_types)
    # 閾値で落ちる分を見越して、表示件数より多めに取る。
    fetch_limit = max(top_k * 3, 30)
    raw = store.hybrid_search(
        collection=alias,
        dense=dense,
        query_text=query_text,
        limit=fetch_limit,
        query_filter=query_filter,
        hybrid=hybrid,
    )
    prepared = _prepare_candidates(conn, raw, expand_parent=expand_parent)
    active = reranker if _should_rerank(reranker, use_reranker) else None
    if active is not None:
        prepared = _apply_reranker(active, query_text, prepared, score_threshold)
    else:
        prepared = [item for item in prepared if _keep_hit(item, score_threshold, hybrid)]
    hits = _limit_hits(
        prepared,
        top_k=top_k,
        dedup_parent=dedup_parent,
        query_text=query_text,
    )
    return {
        "rag_id": rag_id,
        "query": query_text,
        "hits": hits,
        "note": "score は類似度であり確率ではありません",
        "hybrid": hybrid,
        "alias": alias,
    }


def _query_filter(
    rag_id: str,
    acl_tags: list[str] | None,
    source_types: list[str] | None,
) -> qm.Filter:
    must: list[qm.Condition] = [
        qm.FieldCondition(key="rag_id", match=qm.MatchValue(value=rag_id)),
    ]
    if acl_tags:
        must.append(qm.FieldCondition(key="acl_tag", match=qm.MatchAny(any=acl_tags)))
    if source_types:
        must.append(qm.FieldCondition(key="source_type", match=qm.MatchAny(any=source_types)))
    return qm.Filter(must=must)


def _should_rerank(reranker: Reranker | None, use_reranker: bool | None) -> bool:
    """有効かつ use_reranker が False でないときだけ再順位付けする。"""
    return reranker is not None and use_reranker is not False and reranker.enabled


def _prepare_candidates(
    conn: sqlite3.Connection,
    raw: list[dict[str, Any]],
    *,
    expand_parent: bool,
) -> list[dict[str, Any]]:
    parent_cache: dict[str, str | None] = {}
    prepared: list[dict[str, Any]] = []
    for hit in raw:
        payload = dict(hit.get("payload") or {})
        child_id = _as_optional_str(payload.get("child_chunk_id"))
        parent_id = _as_optional_str(payload.get("parent_chunk_id"))
        prepared.append(
            {
                "score": _as_float(hit.get("score")),
                "rrf_score": _as_float(hit.get("score")),
                "raw_rrf": hit.get("raw_rrf"),
                "dense_score": _as_float(hit.get("dense_score")),
                "sparse_score": _as_float(hit.get("sparse_score")),
                "sparse_rank": hit.get("sparse_rank"),
                "payload": payload,
                "child_text": _child_text(conn, child_id),
                "parent_text": _parent_text(conn, parent_id, expand_parent, parent_cache),
            }
        )
    return prepared


def _keep_hit(item: dict[str, Any], score_threshold: float, hybrid: bool) -> bool:
    """dense 比較は負の閾値でもそのまま行う。BM25 単独は通常閾値だけ救済する。"""
    dense_score = _as_float(item.get("dense_score"))
    if not hybrid:
        return dense_score >= score_threshold
    if dense_score >= score_threshold:
        return True
    sparse_rank = item.get("sparse_rank")
    if not isinstance(sparse_rank, int):
        return False
    # 0.80 以上はノイズ遮断。dense が弱すぎる BM25 単独は通さない。
    if score_threshold >= 0.80:
        return sparse_rank <= 3 and dense_score >= score_threshold * 0.65
    return (
        sparse_rank <= 10
        and _as_float(item.get("sparse_score")) > 0.0
        and _as_float(item.get("rrf_score")) >= SPARSE_MIN_RRF_NORM
    )


def _apply_reranker(
    reranker: Reranker,
    query_text: str,
    prepared: list[dict[str, Any]],
    score_threshold: float,
) -> list[dict[str, Any]]:
    """上位 top_n を sigmoid スコアで並べ、そのスコアで閾値を掛ける。"""
    chosen = prepared[: max(reranker.top_n, 0)]
    if not chosen:
        return []
    scores = reranker.score(query_text, [str(item["child_text"]) for item in chosen])
    if len(scores) != len(chosen):
        raise RuntimeError(f"reranker returned {len(scores)} scores for {len(chosen)} passages")
    kept: list[dict[str, Any]] = []
    for item, value in zip(chosen, scores, strict=True):
        calibrated = float(value)
        item["rerank_score"] = calibrated
        if calibrated >= score_threshold:
            kept.append(item)
    kept.sort(key=lambda item: float(item["rerank_score"]), reverse=True)
    return kept


def _limit_hits(
    prepared: list[dict[str, Any]],
    *,
    top_k: int,
    dedup_parent: bool,
    query_text: str,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    grouped: dict[str, dict[str, Any]] = {}
    for item in prepared:
        parent_key = _as_optional_str(item["payload"].get("parent_chunk_id")) or ""
        if dedup_parent and parent_key and parent_key in grouped:
            # 同じ親は枠を増やさない。順位が後の子も matched_children に残す。
            grouped[parent_key]["matched_children"].append(_child_ref(item))
            continue
        if len(hits) >= top_k:
            if dedup_parent:
                continue
            break
        hit = _public_hit(item, rank=len(hits) + 1, query_text=query_text)
        if dedup_parent:
            # 代表自身も含める。子がヒットのフィールドだけに隠れないようにする。
            hit["matched_children"] = [_child_ref(item)]
            if parent_key:
                grouped[parent_key] = hit
        hits.append(hit)
    return hits


def _public_hit(item: dict[str, Any], *, rank: int, query_text: str) -> dict[str, Any]:
    payload = item["payload"]
    child_text = str(item["child_text"])
    display = item["rerank_score"] if "rerank_score" in item else item["rrf_score"]
    hit: dict[str, Any] = {
        "rank": rank,
        "score": round(float(display), 4),
        "dense_score": round(_as_float(item.get("dense_score")), 4),
        "title": payload.get("title"),
        "section_path": payload.get("section_path"),
        "source_type": payload.get("source_type"),
        "acl_tag": payload.get("acl_tag"),
        "child_chunk_id": payload.get("child_chunk_id"),
        "parent_chunk_id": payload.get("parent_chunk_id"),
        "snippet": child_text,
        "highlight": highlight_html(child_text, query_text),
        "parent_text": item["parent_text"] if item["parent_text"] is not None else None,
        "record_id": payload.get("record_id"),
        "sparse_score": round(_as_float(item.get("sparse_score")), 4),
        "sparse_rank": item.get("sparse_rank"),
        "rrf_score": round(_as_float(item.get("rrf_score")), 4),
    }
    if "rerank_score" in item:
        hit["rerank_score"] = round(float(item["rerank_score"]), 4)
    if item.get("raw_rrf") is not None:
        hit["raw_rrf"] = item["raw_rrf"]
    return hit


def _child_ref(item: dict[str, Any]) -> dict[str, Any]:
    display = item["rerank_score"] if "rerank_score" in item else item["rrf_score"]
    return {
        "child_chunk_id": item["payload"].get("child_chunk_id"),
        "score": round(float(display), 4),
        "dense_score": round(_as_float(item.get("dense_score")), 4),
        "snippet": str(item["child_text"]),
    }


def _parent_text(
    conn: sqlite3.Connection,
    parent_id: str | None,
    expand_parent: bool,
    cache: dict[str, str | None],
) -> str | None:
    if not expand_parent or not parent_id:
        return None
    if parent_id not in cache:
        row = conn.execute(
            "SELECT text FROM parent_chunks WHERE parent_chunk_id=?",
            (parent_id,),
        ).fetchone()
        cache[parent_id] = str(row["text"]) if row else None
    return cache[parent_id]


def _child_text(conn: sqlite3.Connection, child_id: str | None) -> str:
    if not child_id:
        return ""
    row = conn.execute(
        "SELECT text FROM child_chunks WHERE child_chunk_id=?",
        (child_id,),
    ).fetchone()
    return str(row["text"]) if row else ""


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
