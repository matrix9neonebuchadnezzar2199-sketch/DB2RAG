"""Phase 2: トークン、絶対スケール RRF、BM25 救済、親の集約、Reranker。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from qdrant_client.http import models as qm

from db2rag.config import Settings
from db2rag.constants import BM25_K1
from db2rag.db import connect, migrate
from db2rag.qdrant_store import QdrantStore
from db2rag.reranker import Reranker
from db2rag.search import search
from db2rag.sparse import to_sparse, token_index, tokenize


def test_sparse_tokenizer_ja_and_codes() -> None:
    tokens = tokenize("東京サーバの障害です。申請から承認。コード ERR-4092-XJ")
    assert "の" not in tokens
    assert "です" not in tokens
    assert "から" not in tokens
    assert "東京" in tokens
    assert "サーバ" in tokens
    assert "障害" in tokens
    assert "京サ" not in tokens
    assert "err-4092-xj" in tokens
    assert "err" in tokens
    assert "4092" in tokens
    assert "xj" in tokens
    wide = tokenize("ＥＲＲ－４０９２－ＸＪ")
    assert "err-4092-xj" in wide
    assert "err" in wide
    assert "4092" in wide

    vector = to_sparse("ERR-4092-XJ")
    index = token_index("err-4092-xj")
    expected = (BM25_K1 + 1.0) * 1 / (1.0 + BM25_K1)
    position = list(vector.indices).index(index)
    assert vector.values[position] == pytest.approx(expected)
    assert token_index("の") not in set(to_sparse("障害のです").indices)


def test_bm25_only_hit_survives_default_threshold(tmp_path: Path) -> None:
    conn = _seed(
        tmp_path,
        [
            ("bm25", "parent-bm25", "型番 ERR-4092-XJ の手順"),
            ("weak", "parent-weak", "無関係な本文"),
        ],
    )
    hits = [
        _raw(
            "bm25",
            "parent-bm25",
            score=0.5,
            dense_score=0.0,
            sparse_score=2.5,
            sparse_rank=1,
        ),
        _raw("weak", "parent-weak", score=0.2, dense_score=0.2, sparse_rank=None),
    ]
    store = _FakeStore(hits)
    found = search(
        conn,
        store,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        rag_id="rag1",
        query="ERR-4092-XJ",
        top_k=10,
        score_threshold=0.60,
    )
    assert store.limit == 30
    assert [hit["child_chunk_id"] for hit in found["hits"]] == ["bm25"]
    assert "ERR-4092-XJ" in found["hits"][0]["snippet"]

    blocked = search(
        conn,
        store,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        rag_id="rag1",
        query="ERR-4092-XJ",
        top_k=10,
        score_threshold=0.85,
    )
    assert blocked["hits"] == []


def test_negative_threshold_keeps_negative_dense(tmp_path: Path) -> None:
    conn = _seed(tmp_path, [("neg", "parent-neg", "負の dense")])
    hits = [_raw("neg", "parent-neg", score=-0.4, dense_score=-0.4, sparse_rank=None)]
    store = _FakeStore(hits)
    kept = search(
        conn,
        store,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        rag_id="rag1",
        query="負",
        score_threshold=-0.5,
    )
    assert [hit["child_chunk_id"] for hit in kept["hits"]] == ["neg"]
    dropped = search(
        conn,
        store,  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        rag_id="rag1",
        query="負",
        score_threshold=-0.3,
    )
    assert dropped["hits"] == []


def test_rrf_score_not_pinned_to_one() -> None:
    only = _Point("only-sparse", 4.0, {"title": "型番"})
    batch = _BatchClient(dense=[], sparse=[only])
    hits = _hybrid(batch)
    assert batch.calls == 1
    assert len(hits) == 1
    assert hits[0]["score"] <= 0.5
    assert hits[0]["score"] == pytest.approx(0.5)
    assert hits[0]["dense_score"] == 0.0
    assert hits[0]["sparse_rank"] == 1
    assert hits[0]["sparse_score"] == 4.0
    assert hits[0]["raw_rrf"] == pytest.approx(1.0 / 61.0)
    assert set(hits[0]) >= {
        "id",
        "score",
        "raw_rrf",
        "dense_score",
        "sparse_score",
        "sparse_rank",
        "payload",
    }

    both = _Point("both", 0.9, {"title": "両方"})
    paired = _hybrid(_BatchClient(dense=[both], sparse=[both]))
    assert paired[0]["score"] == pytest.approx(1.0)

    split = _SplitClient(dense=[], sparse=[only])
    fallback = _hybrid(split)
    assert split.calls == ["dense", "sparse"]
    assert fallback[0]["score"] <= 0.5
    assert fallback[0]["score"] == pytest.approx(0.5)


def test_parent_dedup_aggregates_children(tmp_path: Path) -> None:
    conn = _seed(
        tmp_path,
        [
            ("c1", "parent-1", "子一"),
            ("c2", "parent-1", "子二"),
            ("c3", "parent-1", "子三"),
        ],
    )
    raw = [
        _raw("c1", "parent-1", score=0.9, dense_score=0.95),
        _raw("c2", "parent-1", score=0.8, dense_score=0.9),
        _raw("c3", "parent-1", score=0.7, dense_score=0.85),
    ]
    found = search(
        conn,
        _FakeStore(raw),  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        rag_id="rag1",
        query="子",
        top_k=10,
        score_threshold=0.60,
        dedup_parent=True,
    )
    assert len(found["hits"]) == 1
    hit = found["hits"][0]
    assert hit["child_chunk_id"] == "c1"
    assert [child["child_chunk_id"] for child in hit["matched_children"]] == ["c1", "c2", "c3"]
    assert hit["matched_children"][1]["snippet"] == "子二"
    assert set(hit["matched_children"][0]) >= {"child_chunk_id", "score", "dense_score", "snippet"}


def test_reranker_reorders_hits(tmp_path: Path) -> None:
    conn = _seed(
        tmp_path,
        [
            ("alpha", "parent-a", "アルファの本文"),
            ("beta", "parent-b", "ベータの本文"),
        ],
    )
    raw = [
        _raw("alpha", "parent-a", score=0.9, dense_score=0.9),
        _raw("beta", "parent-b", score=0.4, dense_score=0.4),
    ]
    seen: list[list[str]] = []

    def scorer(query: str, passages: list[str]) -> list[float]:
        assert query
        seen.append(passages)
        return [4.0 if "ベータ" in text else -4.0 for text in passages]

    reranker = Reranker(True, "fake", tmp_path / "models", top_n=30, scorer=scorer)
    filtered = search(
        conn,
        _FakeStore(raw),  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        rag_id="rag1",
        query="手順",
        score_threshold=0.60,
        use_reranker=True,
        reranker=reranker,
    )
    assert [hit["child_chunk_id"] for hit in filtered["hits"]] == ["beta"]
    assert filtered["hits"][0]["rerank_score"] == pytest.approx(round(_sigmoid(4.0), 4))
    assert filtered["hits"][0]["score"] == filtered["hits"][0]["rerank_score"]
    assert "rrf_score" in filtered["hits"][0]

    ordered = search(
        conn,
        _FakeStore(raw),  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        rag_id="rag1",
        query="手順",
        score_threshold=0.0,
        use_reranker=True,
        reranker=reranker,
    )
    assert [hit["child_chunk_id"] for hit in ordered["hits"]] == ["beta", "alpha"]
    assert seen

    def forbidden(query: str, passages: list[str]) -> list[float]:
        raise AssertionError(f"disabled reranker was called: {query} {passages}")

    disabled = Reranker(False, "fake", tmp_path / "off", scorer=forbidden)
    disabled.score = forbidden  # type: ignore[method-assign]
    plain = search(
        conn,
        _FakeStore(raw),  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        rag_id="rag1",
        query="手順",
        score_threshold=0.0,
        use_reranker=None,
        reranker=disabled,
    )
    assert [hit["child_chunk_id"] for hit in plain["hits"]] == ["alpha", "beta"]

    fields = Settings.model_fields
    assert fields["reranker_enabled"].default is False
    assert fields["reranker_model"].default == "cl-nagoya/ruri-v3-reranker-310m"
    assert fields["reranker_top_n"].default == 30


def test_collection_enables_idf_modifier() -> None:
    client = _CreateClient()
    store = QdrantStore.__new__(QdrantStore)
    store.client = client  # type: ignore[assignment]
    store.create_collection("phase2")
    assert client.kwargs is not None
    sparse = client.kwargs["sparse_vectors_config"]["sparse"]
    assert sparse.modifier == qm.Modifier.IDF
    import inspect

    assert "Modifier.IDF" in inspect.getsource(QdrantStore.create_collection)


class _Point:
    def __init__(self, point_id: str, score: float, payload: dict[str, Any] | None = None) -> None:
        self.id = point_id
        self.score = score
        self.payload = payload or {}


class _Response:
    def __init__(self, points: list[_Point]) -> None:
        self.points = points


class _BatchClient:
    def __init__(self, *, dense: list[_Point], sparse: list[_Point]) -> None:
        self._dense = dense
        self._sparse = sparse
        self.calls = 0

    def query_batch_points(self, *, collection_name: str, requests: list[Any]) -> list[_Response]:
        self.calls += 1
        assert collection_name
        assert [request.using for request in requests] == ["dense", "sparse"]
        return [_Response(self._dense), _Response(self._sparse)]


class _SplitClient:
    def __init__(self, *, dense: list[_Point], sparse: list[_Point]) -> None:
        self._dense = dense
        self._sparse = sparse
        self.calls: list[str] = []

    def query_points(self, **kwargs: Any) -> _Response:
        using = str(kwargs["using"])
        self.calls.append(using)
        if using == "sparse":
            return _Response(self._sparse)
        return _Response(self._dense)


class _CreateClient:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def collection_exists(self, name: str) -> bool:
        assert name
        return False

    def create_collection(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def create_payload_index(self, **kwargs: Any) -> None:
        assert kwargs["collection_name"]


class _FakeStore:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits
        self.limit: int | None = None

    def hybrid_search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.limit = int(kwargs["limit"])
        return list(self.hits)


class _Vector:
    def tolist(self) -> list[float]:
        return [0.0, 0.0]


class _Embedder:
    def encode_queries(self, texts: list[str]) -> list[_Vector]:
        return [_Vector() for _ in texts]


def _hybrid(client: _BatchClient | _SplitClient) -> list[dict[str, Any]]:
    store = QdrantStore.__new__(QdrantStore)
    store.client = client  # type: ignore[assignment]
    return store.hybrid_search(
        collection="col",
        dense=[0.1, 0.2],
        query_text="ERR-4092-XJ",
        limit=5,
        query_filter=None,
        hybrid=True,
    )


def _raw(
    child_id: str,
    parent_id: str,
    *,
    score: float,
    dense_score: float,
    sparse_score: float = 0.0,
    sparse_rank: int | None = None,
) -> dict[str, Any]:
    return {
        "id": child_id,
        "score": score,
        "raw_rrf": 0.01,
        "dense_score": dense_score,
        "sparse_score": sparse_score,
        "sparse_rank": sparse_rank,
        "payload": {
            "title": "規程",
            "section_path": "第1章",
            "source_type": "file",
            "acl_tag": "internal",
            "child_chunk_id": child_id,
            "parent_chunk_id": parent_id,
            "record_id": "rec",
        },
    }


def _seed(tmp_path: Path, children: list[tuple[str, str, str]]) -> Any:
    conn = connect(tmp_path / "phase2.db")
    migrate(conn)
    now = "2026-11-23T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO rags(
            rag_id, display_name, status, qdrant_alias, active_collection,
            staging_collection, schema_version, acl_tag_default, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("rag1", "t", "ready", "alias1", "col1", None, 1, "internal", now, now),
    )
    conn.execute(
        """
        INSERT INTO sources(
            source_id, rag_id, source_type, source_name, config_json,
            acl_tag_default, enabled, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("src1", "rag1", "file", "s", "{}", "internal", 1, now),
    )
    conn.execute(
        """
        INSERT INTO source_objects(
            object_id, source_id, object_type, object_key, latest_doc_version_id, last_ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("obj1", "src1", "file", "a.txt", "doc1", now),
    )
    conn.execute(
        """
        INSERT INTO document_versions(
            doc_version_id, object_id, content_hash, file_hash, row_hash,
            extracted_bytes, parent_count, child_count, pipeline_fingerprint, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("doc1", "obj1", "hash", None, None, 1, 1, len(children), "fp", now),
    )
    parents: set[str] = set()
    for index, (child_id, parent_id, text) in enumerate(children):
        if parent_id not in parents:
            conn.execute(
                """
                INSERT INTO parent_chunks(
                    parent_chunk_id, doc_version_id, parent_index, text,
                    section_path, page_from, page_to, token_estimate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (parent_id, "doc1", len(parents), f"親 {parent_id}", "第1章", None, None, 1),
            )
            parents.add(parent_id)
        conn.execute(
            """
            INSERT INTO child_chunks(
                child_chunk_id, parent_chunk_id, child_index, text,
                char_count, qdrant_point_id, embedded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (child_id, parent_id, index, text, len(text), child_id, now),
        )
    conn.commit()
    return conn


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))
