"""RAG の secure purge。ベクトル・SQLite 関連行・export / upload を落とす。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from db2rag.db import row_to_dict, rows_to_dicts, utcnow


def purge_rag(
    conn: sqlite3.Connection,
    store: Any,
    rag_id: str,
    *,
    uploads_dir: Path,
    exports_dir: Path,
) -> dict[str, Any]:
    rag = row_to_dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    if rag is None or rag.get("status") == "purged":
        raise ValueError("ERR_RAG_NOT_FOUND")

    now = utcnow()
    conn.execute(
        """
        UPDATE jobs
        SET status='cancelled', finished_at=COALESCE(finished_at, ?)
        WHERE rag_id=? AND status IN ('pending','running','paused','verifying')
        """,
        (now, rag_id),
    )
    conn.commit()
    _wait_lease_release(conn, rag_id)

    upload_paths = _upload_paths_for_rag(conn, rag_id, uploads_dir)
    export_paths = [
        Path(row["export_path"])
        for row in rows_to_dicts(
            conn.execute("SELECT export_path FROM exports WHERE rag_id=?", (rag_id,)).fetchall()
        )
        if row.get("export_path")
    ]
    collection_names = _collection_names(conn, store, rag)

    alias = rag.get("qdrant_alias") or ""
    if alias:
        store.delete_alias(alias)
    collections_deleted: list[str] = []
    for name in collection_names:
        store.delete_collection(name)
        collections_deleted.append(name)

    for path in export_paths:
        _safe_rmtree(path, exports_dir)
    for path in upload_paths:
        _safe_unlink(path, uploads_dir)

    _delete_rag_rows(conn, rag_id)
    conn.commit()
    return {
        "rag_id": rag_id,
        "status": "purged",
        "collections_deleted": collections_deleted,
    }


def _wait_lease_release(conn: sqlite3.Connection, rag_id: str, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM jobs
            WHERE rag_id=? AND lease_owner IS NOT NULL
              AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
            """,
            (rag_id, utcnow()),
        ).fetchone()
        if int(row["n"]) == 0:
            return
        time.sleep(0.2)


def _collection_names(conn: sqlite3.Connection, store: Any, rag: dict[str, Any]) -> list[str]:
    names: set[str] = set()
    for key in ("active_collection", "staging_collection"):
        value = rag.get(key)
        if value:
            names.add(str(value))
    archived = rows_to_dicts(
        conn.execute(
            "SELECT collection_name FROM archived_collections WHERE rag_id=?",
            (rag["rag_id"],),
        ).fetchall()
    )
    for row in archived:
        if row.get("collection_name"):
            names.add(str(row["collection_name"]))
    prefix = f"db2rag_{rag['rag_id']}"
    for name in store.list_collection_names():
        if name == prefix or name.startswith(prefix + "_"):
            names.add(name)
    return sorted(names)


def _upload_paths_for_rag(conn: sqlite3.Connection, rag_id: str, uploads_dir: Path) -> list[Path]:
    paths: list[Path] = []
    rows = rows_to_dicts(
        conn.execute("SELECT config_json FROM sources WHERE rag_id=? AND source_type='file'", (rag_id,)).fetchall()
    )
    for row in rows:
        try:
            config = json.loads(row.get("config_json") or "{}")
        except json.JSONDecodeError:
            continue
        for raw in config.get("paths") or []:
            paths.append(Path(str(raw)))
    _ = uploads_dir
    return paths


def _delete_rag_rows(conn: sqlite3.Connection, rag_id: str) -> None:
    conn.execute(
        """
        DELETE FROM child_chunks WHERE parent_chunk_id IN (
            SELECT p.parent_chunk_id FROM parent_chunks p
            JOIN document_versions d ON p.doc_version_id = d.doc_version_id
            JOIN source_objects o ON d.object_id = o.object_id
            JOIN sources s ON o.source_id = s.source_id
            WHERE s.rag_id=?
        )
        """,
        (rag_id,),
    )
    conn.execute(
        """
        DELETE FROM parent_chunks WHERE doc_version_id IN (
            SELECT d.doc_version_id FROM document_versions d
            JOIN source_objects o ON d.object_id = o.object_id
            JOIN sources s ON o.source_id = s.source_id
            WHERE s.rag_id=?
        )
        """,
        (rag_id,),
    )
    conn.execute(
        """
        DELETE FROM document_versions WHERE object_id IN (
            SELECT o.object_id FROM source_objects o
            JOIN sources s ON o.source_id = s.source_id
            WHERE s.rag_id=?
        )
        """,
        (rag_id,),
    )
    conn.execute(
        "DELETE FROM job_items WHERE job_id IN (SELECT job_id FROM jobs WHERE rag_id=?)",
        (rag_id,),
    )
    conn.execute("DELETE FROM jobs WHERE rag_id=?", (rag_id,))
    conn.execute(
        "DELETE FROM source_objects WHERE source_id IN (SELECT source_id FROM sources WHERE rag_id=?)",
        (rag_id,),
    )
    conn.execute("DELETE FROM sources WHERE rag_id=?", (rag_id,))
    conn.execute("DELETE FROM evaluation_runs WHERE rag_id=?", (rag_id,))
    conn.execute("DELETE FROM exports WHERE rag_id=?", (rag_id,))
    conn.execute("DELETE FROM archived_collections WHERE rag_id=?", (rag_id,))
    conn.execute(
        """
        UPDATE rags
        SET status='purged', active_collection=NULL, staging_collection=NULL, updated_at=?
        WHERE rag_id=?
        """,
        (utcnow(), rag_id),
    )


def _safe_rmtree(path: Path, root: Path) -> None:
    resolved = _under_root(path, root)
    if resolved is None:
        return
    if resolved.is_dir():
        shutil.rmtree(resolved, ignore_errors=True)
    elif resolved.is_file():
        resolved.unlink(missing_ok=True)


def _safe_unlink(path: Path, root: Path) -> None:
    resolved = _under_root(path, root)
    if resolved is None:
        return
    if resolved.is_file():
        resolved.unlink(missing_ok=True)
        parent = resolved.parent
        root_r = root.resolve()
        while parent != root_r and parent.is_relative_to(root_r):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    elif resolved.is_dir():
        shutil.rmtree(resolved, ignore_errors=True)


def _under_root(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve()
        root_r = root.resolve()
    except OSError:
        return None
    if resolved == root_r or not resolved.is_relative_to(root_r):
        return None
    return resolved
