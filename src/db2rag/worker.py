"""単一 GPU/CPU worker。lease TTL 30s、heartbeat 10s。"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

from db2rag.config import Settings
from db2rag.constants import HEARTBEAT_SECONDS, LEASE_TTL_SECONDS
from db2rag.db import row_to_dict, utcnow
from db2rag.embedding import Embedder
from db2rag.pipeline import process_job
from db2rag.qdrant_store import QdrantStore


def acquire_lease(conn: sqlite3.Connection, worker_id: str) -> dict | None:
    now = datetime.now(UTC)
    expires = (now + timedelta(seconds=LEASE_TTL_SECONDS)).replace(microsecond=0).isoformat()
    now_s = now.replace(microsecond=0).isoformat()
    row = conn.execute(
        """
        SELECT job_id FROM jobs
        WHERE status IN ('pending', 'running', 'paused')
          AND status != 'cancelled'
          AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at < ?)
        ORDER BY created_at
        LIMIT 1
        """,
        (now_s,),
    ).fetchone()
    if row is None:
        return None
    job_id = row["job_id"]
    conn.execute(
        """
        UPDATE jobs
        SET lease_owner=?, lease_expires_at=?, status='running',
            started_at=COALESCE(started_at, ?)
        WHERE job_id=?
        """,
        (worker_id, expires, now_s, job_id),
    )
    conn.commit()
    return row_to_dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())


def heartbeat_once(conn: sqlite3.Connection, worker_id: str) -> None:
    expires = (datetime.now(UTC) + timedelta(seconds=LEASE_TTL_SECONDS)).replace(microsecond=0).isoformat()
    conn.execute(
        """
        UPDATE jobs SET lease_expires_at=?
        WHERE lease_owner=? AND status IN ('running', 'verifying')
        """,
        (expires, worker_id),
    )
    conn.commit()


def run_worker_loop(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    settings: Settings,
    stop: threading.Event,
) -> None:
    embedder.load()
    while not stop.is_set():
        job = acquire_lease(conn, settings.worker_id)
        if job is None:
            stop.wait(1.0)
            continue
        beat = threading.Event()

        def _heart() -> None:
            while not beat.is_set() and not stop.is_set():
                heartbeat_once(conn, settings.worker_id)
                beat.wait(HEARTBEAT_SECONDS)

        thread = threading.Thread(target=_heart, name="db2rag-heartbeat", daemon=True)
        thread.start()
        try:
            password = None
            process_job(conn, store, embedder, job, mysql_password=password)
        finally:
            beat.set()
            thread.join(timeout=2)
        if stop.is_set():
            break
