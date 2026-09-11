"""Qdrant 1.19 collection / alias / hybrid search。"""

from __future__ import annotations

from typing import Any

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
        try:
            telemetry = self.client.http.telemetry_api.telemetry()
            return str(getattr(telemetry, "version", "") or "")
        except Exception:
            pass
        try:
            info = self.client.info()
            version = getattr(info, "version", None) or getattr(info, "title", "")
            return str(version)
        except Exception:
            root = self.client.http.rest.raw_request("GET", "/")
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
                "sparse": qm.SparseVectorParams(index=qm.SparseIndexParams(on_disk=False))
            },
            hnsw_config=qm.HnswConfigDiff(
                m=HNSW_M,
                ef_construct=HNSW_EF_CONSTRUCT,
                full_scan_threshold=10_000,
            ),
            quantization_config=None,
        )
        indexes = [
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
        search_params = qm.SearchParams(hnsw_ef=HNSW_EF)
        dense_hits = self._query(
            collection=collection,
            query=dense,
            using="dense",
            query_filter=query_filter,
            limit=PREFETCH_DENSE,
            search_params=search_params,
        )
        if not hybrid:
            return [_hit_to_dict(hit, source="dense") for hit in dense_hits[:limit]]
        sparse = to_sparse(query_text)
        sparse_hits = self._query(
            collection=collection,
            query=sparse,
            using="sparse",
            query_filter=query_filter,
            limit=PREFETCH_SPARSE,
            search_params=None,
        )
        fused = _rrf(
            [[str(h.id) for h in dense_hits], [str(h.id) for h in sparse_hits]],
            k=RRF_K,
        )
        by_id: dict[str, Any] = {}
        for hit in list(dense_hits) + list(sparse_hits):
            by_id[str(hit.id)] = hit
        results: list[dict[str, Any]] = []
        max_score = max(fused.values()) if fused else 1.0
        for point_id, raw_score in sorted(fused.items(), key=lambda item: item[1], reverse=True):
            hit = by_id[point_id]
            dense_score = next((float(h.score) for h in dense_hits if str(h.id) == point_id), 0.0)
            results.append(
                {
                    "id": point_id,
                    "score": raw_score / max_score if max_score else 0.0,
                    "dense_score": dense_score,
                    "payload": dict(hit.payload or {}),
                }
            )
            if len(results) >= limit:
                break
        return results

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
    return {
        "id": str(hit.id),
        "score": float(hit.score),
        "dense_score": float(hit.score),
        "payload": dict(hit.payload or {}),
        "source": source,
    }


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
