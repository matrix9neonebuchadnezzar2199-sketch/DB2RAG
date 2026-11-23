"""Qdrant 1.19 collection / alias / hybrid search。"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from db2rag.constants import (
    EMBED_DIM,
    HNSW_EF,
    HNSW_EF_CONSTRUCT,
    HNSW_M,
    PREFETCH_DENSE,
    PREFETCH_SPARSE,
    RRF_K,
    UPSERT_BATCH,
)
from db2rag.sparse import to_sparse


class QdrantStore:
    def __init__(self, *, url: str, path: str | None = None) -> None:
        if path:
            self.client = QdrantClient(path=path)
            self.mode = "local"
        else:
            self.client = QdrantClient(url=url, timeout=60)
            self.mode = "http"
        self.url = url
        self.path = path

    def version(self) -> str:
        """Qdrant の版。ローカルモードで HTTP が無いときは空文字に落とす。"""
        try:
            # 生成クライアントの型に telemetry が無い版がある。ローカルはここで落ちる。
            http = cast(Any, self.client.http)
            telemetry = http.telemetry_api.telemetry()
            return str(getattr(telemetry, "version", "") or "")
        except Exception:
            pass
        try:
            info = self.client.info()
            version = getattr(info, "version", None) or getattr(info, "title", "")
            return str(version)
        except Exception:
            try:
                http = cast(Any, self.client.http)
                root = http.rest.raw_request("GET", "/")
            except Exception:
                return ""
            if isinstance(root, dict):
                return str(root.get("version", ""))
            return ""

    def health(self) -> dict[str, Any]:
        version = self.version() or "unknown"
        ok = True
        try:
            self.client.get_collections()
        except Exception as exc:
            return {"ok": False, "version": version, "error": str(exc), "mode": self.mode}
        return {"ok": ok, "version": version, "mode": self.mode}

    def create_collection(self, name: str) -> None:
        """dense と IDF 付き sparse のコレクションを作る。既存なら何もしない。

        Args:
            name: コレクション名。
        """
        if self.client.collection_exists(name):
            return
        self.client.create_collection(
            collection_name=name,
            vectors_config={
                "dense": qm.VectorParams(
                    size=EMBED_DIM,
                    distance=qm.Distance.COSINE,
                    on_disk=False,
                )
            },
            sparse_vectors_config={
                # IDF はここで掛ける。トークン側で log を掛けると二重になる。
                "sparse": qm.SparseVectorParams(
                    index=qm.SparseIndexParams(on_disk=False),
                    modifier=qm.Modifier.IDF,
                )
            },
            hnsw_config=qm.HnswConfigDiff(
                m=HNSW_M,
                ef_construct=HNSW_EF_CONSTRUCT,
                full_scan_threshold=10_000,
            ),
            quantization_config=None,
        )
        indexes: list[tuple[str, qm.PayloadSchemaType | qm.TextIndexParams]] = [
            ("rag_id", qm.PayloadSchemaType.KEYWORD),
            ("acl_tag", qm.PayloadSchemaType.KEYWORD),
            ("source_type", qm.PayloadSchemaType.KEYWORD),
            ("object_id", qm.PayloadSchemaType.KEYWORD),
            ("content_hash", qm.PayloadSchemaType.KEYWORD),
            ("doc_content_hash", qm.PayloadSchemaType.KEYWORD),
            (
                "title",
                qm.TextIndexParams(type=qm.TextIndexType.TEXT, tokenizer=qm.TokenizerType.MULTILINGUAL),
            ),
            (
                "section_path",
                qm.TextIndexParams(type=qm.TextIndexType.TEXT, tokenizer=qm.TokenizerType.MULTILINGUAL),
            ),
            ("created_at", qm.PayloadSchemaType.DATETIME),
            ("updated_at", qm.PayloadSchemaType.DATETIME),
        ]
        for field_name, schema in indexes:
            try:
                self.client.create_payload_index(
                    collection_name=name,
                    field_name=field_name,
                    field_schema=schema,
                )
            except Exception as exc:
                message = str(exc).lower()
                if "already" not in message and "exist" not in message:
                    raise

    def set_alias(self, alias: str, collection: str) -> None:
        self.client.update_collection_aliases(
            change_aliases_operations=[
                qm.CreateAliasOperation(
                    create_alias=qm.CreateAlias(collection_name=collection, alias_name=alias)
                )
            ]
        )

    def list_collection_names(self) -> list[str]:
        try:
            result = self.client.get_collections()
        except Exception:
            return []
        return [str(item.name) for item in result.collections]

    def delete_alias(self, alias: str) -> None:
        if not alias:
            return
        try:
            self.client.update_collection_aliases(
                change_aliases_operations=[
                    qm.DeleteAliasOperation(delete_alias=qm.DeleteAlias(alias_name=alias))
                ]
            )
        except Exception:
            return

    def delete_collection(self, name: str) -> None:
        if not name:
            return
        try:
            if self.client.collection_exists(name):
                self.client.delete_collection(name)
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message or "not exist" in message:
                return
            raise

    def count(self, name: str) -> int:
        return int(self.client.count(name, exact=True).count)

    def delete_points(self, collection: str, point_ids: list[str]) -> None:
        """指定 point を消す。空リストは何もしない。"""
        if not collection or not point_ids:
            return
        for start in range(0, len(point_ids), UPSERT_BATCH):
            batch: list[int | str | UUID] = list(point_ids[start : start + UPSERT_BATCH])
            self.client.delete(
                collection_name=collection,
                points_selector=qm.PointIdsList(points=batch),
                wait=True,
            )

    def upsert_points(
        self,
        collection: str,
        points: list[qm.PointStruct],
    ) -> None:
        for start in range(0, len(points), UPSERT_BATCH):
            batch = points[start : start + UPSERT_BATCH]
            self.client.upsert(collection_name=collection, points=batch, wait=True)

    def hybrid_search(
        self,
        *,
        collection: str,
        dense: list[float],
        query_text: str,
        limit: int,
        query_filter: qm.Filter | None,
        hybrid: bool = True,
    ) -> list[dict[str, Any]]:
        """Dense と sparse を取り、絶対スケールの RRF で融合する。

        取得結果の最大値では割らない。片側だけ 1 位の理論値は 0.5 で、両方 1 位で 1.0。

        Args:
            collection: コレクション名または alias。
            dense: クエリの dense ベクトル。
            query_text: sparse 化するクエリ文字列。
            limit: 返す最大件数。
            query_filter: payload フィルタ。
            hybrid: False のときは dense のみ。

        Returns:
            hybrid 時は ``score`` が ``rrf_norm``。``raw_rrf`` ``dense_score``
            ``sparse_score`` ``sparse_rank`` ``payload`` を含む。

        Raises:
            RuntimeError: batch 応答の本数がリクエストと違うとき。
        """
        if limit < 1:
            return []
        search_params = qm.SearchParams(hnsw_ef=HNSW_EF)
        dense_limit = max(PREFETCH_DENSE, limit)
        sparse_vector = to_sparse(query_text) if hybrid else None
        sparse_limit = max(PREFETCH_SPARSE, limit)
        dense_hits, sparse_hits = self._query_dense_and_sparse(
            collection=collection,
            dense=dense,
            sparse_vector=sparse_vector,
            query_filter=query_filter,
            dense_limit=dense_limit,
            sparse_limit=sparse_limit,
            search_params=search_params,
            hybrid=hybrid,
        )
        if not hybrid:
            return [_hit_to_dict(hit, source="dense") for hit in dense_hits[:limit]]
        return _fuse_rrf(dense_hits, sparse_hits, k=RRF_K, limit=limit)

    def _query_dense_and_sparse(
        self,
        *,
        collection: str,
        dense: list[float],
        sparse_vector: Any,
        query_filter: qm.Filter | None,
        dense_limit: int,
        sparse_limit: int,
        search_params: qm.SearchParams,
        hybrid: bool,
    ) -> tuple[list[Any], list[Any]]:
        """dense と sparse を 1 RTT で取る。batch が無ければ query を分ける。"""
        batch_query = getattr(self.client, "query_batch_points", None)
        if hybrid and callable(batch_query):
            requests = [
                qm.QueryRequest(
                    query=dense,
                    using="dense",
                    filter=query_filter,
                    limit=dense_limit,
                    params=search_params,
                    with_payload=True,
                    with_vector=False,
                ),
                qm.QueryRequest(
                    query=sparse_vector,
                    using="sparse",
                    filter=query_filter,
                    limit=sparse_limit,
                    with_payload=True,
                    with_vector=False,
                ),
            ]
            responses: list[Any] = list(
                batch_query(collection_name=collection, requests=requests)
            )
            if len(responses) != len(requests):
                raise RuntimeError(
                    f"query_batch_points returned {len(responses)} for {len(requests)} requests"
                )
            return list(responses[0].points), list(responses[1].points)
        dense_hits = self._query(
            collection=collection,
            query=dense,
            using="dense",
            query_filter=query_filter,
            limit=dense_limit,
            search_params=search_params,
        )
        if not hybrid or sparse_vector is None:
            return dense_hits, []
        sparse_hits = self._query(
            collection=collection,
            query=sparse_vector,
            using="sparse",
            query_filter=query_filter,
            limit=sparse_limit,
            search_params=None,
        )
        return dense_hits, sparse_hits

    def _query(
        self,
        *,
        collection: str,
        query: Any,
        using: str,
        query_filter: qm.Filter | None,
        limit: int,
        search_params: Any,
    ) -> list[Any]:
        result = self.client.query_points(
            collection_name=collection,
            query=query,
            using=using,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
            search_params=search_params,
        )
        return list(result.points)


def _hit_to_dict(hit: Any, source: str) -> dict[str, Any]:
    score = float(hit.score)
    return {
        "id": str(hit.id),
        "score": score,
        "raw_rrf": None,
        "dense_score": score,
        "sparse_score": 0.0,
        "sparse_rank": None,
        "payload": dict(hit.payload or {}),
        "source": source,
    }


def _fuse_rrf(
    dense_hits: list[Any],
    sparse_hits: list[Any],
    *,
    k: int,
    limit: int,
) -> list[dict[str, Any]]:
    """両リストの順位から RRF を作り、理論最大 ``2/(k+1)`` で 0..1 に写す。"""
    dense_ids = [str(hit.id) for hit in dense_hits]
    sparse_ids = [str(hit.id) for hit in sparse_hits]
    raw_scores = _rrf([dense_ids, sparse_ids], k)
    dense_by_id: dict[str, Any] = {}
    for hit in dense_hits:
        dense_by_id.setdefault(str(hit.id), hit)
    sparse_by_id: dict[str, Any] = {}
    sparse_rank: dict[str, int] = {}
    for rank, hit in enumerate(sparse_hits, start=1):
        point_id = str(hit.id)
        sparse_by_id.setdefault(point_id, hit)
        sparse_rank.setdefault(point_id, rank)
    ordered = sorted(raw_scores.items(), key=lambda item: (-item[1], item[0]))
    results: list[dict[str, Any]] = []
    for point_id, raw_rrf in ordered:
        dense_hit = dense_by_id.get(point_id)
        sparse_hit = sparse_by_id.get(point_id)
        payload_hit = dense_hit if dense_hit is not None else sparse_hit
        payload = dict(getattr(payload_hit, "payload", None) or {})
        results.append(
            {
                "id": point_id,
                "score": _rrf_norm(raw_rrf, k),
                "raw_rrf": raw_rrf,
                "dense_score": float(dense_hit.score) if dense_hit is not None else 0.0,
                "sparse_score": float(sparse_hit.score) if sparse_hit is not None else 0.0,
                "sparse_rank": sparse_rank.get(point_id),
                "payload": payload,
            }
        )
        if len(results) >= limit:
            break
    return results


def _rrf_norm(raw_rrf: float, k: int) -> float:
    """``min(1, raw / (2/(k+1)))``。片側 1 位は 0.5。"""
    return min(1.0, raw_rrf / (2.0 / (k + 1)))


def _rrf(rank_lists: list[list[str]], k: int) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rank_lists:
        for rank, point_id in enumerate(ranking, start=1):
            scores[point_id] = scores.get(point_id, 0.0) + 1.0 / (k + rank)
    return scores


def make_point(
    *,
    point_id: str,
    dense: list[float],
    text: str,
    payload: dict[str, Any],
) -> qm.PointStruct:
    return qm.PointStruct(
        id=point_id,
        vector={
            "dense": dense,
            "sparse": to_sparse(text),
        },
        payload=payload,
    )
