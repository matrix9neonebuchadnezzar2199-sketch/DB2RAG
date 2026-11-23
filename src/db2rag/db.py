"""SQLite WAL 制御 DB。ベクトルは持たない。"""

from __future__ import annotations

import sqlite3
import threading
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
    sync_cursor TEXT,
    config_json TEXT DEFAULT '{}',
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
    record_id TEXT,
    is_latest INTEGER NOT NULL DEFAULT 1,
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
CREATE INDEX IF NOT EXISTS idx_doc_object_record ON document_versions(object_id, record_id, is_latest);
"""

# v1 既存 DB では record_id 列が無いので、この索引だけ executescript の後段に回す。
_PHASE4_INDEX_MARKER = "CREATE INDEX IF NOT EXISTS idx_doc_object_record"
_PHASE4_TABLES = frozenset({"document_versions", "source_objects"})


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


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """PRAGMA table_info で列名を返す。

    Args:
        conn: 開いている SQLite 接続。
        table: ``document_versions`` または ``source_objects``。

    Returns:
        列名の集合。表が無いときは空。

    Raises:
        ValueError: 許可していない表名のとき。
    """
    if table not in _PHASE4_TABLES:
        raise ValueError(f"unknown table: {table}")
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _split_phase4_index(script: str) -> tuple[str, str]:
    """索引 DDL を本体から切り離す。v1 表にはまだ対象列が無い。"""
    head, sep, tail = script.partition(_PHASE4_INDEX_MARKER)
    if not sep:
        return script, ""
    return head, sep + tail


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """列が無いときだけ ALTER する。ddl は呼び出し側の固定句。"""
    if column in table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _ensure_phase4_columns(conn: sqlite3.Connection) -> None:
    """Phase 4 の列と索引を冪等に足す。

    SQLite の ALTER は既存行へ NOT NULL を付けにくい。
    ``is_latest`` は ``INTEGER DEFAULT 1`` とし、NULL は 1 に埋め戻す。
    ``SQLITE_SCHEMA_VERSION`` は変更しない。

    Args:
        conn: マイグレーション対象の接続。
    """
    _add_column_if_missing(conn, "document_versions", "record_id", "TEXT")
    _add_column_if_missing(conn, "document_versions", "is_latest", "INTEGER DEFAULT 1")
    if "is_latest" in table_columns(conn, "document_versions"):
        conn.execute("UPDATE document_versions SET is_latest=1 WHERE is_latest IS NULL")
    _add_column_if_missing(conn, "source_objects", "sync_cursor", "TEXT")
    _add_column_if_missing(conn, "source_objects", "config_json", "TEXT DEFAULT '{}'")
    if "config_json" in table_columns(conn, "source_objects"):
        conn.execute("UPDATE source_objects SET config_json='{}' WHERE config_json IS NULL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_doc_object_record "
        "ON document_versions(object_id, record_id, is_latest)"
    )


def migrate(conn: sqlite3.Connection) -> None:
    """未作成の表を作り、既存 DB には Phase 4 列を冪等に足す。

    Args:
        conn: 起動時の SQLite 接続。
    """
    # 索引は列追加の後。v1 の既存表に対して先に作ると record_id が無くて失敗する。
    head, index_sql = _split_phase4_index(SCHEMA_SQL)
    conn.executescript(head)
    _ensure_phase4_columns(conn)
    if index_sql.strip():
        conn.executescript(index_sql)
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


class DbManager:
    """スレッドごとに別接続を持つ。接続オブジェクトをスレッド間で共有しない。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()
        self._guard = threading.Lock()
        self._all: list[sqlite3.Connection] = []
        self.write_lock = threading.RLock()

    def connection(self) -> sqlite3.Connection:
        """呼び出しスレッド専用の接続を返す。初回だけ schema を適用する。"""
        existing = getattr(self._local, "conn", None)
        if isinstance(existing, sqlite3.Connection):
            return existing
        # 複数スレッドが同時に migrate すると SQLite が database is locked になる。
        with self.write_lock:
            conn = connect(self.path)
            migrate(conn)
            self._local.conn = conn
            with self._guard:
                self._all.append(conn)
        return conn

    def close_all(self) -> None:
        """プロセス終了時に、生成済み接続をすべて閉じる。"""
        with self._guard:
            conns = list(self._all)
            self._all.clear()
        for conn in conns:
            conn.close()
        self._local.conn = None


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    # Row の反復は値であり列名ではない。keys() を残す。
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]  # noqa: SIM118
