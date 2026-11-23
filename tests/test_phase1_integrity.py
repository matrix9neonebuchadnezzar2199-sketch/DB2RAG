"""Phase 1: append が既存点を消さない、dedup、秘密情報、件数検証。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from db2rag.db import DbManager, connect, migrate
from db2rag.embedding import Embedder
from db2rag.mysql_reader import write_blocked_from_grants
from db2rag.pipeline import create_job_from_files, create_job_from_mysql, create_rag, process_job
from db2rag.purge import purge_rag
from db2rag.qdrant_store import QdrantStore
from db2rag.search import search
from db2rag.secrets import decrypt_secret, load_or_create_key


def _stack(tmp_path: Path):
    conn = connect(tmp_path / "db.db")
    migrate(conn)
    store = QdrantStore(url="http://127.0.0.1:6333", path=str(tmp_path / "qdrant"))
    embedder = Embedder(backend="hash", models_dir=tmp_path / "models")
    return conn, store, embedder


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_files(conn, store, embedder, rag, name: str, acl: str, paths: list[Path], mode: str) -> str:
    created = create_job_from_files(
        conn,
        rag=rag,
        display_name=name,
        acl_tag=acl,
        paths=paths,
        mode=mode,
    )
    job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (created["job_id"],)).fetchone()
    process_job(conn, store, embedder, dict(job))
    return created["rag_id"]


def _child_count(conn, rag_id: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS n FROM child_chunks c
            JOIN parent_chunks p ON c.parent_chunk_id=p.parent_chunk_id
            JOIN document_versions d ON p.doc_version_id=d.doc_version_id
            JOIN source_objects o ON d.object_id=o.object_id
            JOIN sources s ON o.source_id=s.source_id
            WHERE s.rag_id=?
            """,
            (rag_id,),
        ).fetchone()["n"]
    )


def test_append_preserves_existing_documents(tmp_path: Path) -> None:
    conn, store, embedder = _stack(tmp_path)
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    _write(file_a, "年次有給休暇の申請は10日前まで。")
    _write(file_b, "通勤手当は月額15000円。")
    rag_id = _run_files(conn, store, embedder, None, "p1", "internal", [file_a], "create")
    rag = dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    _run_files(conn, store, embedder, rag, "p1", "internal", [file_b], "append")
    hit = search(
        conn, store, embedder, rag_id=rag_id, query="有給休暇", top_k=10, score_threshold=-1.0, acl_tags=["internal"]
    )
    blob = json.dumps(hit, ensure_ascii=False)
    assert "10日前" in blob
    hit_b = search(
        conn, store, embedder, rag_id=rag_id, query="通勤手当", top_k=10, score_threshold=-1.0, acl_tags=["internal"]
    )
    assert "15000" in json.dumps(hit_b, ensure_ascii=False)
    active = conn.execute("SELECT active_collection FROM rags WHERE rag_id=?", (rag_id,)).fetchone()["active_collection"]
    assert store.count(active) == _child_count(conn, rag_id)


def test_dedup_skips_unchanged_file(tmp_path: Path) -> None:
    conn, store, embedder = _stack(tmp_path)
    file_a = tmp_path / "a.txt"
    _write(file_a, "在宅勤務は週2日まで。")
    rag_id = _run_files(conn, store, embedder, None, "dedup", "internal", [file_a], "create")
    before_docs = conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"]
    before_points = store.count(conn.execute("SELECT active_collection FROM rags WHERE rag_id=?", (rag_id,)).fetchone()["active_collection"])
    rag = dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    _run_files(conn, store, embedder, rag, "dedup", "internal", [file_a], "append")
    after_docs = conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"]
    active = conn.execute("SELECT active_collection FROM rags WHERE rag_id=?", (rag_id,)).fetchone()["active_collection"]
    assert after_docs == before_docs
    assert store.count(active) == before_points


def test_update_replaces_old_points(tmp_path: Path) -> None:
    conn, store, embedder = _stack(tmp_path)
    file_a = tmp_path / "a.txt"
    _write(file_a, "旧文言アルファのみ。")
    rag_id = _run_files(conn, store, embedder, None, "upd", "internal", [file_a], "create")
    _write(file_a, "新文言ベータのみ。")
    rag = dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    _run_files(conn, store, embedder, rag, "upd", "internal", [file_a], "append")
    texts = [row["text"] for row in conn.execute("SELECT text FROM child_chunks").fetchall()]
    joined = "\n".join(texts)
    assert "ベータ" in joined
    assert "アルファ" not in joined
    active = conn.execute("SELECT active_collection FROM rags WHERE rag_id=?", (rag_id,)).fetchone()["active_collection"]
    assert store.count(active) == _child_count(conn, rag_id)


def test_verify_mismatch_fails_job(tmp_path: Path) -> None:
    conn, store, embedder = _stack(tmp_path)
    file_a = tmp_path / "a.txt"
    _write(file_a, "検証用の文書です。")
    rag_id = _run_files(conn, store, embedder, None, "verify", "internal", [file_a], "create")
    point_id = conn.execute("SELECT qdrant_point_id FROM child_chunks").fetchone()["qdrant_point_id"]
    active = conn.execute("SELECT active_collection FROM rags WHERE rag_id=?", (rag_id,)).fetchone()["active_collection"]
    store.delete_points(active, [point_id])
    rag = dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    created = create_job_from_files(conn, rag=rag, display_name="verify", acl_tag="internal", paths=[file_a], mode="append")
    job = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (created["job_id"],)).fetchone())
    process_job(conn, store, embedder, job)
    status = conn.execute("SELECT status, error_code FROM jobs WHERE job_id=?", (created["job_id"],)).fetchone()
    assert status["status"] == "failed"
    assert status["error_code"] == "ERR_VERIFY_COUNT_MISMATCH"
    still = conn.execute("SELECT active_collection, qdrant_alias FROM rags WHERE rag_id=?", (rag_id,)).fetchone()
    assert still["active_collection"] == active
    assert still["qdrant_alias"]


def test_mysql_password_encrypted_in_secrets(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "db.db")
    migrate(conn)

    class FakeCursor:
        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return [{"table_schema": "hr", "table_name": "staff", "table_type": "BASE TABLE", "table_rows": 1}]

        def close(self):
            return None

    class FakeConn:
        def cursor(self, dictionary=False):
            return FakeCursor()

        def close(self):
            return None

        def commit(self):
            return None

    monkeypatch.setattr("db2rag.pipeline.mysql_connect", lambda **kwargs: FakeConn())
    monkeypatch.setattr(
        "db2rag.pipeline.list_objects",
        lambda conn, database: [{"object_key": "hr.staff", "object_type": "table", "pk_columns": ["id"], "estimated_rows": 1, "selectable": True}],
    )
    monkeypatch.setattr("db2rag.mysql_reader._primary_key", lambda *args, **kwargs: ["id"])
    key = tmp_path / "secrets.key"
    rag = create_rag(conn, "mysql", "internal")
    create_job_from_mysql(
        conn,
        rag=rag,
        display_name="mysql",
        acl_tag="internal",
        mysql_config={"host": "127.0.0.1", "port": 3307, "user": "ro", "password": "s3cret-value", "database": "hr"},
        tables=["hr.staff"],
        mode="append",
        secrets_key_path=key,
    )
    stored = conn.execute("SELECT config_json FROM sources").fetchone()["config_json"]
    assert "s3cret-value" not in stored
    secret_id = json.loads(stored)["secret_id"]
    cipher = conn.execute("SELECT ciphertext FROM secrets WHERE secret_id=?", (secret_id,)).fetchone()["ciphertext"]
    assert decrypt_secret(load_or_create_key(key), bytes(cipher)) == "s3cret-value"
    purge_rag(conn, type("S", (), {"delete_alias": lambda *a, **k: None, "delete_collection": lambda *a, **k: None, "list_collection_names": lambda *a, **k: []})(), rag["rag_id"], uploads_dir=tmp_path / "up", exports_dir=tmp_path / "ex")
    assert conn.execute("SELECT COUNT(*) AS n FROM secrets").fetchone()["n"] == 0


def test_mysql_grants_classify_write() -> None:
    assert write_blocked_from_grants(["GRANT SELECT ON hr.* TO 'ro'@'%'"]) is True
    assert write_blocked_from_grants(["GRANT SELECT, INSERT ON hr.* TO 'rw'@'%'"]) is False
    assert write_blocked_from_grants([]) is True


def test_multithread_sqlite_access(tmp_path: Path) -> None:
    db = DbManager(tmp_path / "db.db")
    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            conn = db.connection()
            for n in range(10):
                with db.write_lock:
                    create_rag(conn, f"rag-{index}-{n}", "internal")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    total = db.connection().execute("SELECT COUNT(*) AS n FROM rags").fetchone()["n"]
    assert total == 40
