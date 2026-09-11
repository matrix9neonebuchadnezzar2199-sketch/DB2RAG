"""JSONL.zst + manifest export。AISSS 再 ingest 用。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import zstandard as zstd

from db2rag.constants import CHUNK_STRATEGY, EMBED_DIM, RURI_MODEL_ID, RURI_REVISION
from db2rag.db import row_to_dict, utcnow
from db2rag.ids import new_id


def export_rag(conn: sqlite3.Connection, rag_id: str, export_root: Path) -> dict[str, str]:
    rag = row_to_dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    if rag is None:
        raise ValueError("ERR_RAG_NOT_FOUND")
    export_id = new_id("exp")
    dest = export_root / export_id
    dest.mkdir(parents=True, exist_ok=True)
    docs_path = dest / "documents.jsonl.zst"
    chunks_path = dest / "chunks.jsonl.zst"
    compressor = zstd.ZstdCompressor(level=3)
    doc_count = 0
    chunk_count = 0
    with compressor.stream_writer(docs_path.open("wb")) as writer:
        for row in conn.execute(
            """
            SELECT d.*, o.object_key, s.source_type, s.source_name
            FROM document_versions d
            JOIN source_objects o ON d.object_id=o.object_id
            JOIN sources s ON o.source_id=s.source_id
            WHERE s.rag_id=?
            """,
            (rag_id,),
        ):
            line = json.dumps(dict(row), ensure_ascii=False) + "\n"
            writer.write(line.encode("utf-8"))
            doc_count += 1
    with compressor.stream_writer(chunks_path.open("wb")) as writer:
        for row in conn.execute(
            """
            SELECT c.*, p.section_path, p.text AS parent_text, d.content_hash
            FROM child_chunks c
            JOIN parent_chunks p ON c.parent_chunk_id=p.parent_chunk_id
            JOIN document_versions d ON p.doc_version_id=d.doc_version_id
            JOIN source_objects o ON d.object_id=o.object_id
            JOIN sources s ON o.source_id=s.source_id
            WHERE s.rag_id=?
            """,
            (rag_id,),
        ):
            payload = {key: row[key] for key in row.keys()}
            writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            chunk_count += 1
    manifest = {
        "export_id": export_id,
        "rag_id": rag_id,
        "created_at": utcnow(),
        "embedding_model": RURI_MODEL_ID,
        "embedding_revision": RURI_REVISION,
        "embedding_dim": EMBED_DIM,
        "chunk_strategy": CHUNK_STRATEGY,
        "documents": doc_count,
        "chunks": chunk_count,
        "active_collection": rag.get("active_collection"),
        "alias": rag.get("qdrant_alias"),
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    checksums = []
    for file in (docs_path, chunks_path, dest / "manifest.json"):
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        checksums.append(f"{digest}  {file.name}")
    (dest / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    manifest_sha = hashlib.sha256((dest / "manifest.json").read_bytes()).hexdigest()
    conn.execute(
        """
        INSERT INTO exports(export_id, rag_id, export_path, manifest_sha256, status, created_at)
        VALUES (?, ?, ?, ?, 'completed', ?)
        """,
        (export_id, rag_id, str(dest), manifest_sha, utcnow()),
    )
    conn.commit()
    return {"export_id": export_id, "path": str(dest), "manifest_sha256": manifest_sha}
