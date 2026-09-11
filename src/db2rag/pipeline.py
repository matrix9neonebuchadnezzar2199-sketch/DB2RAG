"""取り込みパイプライン。discover → activate。"""

from __future__ import annotations

import getpass
import json
import sqlite3
from pathlib import Path
from typing import Any

from db2rag.capacity import estimate_from_bytes
from db2rag.chunking import ChunkResult, chunk_document
from db2rag.constants import (
    CHUNK_STRATEGY,
    EMBED_DIM,
    PARSER_VERSION,
    PREFIX_DOC,
    PREFIX_QUERY,
    RURI_MODEL_ID,
    RURI_REVISION,
    SCHEMA_VERSION,
    SPARSE_INDEX,
)
from db2rag.db import row_to_dict, rows_to_dicts, utcnow
from db2rag.embedding import Embedder
from db2rag.ids import new_id
from db2rag.mysql_reader import (
    connect as mysql_connect,
)
from db2rag.mysql_reader import (
    iter_table_rows,
    list_objects,
    record_id_from,
    render_record,
    row_hash,
)
from db2rag.parsers import (
    EXCLUDED_DIRS,
    EXCLUDED_SUFFIXES,
    MAX_FILE_BYTES,
    MAX_FILES_PER_JOB,
    mime_for,
    parse_file,
)
from db2rag.parsers.canonical import Block, CanonicalDocument
from db2rag.qdrant_store import QdrantStore, make_point
from db2rag.textnorm import normalize_text, sha256_file, sha256_text

PIPELINE_FINGERPRINT = sha256_text(
    "|".join(
        [
            PARSER_VERSION,
            CHUNK_STRATEGY,
            f"{RURI_MODEL_ID}@{RURI_REVISION}",
            PREFIX_DOC,
            PREFIX_QUERY,
            "normalize=true",
            "precision=fp32",
        ]
    )
)


def alias_for(rag_id: str) -> str:
    return f"db2rag_{rag_id}"


def create_rag(conn: sqlite3.Connection, display_name: str, acl_tag: str) -> dict[str, Any]:
    rag_id = new_id("rag")
    now = utcnow()
    alias = alias_for(rag_id)
    conn.execute(
        """
        INSERT INTO rags(rag_id, display_name, status, qdrant_alias, schema_version,
                           acl_tag_default, created_at, updated_at)
        VALUES (?, ?, 'draft', ?, ?, ?, ?, ?)
        """,
        (rag_id, display_name, alias, SCHEMA_VERSION, acl_tag, now, now),
    )
    conn.commit()
    return row_to_dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone()) or {}


def list_rags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM rags WHERE status != 'purged' ORDER BY updated_at DESC").fetchall()
    result = []
    for rag in rows_to_dicts(rows):
        rag["child_count"] = conn.execute(
            """
            SELECT COUNT(*) AS n FROM child_chunks c
            JOIN parent_chunks p ON c.parent_chunk_id = p.parent_chunk_id
            JOIN document_versions d ON p.doc_version_id = d.doc_version_id
            JOIN source_objects o ON d.object_id = o.object_id
            JOIN sources s ON o.source_id = s.source_id
            WHERE s.rag_id=?
            """,
            (rag["rag_id"],),
        ).fetchone()["n"]
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE rag_id=? AND status IN ('pending','running','verifying')",
            (rag["rag_id"],),
        ).fetchone()["n"]
        rag["running_jobs"] = running
        result.append(rag)
    return result


def create_job_from_files(
    conn: sqlite3.Connection,
    *,
    rag: dict[str, Any] | None,
    display_name: str,
    acl_tag: str,
    paths: list[Path],
    mode: str,
) -> dict[str, Any]:
    if rag is None:
        rag = create_rag(conn, display_name, acl_tag)
        mode = "create"
    rag_id = rag["rag_id"]
    source_id = new_id("src")
    now = utcnow()
    conn.execute(
        """
        INSERT INTO sources(source_id, rag_id, source_type, source_name, config_json,
                             acl_tag_default, enabled, created_at)
        VALUES (?, ?, 'file', ?, ?, ?, 1, ?)
        """,
        (source_id, rag_id, "files", json.dumps({"paths": [str(p) for p in paths]}), acl_tag, now),
    )
    job_id = new_id("job")
    conn.execute(
        """
        INSERT INTO jobs(job_id, rag_id, mode, status, created_by, created_at, work_units_total)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
        """,
        (job_id, rag_id, mode, getpass.getuser(), now, 0),
    )
    files = _discover_files(paths)
    for path in files:
        object_id = new_id("obj")
        conn.execute(
            """
            INSERT INTO source_objects(object_id, source_id, object_type, object_key)
            VALUES (?, ?, 'file', ?)
            """,
            (object_id, source_id, _posix_key(path)),
        )
        item_id = new_id("item")
        conn.execute(
            """
            INSERT INTO job_items(job_item_id, job_id, object_id, status, checkpoint)
            VALUES (?, ?, ?, 'pending', 'discovered')
            """,
            (item_id, job_id, object_id),
        )
    conn.execute(
        "UPDATE jobs SET work_units_total=? WHERE job_id=?",
        (float(len(files)), job_id),
    )
    conn.commit()
    return {"job_id": job_id, "rag_id": rag_id, "item_count": len(files)}


def create_job_from_mysql(
    conn: sqlite3.Connection,
    *,
    rag: dict[str, Any] | None,
    display_name: str,
    acl_tag: str,
    mysql_config: dict[str, Any],
    tables: list[str],
    mode: str,
) -> dict[str, Any]:
    # 空選択を「全表」に読み替えない。INSERT 前に弾き、共有接続の rollback を避ける。
    if not tables:
        raise ValueError("tables required: select at least one MySQL object")
    if rag is None:
        rag = create_rag(conn, display_name, acl_tag)
        mode = "create"
    rag_id = rag["rag_id"]
    source_id = new_id("src")
    now = utcnow()
    # lab 接続情報は source に保持する。本番では secrets テーブルへ移す。
    conn.execute(
        """
        INSERT INTO sources(source_id, rag_id, source_type, source_name, config_json,
                             acl_tag_default, enabled, created_at)
        VALUES (?, ?, 'mysql', ?, ?, ?, 1, ?)
        """,
        (source_id, rag_id, mysql_config.get("host", "mysql"), json.dumps(mysql_config), acl_tag, now),
    )
    job_id = new_id("job")
    conn.execute(
        """
        INSERT INTO jobs(job_id, rag_id, mode, status, created_by, created_at, work_units_total)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
        """,
        (job_id, rag_id, mode, getpass.getuser(), now, 0),
    )
    mysql_conn = mysql_connect(
        host=mysql_config["host"],
        port=int(mysql_config["port"]),
        user=mysql_config["user"],
        password=mysql_config["password"],
        database=mysql_config["database"],
        ssl_mode=mysql_config.get("ssl_mode", "DISABLED"),
    )
    available = {item["object_key"]: item for item in list_objects(conn=mysql_conn, database=mysql_config["database"])}
    mysql_conn.close()
    selected = tables
    count = 0
    for key in selected:
        meta = available.get(key)
        if meta is None:
            continue
        object_id = new_id("obj")
        conn.execute(
            """
            INSERT INTO source_objects(object_id, source_id, object_type, object_key)
            VALUES (?, ?, ?, ?)
            """,
            (object_id, source_id, meta["object_type"], key),
        )
        conn.execute(
            """
            INSERT INTO job_items(job_item_id, job_id, object_id, status, checkpoint)
            VALUES (?, ?, ?, 'pending', 'discovered')
            """,
            (new_id("item"), job_id, object_id),
        )
        count += 1
    conn.execute("UPDATE jobs SET work_units_total=? WHERE job_id=?", (float(max(count, 1)), job_id))
    conn.commit()
    return {"job_id": job_id, "rag_id": rag_id, "item_count": count}


def process_job(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    job: dict[str, Any],
    *,
    mysql_password: str | None = None,
) -> None:
    rag = row_to_dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (job["rag_id"],)).fetchone())
    if rag is None or rag.get("status") == "purged":
        return
    collection = rag.get("staging_collection")
    if not collection:
        collection = f"db2rag_{rag['rag_id']}_staging_{job['job_id']}"
        store.create_collection(collection)
        conn.execute(
            "UPDATE rags SET staging_collection=?, updated_at=? WHERE rag_id=?",
            (collection, utcnow(), rag["rag_id"]),
        )
        conn.commit()
    items = rows_to_dicts(
        conn.execute(
            "SELECT * FROM job_items WHERE job_id=? AND status IN ('pending','running','failed')",
            (job["job_id"],),
        ).fetchall()
    )
    for item in items:
        current = row_to_dict(conn.execute("SELECT status FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone())
        if current and current["status"] in {"cancelled"}:
            return
        _process_item(conn, store, embedder, rag, job, item, collection, mysql_password=mysql_password)
        _update_progress(conn, job["job_id"])
    current = row_to_dict(conn.execute("SELECT status FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone())
    if current and current["status"] in {"cancelled"}:
        return
    rag_now = row_to_dict(conn.execute("SELECT status FROM rags WHERE rag_id=?", (rag["rag_id"],)).fetchone())
    if rag_now is None or rag_now.get("status") == "purged":
        return
    _verify_and_activate(conn, store, rag, job, collection)


def _process_item(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    rag: dict[str, Any],
    job: dict[str, Any],
    item: dict[str, Any],
    collection: str,
    mysql_password: str | None,
) -> None:
    object_row = row_to_dict(
        conn.execute("SELECT * FROM source_objects WHERE object_id=?", (item["object_id"],)).fetchone()
    )
    source = row_to_dict(
        conn.execute("SELECT * FROM sources WHERE source_id=?", (object_row["source_id"],)).fetchone()
    )
    conn.execute(
        "UPDATE job_items SET status='running' WHERE job_item_id=?",
        (item["job_item_id"],),
    )
    conn.execute(
        "UPDATE jobs SET current_item_name=?, status='running' WHERE job_id=?",
        (object_row["object_key"], job["job_id"]),
    )
    conn.commit()
    try:
        documents = _extract_documents(source, object_row, mysql_password)
        all_parents = 0
        all_children = 0
        n_docs = max(len(documents), 1)
        for doc_i, (document, meta) in enumerate(documents, start=1):
            _touch_item_progress(conn, job["job_id"], object_row["object_key"], doc_i, n_docs)
            text = normalize_text(document.plain_text())
            content_hash = sha256_text(text)
            existing = conn.execute(
                "SELECT doc_version_id FROM document_versions WHERE object_id=? AND content_hash=?",
                (object_row["object_id"], content_hash),
            ).fetchone()
            if existing:
                continue
            kind = "mysql" if source["source_type"] == "mysql" else None
            chunks = chunk_document(document, kind=kind)
            if not chunks.children:
                continue
            now = utcnow()
            doc_id = new_id("docv")
            conn.execute(
                """
                INSERT INTO document_versions(doc_version_id, object_id, content_hash, file_hash, row_hash,
                    extracted_bytes, parent_count, child_count, pipeline_fingerprint, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    object_row["object_id"],
                    content_hash,
                    meta.get("file_hash"),
                    meta.get("row_hash"),
                    len(text.encode("utf-8")),
                    len(chunks.parents),
                    len(chunks.children),
                    PIPELINE_FINGERPRINT,
                    now,
                ),
            )
            for parent in chunks.parents:
                conn.execute(
                    """
                    INSERT INTO parent_chunks(parent_chunk_id, doc_version_id, parent_index, text,
                        section_path, page_from, page_to, token_estimate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parent.parent_chunk_id,
                        doc_id,
                        parent.parent_index,
                        parent.text,
                        parent.section_path,
                        parent.page_from,
                        parent.page_to,
                        parent.token_estimate,
                    ),
                )
            texts = [child.text for child in chunks.children]
            vectors = embedder.encode_documents(texts)
            points = []
            for child, vector in zip(chunks.children, vectors, strict=True):
                conn.execute(
                    """
                    INSERT INTO child_chunks(child_chunk_id, parent_chunk_id, child_index, text,
                        char_count, qdrant_point_id, embedded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        child.child_chunk_id,
                        child.parent_chunk_id,
                        child.child_index,
                        child.text,
                        child.char_count,
                        child.qdrant_point_id,
                        now,
                    ),
                )
                parent = next(p for p in chunks.parents if p.parent_chunk_id == child.parent_chunk_id)
                payload = _payload(
                    rag=rag,
                    source=source,
                    object_row=object_row,
                    document=document,
                    parent=parent,
                    child=child,
                    content_hash=sha256_text(child.text),
                    doc_content_hash=content_hash,
                    extra=meta,
                )
                points.append(
                    make_point(
                        point_id=child.qdrant_point_id,
                        dense=vector.tolist(),
                        text=child.text,
                        payload=payload,
                    )
                )
            store.upsert_points(collection, points)
            conn.execute(
                "UPDATE source_objects SET latest_doc_version_id=?, last_ingested_at=? WHERE object_id=?",
                (doc_id, now, object_row["object_id"]),
            )
            all_parents += len(chunks.parents)
            all_children += len(chunks.children)
            _touch_item_progress(conn, job["job_id"], object_row["object_key"], doc_i, n_docs)
        conn.execute(
            "UPDATE job_items SET status='completed', checkpoint='verified', error_code=NULL, error_message=NULL WHERE job_item_id=?",
            (item["job_item_id"],),
        )
        conn.commit()
        _ = (all_parents, all_children)
    except Exception as exc:
        conn.execute(
            """
            UPDATE job_items SET status='failed', error_code=?, error_message=?, retry_count=retry_count+1
            WHERE job_item_id=?
            """,
            (type(exc).__name__, str(exc)[:500], item["job_item_id"]),
        )
        conn.commit()


def _extract_documents(
    source: dict[str, Any],
    object_row: dict[str, Any],
    mysql_password: str | None,
) -> list[tuple[CanonicalDocument, dict[str, Any]]]:
    if source["source_type"] == "file":
        path = _resolve_file(source, object_row["object_key"])
        if path.is_symlink():
            raise ValueError("ERR_FILE_SYMLINK")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("ERR_FILE_TOO_LARGE")
        document = parse_file(path)
        return [(document, {"file_hash": sha256_file(path), "path": str(path), "mime": mime_for(path)})]
    config = json.loads(source["config_json"])
    schema, table = object_row["object_key"].split(".", 1)
    mysql_conn = mysql_connect(
        host=config["host"],
        port=int(config["port"]),
        user=config["user"],
        password=mysql_password or config.get("password", ""),
        database=config["database"],
        ssl_mode=config.get("ssl_mode", "DISABLED"),
    )
    pk = []
    for obj in list_objects(mysql_conn, config["database"]):
        if obj["object_key"] == object_row["object_key"]:
            pk = obj["pk_columns"]
            break
    docs: list[tuple[CanonicalDocument, dict[str, Any]]] = []
    for row in iter_table_rows(mysql_conn, schema, table):
        rec_id = record_id_from(pk, row)
        text = render_record(object_row["object_key"], rec_id, row)
        document = CanonicalDocument(
            title=f"{object_row['object_key']} {rec_id}",
            language="ja",
            mime="text/plain",
            blocks=[Block(type="paragraph", text=text)],
            extra={"kind": "mysql", "source_type": "mysql"},
        )
        docs.append(
            (
                document,
                {
                    "row_hash": row_hash(row),
                    "record_id": rec_id,
                    "mysql_schema": schema,
                    "mysql_table": table,
                    "mysql_pk": {col: row.get(col) for col in pk},
                },
            )
        )
    mysql_conn.close()
    return docs


def _verify_and_activate(
    conn: sqlite3.Connection,
    store: QdrantStore,
    rag: dict[str, Any],
    job: dict[str, Any],
    collection: str,
) -> None:
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM job_items WHERE job_id=? AND status='failed'",
        (job["job_id"],),
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM job_items WHERE job_id=? AND status IN ('pending','running')",
        (job["job_id"],),
    ).fetchone()["n"]
    if pending:
        return
    conn.execute("UPDATE jobs SET status='verifying' WHERE job_id=?", (job["job_id"],))
    conn.commit()
    sqlite_children = conn.execute(
        """
        SELECT COUNT(*) AS n FROM child_chunks c
        JOIN parent_chunks p ON c.parent_chunk_id=p.parent_chunk_id
        JOIN document_versions d ON p.doc_version_id=d.doc_version_id
        JOIN source_objects o ON d.object_id=o.object_id
        JOIN sources s ON o.source_id=s.source_id
        WHERE s.rag_id=?
        """,
        (rag["rag_id"],),
    ).fetchone()["n"]
    qcount = store.count(collection)
    now = utcnow()
    if failed and qcount == 0:
        conn.execute(
            "UPDATE jobs SET status='failed', error_code=?, error_message=?, finished_at=?, progress_pct=100 WHERE job_id=?",
            ("ERR_JOB_ITEMS_FAILED", f"{failed} items failed", now, job["job_id"]),
        )
        conn.commit()
        return
    alias = rag["qdrant_alias"]
    store.set_alias(alias, collection)
    old = rag.get("active_collection")
    conn.execute(
        """
        UPDATE rags SET status='active', active_collection=?, staging_collection=NULL, updated_at=?
        WHERE rag_id=?
        """,
        (collection, now, rag["rag_id"]),
    )
    if old and old != collection:
        conn.execute(
            "INSERT OR REPLACE INTO archived_collections(collection_name, rag_id, archived_at, delete_after) VALUES (?, ?, ?, ?)",
            (old, rag["rag_id"], now, now),
        )
    conn.execute(
        """
        UPDATE jobs SET status='completed', progress_pct=100, work_units_done=work_units_total,
            finished_at=?, eta_seconds=0, current_item_name=NULL
        WHERE job_id=?
        """,
        (now, job["job_id"]),
    )
    conn.commit()
    _ = sqlite_children


def _payload(
    *,
    rag: dict[str, Any],
    source: dict[str, Any],
    object_row: dict[str, Any],
    document: CanonicalDocument,
    parent: Any,
    child: Any,
    content_hash: str,
    doc_content_hash: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    now = utcnow()
    return {
        "schema_version": SCHEMA_VERSION,
        "rag_id": rag["rag_id"],
        "source_type": source["source_type"],
        "source_name": source["source_name"],
        "object_id": object_row["object_id"],
        "record_id": extra.get("record_id") or extra.get("path") or object_row["object_key"],
        "path": extra.get("path"),
        "title": document.title or object_row["object_key"],
        "section_path": parent.section_path,
        "page_from": parent.page_from,
        "page_to": parent.page_to,
        "parent_chunk_id": child.parent_chunk_id,
        "child_chunk_id": child.child_chunk_id,
        "parent_index": parent.parent_index,
        "child_index": child.child_index,
        "created_at": now,
        "updated_at": extra.get("updated_at"),
        "content_hash": content_hash,
        "doc_content_hash": doc_content_hash,
        "hash": content_hash,
        "mime": extra.get("mime") or document.mime,
        "language": "ja",
        "acl_tag": source["acl_tag_default"],
        "pipeline_fingerprint": PIPELINE_FINGERPRINT,
        "embedding_model": RURI_MODEL_ID,
        "embedding_revision": RURI_REVISION,
        "embedding_dim": EMBED_DIM,
        "embedding_prefix_doc": PREFIX_DOC,
        "embedding_prefix_query": PREFIX_QUERY,
        "chunk_strategy": CHUNK_STRATEGY,
        "sparse_index": SPARSE_INDEX,
        "extra": {
            "mysql_schema": extra.get("mysql_schema"),
            "mysql_table": extra.get("mysql_table"),
            "mysql_pk": extra.get("mysql_pk"),
            "sheet_name": extra.get("sheet_name"),
            "original_filename": object_row["object_key"],
        },
    }




def _item_counts(conn: sqlite3.Connection, job_id: str) -> tuple[int, int]:
    """(成功数, 処理済み数)。進捗率は処理済み（失敗も含む）で数え、item 失敗で巻き戻らないようにする。"""
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status IN ('completed','skipped') THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN status IN ('completed','skipped','failed') THEN 1 ELSE 0 END) AS processed
        FROM job_items WHERE job_id=?
        """,
        (job_id,),
    ).fetchone()
    return int(row["done"] or 0), int(row["processed"] or 0)


def _touch_item_progress(
    conn: sqlite3.Connection,
    job_id: str,
    object_key: str,
    doc_i: int,
    n_docs: int,
) -> None:
    job = row_to_dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    if not job:
        return
    _, processed = _item_counts(conn, job_id)
    total = max(job["work_units_total"] or 1, 1)
    pct = min(99.0, 100.0 * (float(processed) + (doc_i / max(n_docs, 1))) / total)
    conn.execute(
        "UPDATE jobs SET progress_pct=?, current_item_name=? WHERE job_id=?",
        (pct, f"{object_key} ({doc_i}/{n_docs})", job_id),
    )
    conn.commit()


def _update_progress(conn: sqlite3.Connection, job_id: str) -> None:
    job = row_to_dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    if not job:
        return
    done, processed = _item_counts(conn, job_id)
    total = max(job["work_units_total"] or 1, 1)
    pct = min(99.0, 100.0 * processed / total)
    eta = None
    if job.get("started_at") and processed >= 1:
        from datetime import datetime

        started = datetime.fromisoformat(job["started_at"])
        elapsed = max((datetime.fromisoformat(utcnow()) - started).total_seconds(), 1)
        per = elapsed / processed
        eta = int(per * (total - processed))
    conn.execute(
        """
        UPDATE jobs SET progress_pct=?, work_units_done=?, eta_seconds=? WHERE job_id=?
        """,
        (pct, float(done), eta, job_id),
    )
    conn.commit()


def _discover_files(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        path = path.resolve()
        if path.is_file():
            found.append(path)
            continue
        for child in path.rglob("*"):
            if any(part in EXCLUDED_DIRS for part in child.parts):
                continue
            if child.name.startswith(".") and child.name != ".gitkeep":
                continue
            if child.is_dir():
                continue
            if child.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            found.append(child)
            if len(found) >= MAX_FILES_PER_JOB:
                return found
    return found


def _posix_key(path: Path) -> str:
    return path.as_posix()


def _resolve_file(source: dict[str, Any], object_key: str) -> Path:
    direct = Path(object_key)
    if direct.exists():
        return direct
    config = json.loads(source["config_json"])
    for raw in config.get("paths", []):
        candidate = Path(raw)
        if candidate.as_posix() == object_key:
            return candidate
        if candidate.is_dir():
            maybe = Path(object_key)
            if maybe.exists():
                return maybe
    raise FileNotFoundError(object_key)


def estimate_paths(paths: list[Path]) -> dict[str, Any]:
    files = _discover_files(paths)
    total = sum(p.stat().st_size for p in files if p.exists())
    est = estimate_from_bytes(total, kind="file")
    est["file_count"] = len(files)
    return est
