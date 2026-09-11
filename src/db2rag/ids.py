"""ULID と決定的 Qdrant point ID。"""

from __future__ import annotations

from uuid import uuid5

from ulid import ULID

from db2rag.constants import NAMESPACE_DB2RAG


def new_id(prefix: str) -> str:
    """例: rag_01HQ... 。プレフィックスは設計書のエンティティ接頭辞。"""
    return f"{prefix}_{ULID()}"


def point_id_for_child(child_chunk_id: str) -> str:
    """同一 child の再 upsert が上書きになるよう UUID v5 で固定する。"""
    return str(uuid5(NAMESPACE_DB2RAG, child_chunk_id))
