"""Phase 4: MySQL スキーマ追加、テンプレート、増分、ストリーミング。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from db2rag.api import mysql_job_config
from db2rag.db import _ensure_phase4_columns, connect, migrate, table_columns
from db2rag.embedding import Embedder
from db2rag.mysql_reader import (
    MysqlError,
    assert_readonly_sql,
    build_table_select,
    record_id_from,
    render_record_with_template,
)
from db2rag.pipeline import (
    MYSQL_BATCH_SIZE,
    create_job_from_mysql,
    iter_row_batches,
    mysql_point_ids_to_delete,
    process_job,
)
from db2rag.qdrant_store import QdrantStore


def test_schema_v1_migrates_to_v2(tmp_path: Path) -> None:
    conn = connect(tmp_path / "v1.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        CREATE TABLE source_objects (
            object_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_key TEXT NOT NULL,
            latest_doc_version_id TEXT,
            last_ingested_at TEXT
        );
        CREATE TABLE document_versions (
            doc_version_id TEXT PRIMARY KEY,
            object_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            file_hash TEXT,
            row_hash TEXT,
            extracted_bytes INTEGER NOT NULL DEFAULT 0,
            parent_count INTEGER NOT NULL DEFAULT 0,
            child_count INTEGER NOT NULL DEFAULT 0,
            pipeline_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO source_objects(object_id, source_id, object_type, object_key)
        VALUES ('obj1', 'src1', 'table', 'hr.staff')
        """
    )
    conn.execute(
        """
        INSERT INTO document_versions(
            doc_version_id, object_id, content_hash, pipeline_fingerprint, created_at
        ) VALUES ('doc1', 'obj1', 'hash1', 'fp', '2024-01-01T00:00:00+00:00')
        """
    )
    migrate(conn)
    _ensure_phase4_columns(conn)
    _ensure_phase4_columns(conn)
    assert {"record_id", "is_latest"} <= table_columns(conn, "document_versions")
    assert {"sync_cursor", "config_json"} <= table_columns(conn, "source_objects")
    row = conn.execute(
        "SELECT content_hash, record_id, is_latest FROM document_versions WHERE doc_version_id='doc1'"
    ).fetchone()
    assert row["content_hash"] == "hash1"
    assert row["record_id"] is None
    assert int(row["is_latest"]) == 1
    obj = conn.execute(
        "SELECT sync_cursor, config_json FROM source_objects WHERE object_id='obj1'"
    ).fetchone()
    assert obj["sync_cursor"] is None
    assert obj["config_json"] == "{}"
    indexes = {item["name"] for item in conn.execute("PRAGMA index_list('document_versions')")}
    assert "idx_doc_object_record" in indexes
    assert conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"] == 1
    migrate(conn)


def test_template_and_lookup_rendering() -> None:
    template = {
        "title_column": "contract_name",
        "updated_at_column": "updated_at",
        "acl_column": "acl_tag",
        "sections": [
            {
                "heading": "基本情報",
                "fields": [
                    {"label": "契約日", "column": "contract_date", "format": "date_ja"},
                ],
            },
            {
                "heading": "詳細",
                "fields": [
                    {"label": "説明", "column": "description", "type": "long_text"},
                ],
            },
            {
                "heading": "ステータス",
                "fields": [
                    {
                        "label": "ステータス",
                        "column": "status_code",
                        "lookup_table": "status_master",
                        "lookup_key": "code",
                        "lookup_label": "label",
                    }
                ],
            },
        ],
    }
    row = {
        "contract_name": "基本契約",
        "contract_date": "2024-03-05",
        "description": "これは長い本文です。",
        "status_code": "01",
        "__label_status_code": "承認済",
        "updated_at": "2024-03-06T01:02:03+00:00",
        "acl_tag": "hr",
        "bad_date": "2024-13-40",
    }
    text, title, updated_at, acl = render_record_with_template("sales.contracts", "id=9", row, template)
    assert title == "基本契約"
    assert "2024年3月5日" in text
    assert "コード 01 → 承認済" in text
    assert "これは長い本文です。" in text
    assert "- 説明:" not in text
    assert updated_at == "2024-03-06T01:02:03+00:00"
    assert acl == "hr"
    broken = dict(row)
    broken["contract_date"] = "2024-13-40"
    broken_text, _, _, _ = render_record_with_template("sales.contracts", "id=9", broken, template)
    assert "2024-13-40" in broken_text
    plain, plain_title, plain_updated, plain_acl = render_record_with_template(
        "hr.staff",
        "id=1",
        {"id": 1, "name": "alpha"},
        None,
    )
    assert plain_title == "hr.staff id=1"
    assert plain_updated is None
    assert plain_acl is None
    assert "alpha" in plain
    sql, params = build_table_select(
        "hr",
        "staff",
        where_col="updated_at",
        since_value="2024-01-01",
        lookups=[
            {
                "column": "status_code",
                "lookup_table": "status_master",
                "lookup_key": "code",
                "lookup_label": "label",
            }
        ],
    )
    assert "LEFT JOIN `hr`.`status_master` AS `__lk_0`" in sql
    assert "`__label_status_code`" in sql
    assert "WHERE `base`.`updated_at` > %s ORDER BY `base`.`updated_at` ASC" in sql
    assert params == ("2024-01-01",)
    with pytest.raises(MysqlError):
        build_table_select("hr", "bad name", where_col=None, since_value=None, lookups=None)


def test_custom_sql_readonly_guard() -> None:
    assert assert_readonly_sql("SELECT id FROM staff").startswith("SELECT")
    assert assert_readonly_sql("WITH c AS (SELECT 1 AS n) SELECT n FROM c").startswith("WITH")
    assert assert_readonly_sql("  select 1;  ") == "select 1"
    for sql in (
        "DELETE FROM staff",
        "INSERT INTO staff(id) VALUES (1)",
        "DROP TABLE staff",
        "SELECT 1; SELECT 2",
        "SELECT * FROM staff INTO OUTFILE '/tmp/x'",
    ):
        with pytest.raises(MysqlError):
            assert_readonly_sql(sql)


def test_composite_pk_sorted_deterministically() -> None:
    row = {"a_id": 1, "b_id": 2}
    assert record_id_from(["b_id", "a_id"], row) == "a_id=1|b_id=2"
    assert record_id_from(["a_id", "b_id"], row) == "a_id=1|b_id=2"


class _CountingRows:
    """next() 回数を数える。list() されると一気に 650 まで進む。"""

    def __init__(self, count: int) -> None:
        self.count = count
        self.pulls = 0
        self._index = 0

    def __iter__(self) -> _CountingRows:
        return self

    def __next__(self) -> dict[str, Any]:
        if self._index >= self.count:
            raise StopIteration
        self._index += 1
        self.pulls += 1
        return {"id": self._index, "body": f"row-{self._index}-TOKEN"}


class _FakeMysql:
    def close(self) -> None:
        return None


def _install_mysql(monkeypatch: pytest.MonkeyPatch, iterator_factory: Any) -> None:
    monkeypatch.setattr("db2rag.pipeline.mysql_connect", lambda **kwargs: _FakeMysql())
    monkeypatch.setattr(
        "db2rag.pipeline.list_objects",
        lambda conn, database: [
            {
                "object_key": "hr.staff",
                "object_type": "table",
                "pk_columns": ["id"],
                "estimated_rows": 3,
                "selectable": True,
            }
        ],
    )
    monkeypatch.setattr("db2rag.pipeline.iter_table_rows", iterator_factory)


def _run_mysql(conn, store, embedder, rag, key: Path, mode: str) -> str:
    created = create_job_from_mysql(
        conn,
        rag=rag,
        display_name="mysql",
        acl_tag="internal",
        mysql_config={
            "host": "127.0.0.1",
            "port": 3306,
            "user": "ro",
            "password": "s3cret-value",
            "database": "hr",
        },
        tables=["hr.staff"],
        mode=mode,
        secrets_key_path=key,
    )
    job = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (created["job_id"],)).fetchone())
    process_job(conn, store, embedder, job, secrets_key_path=key)
    status = conn.execute(
        "SELECT status, error_code, error_message FROM jobs WHERE job_id=?",
        (created["job_id"],),
    ).fetchone()
    assert status["status"] == "completed", (status["error_code"], status["error_message"])
    return str(created["rag_id"])


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


def test_incremental_sync_skips_unchanged_and_replaces_updated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert mysql_point_ids_to_delete("same", "same", ["old-point"]) is None
    assert mysql_point_ids_to_delete("old", "new", ["old-point"]) == ["old-point"]
    assert mysql_point_ids_to_delete(None, "new", []) == []
    first = [
        {"id": 1, "name": "ALPHA-UNCHANGED", "updated_at": "2024-01-01T00:00:00+00:00"},
        {"id": 2, "name": "BETA-OLD", "updated_at": "2024-01-02T00:00:00+00:00"},
        {"id": 3, "name": "GAMMA-KEEP", "updated_at": "2024-01-03T00:00:00+00:00"},
    ]
    second = [
        {"id": 1, "name": "ALPHA-UNCHANGED", "updated_at": "2024-01-01T00:00:00+00:00"},
        {"id": 2, "name": "BETA-NEW", "updated_at": "2024-02-02T00:00:00+00:00"},
        {"id": 4, "name": "DELTA-ADDED", "updated_at": "2024-03-04T00:00:00+00:00"},
    ]
    waves = [first, second]
    state = {"n": 0}

    def fake_iter(conn, schema, table, **kwargs):
        rows = waves[state["n"]]
        state["n"] += 1
        return iter(rows)

    _install_mysql(monkeypatch, fake_iter)
    conn = connect(tmp_path / "db.db")
    migrate(conn)
    store = QdrantStore(url="http://127.0.0.1:6333", path=str(tmp_path / "qdrant"))
    embedder = Embedder(backend="hash", models_dir=tmp_path / "models")
    key = tmp_path / "secrets.key"
    rag_id = _run_mysql(conn, store, embedder, None, key, "create")
    rag = dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    active = rag["active_collection"]
    old_point = conn.execute(
        "SELECT qdrant_point_id FROM child_chunks WHERE text LIKE '%BETA-OLD%'"
    ).fetchone()["qdrant_point_id"]
    calls = {"n": 0}
    texts: list[str] = []
    inner = embedder.encode_documents

    def wrapped(batch_texts: list[str]):
        calls["n"] += 1
        texts.extend(batch_texts)
        return inner(batch_texts)

    embedder.encode_documents = wrapped  # type: ignore[method-assign]
    _run_mysql(conn, store, embedder, rag, key, "append")
    joined = "\n".join(texts)
    assert calls["n"] == 1
    assert "ALPHA-UNCHANGED" not in joined
    assert "BETA-NEW" in joined
    assert "DELTA-ADDED" in joined
    stored = "\n".join(row["text"] for row in conn.execute("SELECT text FROM child_chunks"))
    assert "BETA-OLD" not in stored
    assert "BETA-NEW" in stored
    assert "ALPHA-UNCHANGED" in stored
    assert "GAMMA-KEEP" in stored
    assert "DELTA-ADDED" in stored
    still = conn.execute("SELECT active_collection, staging_collection FROM rags WHERE rag_id=?", (rag_id,)).fetchone()
    assert still["active_collection"] == active
    assert still["staging_collection"] is None
    assert store.count(active) == _child_count(conn, rag_id)
    found = store.client.retrieve(collection_name=active, ids=[old_point])
    assert all(str(point.id) != old_point for point in found)
    latest = {
        row["record_id"]
        for row in conn.execute("SELECT record_id FROM document_versions WHERE is_latest=1")
    }
    assert latest == {"id=1", "id=2", "id=3", "id=4"}
    flags = {
        int(row["is_latest"])
        for row in conn.execute("SELECT is_latest FROM document_versions WHERE record_id='id=2'")
    }
    assert flags == {0, 1}
    cursor = conn.execute("SELECT sync_cursor FROM source_objects").fetchone()["sync_cursor"]
    assert cursor == "2024-03-04T00:00:00+00:00"


def test_streaming_batches_without_full_materialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MYSQL_BATCH_SIZE == 200
    probe = _CountingRows(650)
    observed = [(len(batch), probe.pulls) for batch in iter_row_batches(probe, MYSQL_BATCH_SIZE)]
    assert observed == [(200, 200), (200, 400), (200, 600), (50, 650)]
    holder: dict[str, _CountingRows] = {}

    def fake_iter(conn, schema, table, **kwargs):
        counter = _CountingRows(650)
        holder["rows"] = counter
        return counter

    _install_mysql(monkeypatch, fake_iter)
    conn = connect(tmp_path / "db.db")
    migrate(conn)
    store = QdrantStore(url="http://127.0.0.1:6333", path=str(tmp_path / "qdrant"))
    embedder = Embedder(backend="hash", models_dir=tmp_path / "models")
    pulls_at_encode: list[int] = []
    inner = embedder.encode_documents

    def wrapped(batch_texts: list[str]):
        pulls_at_encode.append(holder["rows"].pulls)
        return inner(batch_texts)

    embedder.encode_documents = wrapped  # type: ignore[method-assign]
    rag_id = _run_mysql(conn, store, embedder, None, tmp_path / "secrets.key", "create")
    assert pulls_at_encode == [200, 400, 600, 650]
    assert holder["rows"].pulls == 650
    active = conn.execute("SELECT active_collection FROM rags WHERE rag_id=?", (rag_id,)).fetchone()["active_collection"]
    assert store.count(active) == 650
    assert _child_count(conn, rag_id) == 650


def test_job_config_keeps_template_without_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mysql(monkeypatch, lambda *args, **kwargs: iter(()))
    conn = connect(tmp_path / "db.db")
    migrate(conn)
    config = mysql_job_config(
        {"host": "127.0.0.1", "port": 3306, "user": "ro", "password": "s3cret-value", "database": "hr"},
        templates={"hr.staff": {"title_column": "name", "updated_at_column": "updated_at"}},
        pk_overrides={"hr.staff": ["id"]},
        custom_sql=[{"object_key": "hr.staff", "sql": "SELECT id, name FROM staff", "pk_columns": ["id"]}],
    )
    create_job_from_mysql(
        conn,
        rag=None,
        display_name="opts",
        acl_tag="internal",
        mysql_config=config,
        tables=["hr.staff"],
        mode="create",
        secrets_key_path=tmp_path / "secrets.key",
    )
    stored = json.loads(conn.execute("SELECT config_json FROM sources").fetchone()["config_json"])
    assert "s3cret-value" not in json.dumps(stored)
    assert stored["template"]["hr.staff"]["title_column"] == "name"
    assert stored["pk_columns"]["hr.staff"] == ["id"]
    assert stored["custom_sql"][0]["sql"].startswith("SELECT")
    assert stored["secret_id"]
