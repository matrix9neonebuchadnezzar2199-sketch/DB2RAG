"""SQLite WAL 制御 DB。ベクトルは持たない。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from db2rag.constants import SQLITE_SCHEMA_VERSION

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rags (
    rag_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    qdrant_alias TEXT NOT NULL,
    active_collection TEXT,
    staging_collection TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    acl_tag_default TEXT NOT NULL DEFAULT 'internal',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    rag_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    acl_tag_default TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (rag_id) REFERENCES rags(rag_id)
);

CREATE TABLE IF NOT EXISTS source_objects (
    object_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_key TEXT NOT NULL,
    latest_doc_version_id TEXT,
    last_ingested_at TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS document_versions (
    doc_version_id TEXT PRIMARY KEY,
    object_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    file_hash TEXT,
    row_hash TEXT,
    extracted_bytes INTEGER NOT NULL DEFAULT 0,
    parent_count INTEGER NOT NULL DEFAULT 0,
    child_count INTEGER NOT NULL DEFAULT 0,
    pipeline_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (object_id) REFERENCES source_objects(object_id)
);

CREATE TABLE IF NOT EXISTS parent_chunks (
    parent_chunk_id TEXT PRIMARY KEY,
    doc_version_id TEXT NOT NULL,
    parent_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    section_path TEXT,
    page_from INTEGER,
    page_to INTEGER,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (doc_version_id) REFERENCES document_versions(doc_version_id)
);

CREATE TABLE IF NOT EXISTS child_chunks (
    child_chunk_id TEXT PRIMARY KEY,
    parent_chunk_id TEXT NOT NULL,
    child_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    qdrant_point_id TEXT NOT NULL,
    embedded_at TEXT,
    FOREIGN KEY (parent_chunk_id) REFERENCES parent_chunks(parent_chunk_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    rag_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    progress_pct REAL NOT NULL DEFAULT 0,
    work_units_total REAL NOT NULL DEFAULT 0,
    work_units_done REAL NOT NULL DEFAULT 0,
    eta_seconds INTEGER,
    current_item_name TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    error_code TEXT,
    error_message TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    started_at TEXT,
    FOREIGN KEY (rag_id) REFERENCES rags(rag_id)
);

CREATE TABLE IF NOT EXISTS job_items (
    job_item_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    status TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (object_id) REFERENCES source_objects(object_id)
);

CREATE TABLE IF NOT EXISTS exports (
    export_id TEXT PRIMARY KEY,
    rag_id TEXT NOT NULL,
    export_path TEXT NOT NULL,
    manifest_sha256 TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (rag_id) REFERENCES rags(rag_id)
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    eval_run_id TEXT PRIMARY KEY,
    rag_id TEXT NOT NULL,
    question_set_version TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    gate_passed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (rag_id) REFERENCES rags(rag_id)
);

CREATE TABLE IF NOT EXISTS archived_collections (
    collection_name TEXT PRIMARY KEY,
    rag_id TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    delete_after TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS secrets (
    secret_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_rag ON sources(rag_id);
CREATE INDEX IF NOT EXISTS idx_objects_source ON source_objects(source_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_job_items_job ON job_items(job_id);
CREATE INDEX IF NOT EXISTS idx_child_parent ON child_chunks(parent_chunk_id);
CREATE INDEX IF NOT EXISTS idx_doc_object_hash ON document_versions(object_id, content_hash);
"""


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    row = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None or int(row["version"]) < SQLITE_SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SQLITE_SCHEMA_VERSION, utcnow()),
        )
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return int(row["version"]) if row else 0


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]
