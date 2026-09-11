"""RAG purge: SQLite cascade と uploads 配下だけ消す。"""

from __future__ import annotations

import json
from pathlib import Path

from db2rag.db import connect, migrate, utcnow
from db2rag.ids import new_id
from db2rag.pipeline import create_rag, list_rags
from db2rag.purge import purge_rag


class MemoryStore:
    def __init__(self) -> None:
        self.collections = {"db2rag_keep_other", "db2rag_old_staging"}
        self.deleted_collections: list[str] = []
        self.deleted_aliases: list[str] = []

    def list_collection_names(self) -> list[str]:
        return list(self.collections)

    def delete_alias(self, alias: str) -> None:
        self.deleted_aliases.append(alias)

    def delete_collection(self, name: str) -> None:
        self.collections.discard(name)
        self.deleted_collections.append(name)


def _seed_rag(conn, rag_id: str, uploads_dir: Path, exports_dir: Path) -> tuple[Path, Path, Path]:
    now = utcnow()
    source_id = new_id("src")
    object_id = new_id("obj")
    doc_id = new_id("doc")
    parent_id = new_id("par")
    child_id = new_id("chk")
    job_id = new_id("job")
    item_id = new_id("item")
    export_id = new_id("exp")
    upload_file = uploads_dir / "batch1" / "note.txt"
    upload_file.parent.mkdir(parents=True)
    upload_file.write_text("secret", encoding="utf-8")
    outside = uploads_dir.parent / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    export_dir = exports_dir / export_id
    export_dir.mkdir()
    (export_dir / "manifest.json").write_text("{}", encoding="utf-8")
    conn.execute(
        """
        INSERT INTO sources(source_id, rag_id, source_type, source_name, config_json, acl_tag_default, enabled, created_at)
        VALUES (?, ?, 'file', 'files', ?, 'internal', 1, ?)
        """,
        (source_id, rag_id, json.dumps({"paths": [str(upload_file), str(outside)]}), now),
    )
    conn.execute(
        "INSERT INTO source_objects(object_id, source_id, object_type, object_key) VALUES (?, ?, 'file', 'note.txt')",
        (object_id, source_id),
    )
    conn.execute(
        """
        INSERT INTO document_versions(doc_version_id, object_id, content_hash, extracted_bytes, pipeline_fingerprint, created_at)
        VALUES (?, ?, 'abc', 4, 'fp', ?)
        """,
        (doc_id, object_id, now),
    )
    conn.execute(
        """
        INSERT INTO parent_chunks(parent_chunk_id, doc_version_id, parent_index, text, token_estimate)
        VALUES (?, ?, 0, 'parent', 1)
        """,
        (parent_id, doc_id),
    )
    conn.execute(
        """
        INSERT INTO child_chunks(child_chunk_id, parent_chunk_id, child_index, text, char_count, qdrant_point_id)
        VALUES (?, ?, 0, 'child', 5, 'pt1')
        """,
        (child_id, parent_id),
    )
    conn.execute(
        """
        INSERT INTO jobs(job_id, rag_id, mode, status, created_by, created_at, work_units_total)
        VALUES (?, ?, 'create', 'completed', 'test', ?, 1)
        """,
        (job_id, rag_id, now),
    )
    conn.execute(
        """
        INSERT INTO job_items(job_item_id, job_id, object_id, status, checkpoint)
        VALUES (?, ?, ?, 'completed', 'done')
        """,
        (item_id, job_id, object_id),
    )
    conn.execute(
        """
        INSERT INTO exports(export_id, rag_id, export_path, status, created_at)
        VALUES (?, ?, ?, 'completed', ?)
        """,
        (export_id, rag_id, str(export_dir), now),
    )
    conn.execute(
        """
        INSERT INTO archived_collections(collection_name, rag_id, archived_at, delete_after)
        VALUES (?, ?, ?, ?)
        """,
        ("db2rag_old_staging", rag_id, now, now),
    )
    conn.execute(
        "UPDATE rags SET status='active', active_collection=?, staging_collection=NULL WHERE rag_id=?",
        (f"db2rag_{rag_id}_staging_{job_id}", rag_id),
    )
    conn.commit()
    return upload_file, outside, export_dir


def test_purge_removes_related_rows_and_local_files(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db2rag.db")
    migrate(conn)
    uploads = tmp_path / "uploads"
    exports = tmp_path / "exports"
    uploads.mkdir()
    exports.mkdir()
    rag = create_rag(conn, "削除対象", "internal")
    rag_id = rag["rag_id"]
    upload_file, outside, export_dir = _seed_rag(conn, rag_id, uploads, exports)
    store = MemoryStore()
    store.collections.add(f"db2rag_{rag_id}_staging_jobx")
    store.collections.add(rag["qdrant_alias"])

    result = purge_rag(conn, store, rag_id, uploads_dir=uploads, exports_dir=exports)

    assert result["status"] == "purged"
    assert list_rags(conn) == []
    row = conn.execute("SELECT status FROM rags WHERE rag_id=?", (rag_id,)).fetchone()
    assert row["status"] == "purged"
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE rag_id=?", (rag_id,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM child_chunks").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM sources WHERE rag_id=?", (rag_id,)).fetchone()["n"] == 0
    assert not upload_file.exists()
    assert outside.exists()
    assert not export_dir.exists()
    assert rag["qdrant_alias"] in store.deleted_aliases
    assert any(name.startswith(f"db2rag_{rag_id}") for name in store.deleted_collections)
    assert "db2rag_keep_other" in store.collections


def test_purge_missing_rag(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db2rag.db")
    migrate(conn)
    store = MemoryStore()
    try:
        purge_rag(conn, store, "rag_missing", uploads_dir=tmp_path, exports_dir=tmp_path)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "ERR_RAG_NOT_FOUND" in str(exc)
