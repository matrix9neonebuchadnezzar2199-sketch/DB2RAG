"""取り込みパイプライン。discover → activate。"""

from __future__ import annotations

import getpass
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

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
from db2rag.db import row_to_dict, rows_to_dicts, table_columns, utcnow
from db2rag.embedding import Embedder
from db2rag.ids import new_id
from db2rag.mysql_reader import (
    MysqlError,
    iter_custom_sql_rows,
    iter_table_rows,
    list_objects,
    record_id_from,
    render_record_with_template,
    row_hash,
)
from db2rag.mysql_reader import (
    connect as mysql_connect,
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
from db2rag.secrets import decrypt_secret, encrypt_secret, load_or_create_key
from db2rag.textnorm import normalize_text, sha256_file, sha256_text

MYSQL_BATCH_SIZE = 200
_ISO_UPDATED_AT = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)
_OBJECT_CONFIG_KEYS = ("template", "custom_sql", "pk_columns")

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


def mysql_point_ids_to_delete(
    existing_hash: str | None,
    new_hash: str,
    old_point_ids: list[str],
) -> list[str] | None:
    """未変更行はスキップし、変わった行は消す point id を返す。

    Args:
        existing_hash: 最新版の row_hash。版が無いときは None。
        new_hash: 取り込み行の row_hash。
        old_point_ids: 最新版に属する Qdrant point。新規行は空で渡す。

    Returns:
        ハッシュが同じなら None（埋め込みしない）。
        新規または更新なら削除する point id。新規は空リスト。
    """
    if existing_hash is not None and existing_hash == new_hash:
        return None
    return list(old_point_ids)


def iter_row_batches(
    rows: Iterator[dict[str, Any]],
    batch_size: int = MYSQL_BATCH_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """行ストリームを固定件数ずつ渡す。全件リストは持たない。

    Args:
        rows: 1 行ずつ進むイテレータ。
        batch_size: 同時にメモリへ載せる最大行数。

    Returns:
        行リストのイテレータ。末尾だけ短い。

    Raises:
        ValueError: batch_size が 1 未満のとき。
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


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
        object_id = _reuse_object(
            conn,
            rag_id=rag_id,
            source_id=source_id,
            source_type="file",
            object_type="file",
            object_key=_posix_key(path),
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
    secrets_key_path: Path | None = None,
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
    stored_config = _seal_mysql_config(conn, mysql_config, secrets_key_path, now)
    conn.execute(
        """
        INSERT INTO sources(source_id, rag_id, source_type, source_name, config_json,
                             acl_tag_default, enabled, created_at)
        VALUES (?, ?, 'mysql', ?, ?, ?, 1, ?)
        """,
        (source_id, rag_id, mysql_config.get("host", "mysql"), json.dumps(stored_config), acl_tag, now),
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
        object_id = _reuse_object(
            conn,
            rag_id=rag_id,
            source_id=source_id,
            source_type="mysql",
            object_type=str(meta["object_type"]),
            object_key=key,
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
    secrets_key_path: Path | None = None,
) -> None:
    rag = row_to_dict(conn.execute("SELECT * FROM rags WHERE rag_id=?", (job["rag_id"],)).fetchone())
    if rag is None or rag.get("status") == "purged":
        return
    gc_archived_collections(conn, store)
    mode = str(job.get("mode") or "create")
    active = rag.get("active_collection")
    # append は既存 collection へ差分 upsert する。空の staging へ切り替えると既存点が消える。
    switch_alias = not (mode == "append" and active)
    if mode == "append" and active:
        collection = str(active)
    else:
        staging_name = rag.get("staging_collection")
        collection = str(staging_name) if staging_name else ""
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
        _process_item(
            conn,
            store,
            embedder,
            rag,
            job,
            item,
            collection,
            mysql_password=mysql_password,
            secrets_key_path=secrets_key_path,
            reindex=mode == "reindex",
        )
        _update_progress(conn, job["job_id"])
    current = row_to_dict(conn.execute("SELECT status FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone())
    if current and current["status"] in {"cancelled"}:
        return
    rag_now = row_to_dict(conn.execute("SELECT status FROM rags WHERE rag_id=?", (rag["rag_id"],)).fetchone())
    if rag_now is None or rag_now.get("status") == "purged":
        return
    if mode == "reindex":
        _reembed_rag_children(conn, store, embedder, rag, collection)
    _verify_and_activate(conn, store, rag, job, collection, switch_alias=switch_alias)


def _process_item(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    rag: dict[str, Any],
    job: dict[str, Any],
    item: dict[str, Any],
    collection: str,
    mysql_password: str | None,
    secrets_key_path: Path | None = None,
    reindex: bool = False,
) -> None:
    object_row = row_to_dict(
        conn.execute("SELECT * FROM source_objects WHERE object_id=?", (item["object_id"],)).fetchone()
    )
    if object_row is None:
        raise ValueError("ERR_OBJECT_MISSING")
    source = row_to_dict(
        conn.execute("SELECT * FROM sources WHERE source_id=?", (object_row["source_id"],)).fetchone()
    )
    if source is None:
        raise ValueError("ERR_SOURCE_MISSING")
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
        if source["source_type"] == "mysql":
            # 表全体を list にしない。200 行ずつ描画して埋め込む。
            created_docs = _process_mysql_item(
                conn,
                store,
                embedder,
                rag,
                job,
                source,
                object_row,
                collection,
                mysql_password=mysql_password,
                secrets_key_path=secrets_key_path,
            )
            item_status = "completed" if created_docs or reindex else "skipped"
            conn.execute(
                "UPDATE job_items SET status=?, checkpoint='verified', error_code=NULL, error_message=NULL WHERE job_item_id=?",
                (item_status, item["job_item_id"]),
            )
            conn.commit()
            return
        documents = _extract_documents(
            conn, source, object_row, mysql_password, secrets_key_path=secrets_key_path
        )
        all_parents = 0
        all_children = 0
        created_docs = 0
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
                if reindex:
                    _reupsert_document(conn, store, embedder, rag, source, object_row, existing["doc_version_id"], collection)
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
            if source["source_type"] == "file":
                _retire_other_versions(conn, store, collection, object_row["object_id"], doc_id)
            conn.execute(
                "UPDATE source_objects SET latest_doc_version_id=?, last_ingested_at=? WHERE object_id=?",
                (doc_id, now, object_row["object_id"]),
            )
            all_parents += len(chunks.parents)
            all_children += len(chunks.children)
            created_docs += 1
            _touch_item_progress(conn, job["job_id"], object_row["object_key"], doc_i, n_docs)
        item_status = "completed" if created_docs or reindex else "skipped"
        conn.execute(
            "UPDATE job_items SET status=?, checkpoint='verified', error_code=NULL, error_message=NULL WHERE job_item_id=?",
            (item_status, item["job_item_id"]),
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


@dataclass
class _MysqlPending:
    """1 バッチの中で、埋め込みが必要な 1 レコード。"""

    document: CanonicalDocument
    meta: dict[str, Any]
    chunks: ChunkResult
    content_hash: str
    old_doc_id: str | None
    record_id: str
    row_hash_value: str
    doomed_points: list[str]


def _process_mysql_item(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    rag: dict[str, Any],
    job: dict[str, Any],
    source: dict[str, Any],
    object_row: dict[str, Any],
    collection: str,
    *,
    mysql_password: str | None,
    secrets_key_path: Path | None,
) -> int:
    """MySQL オブジェクトをバッチで取り込む。戻り値は新規または置換したレコード数。"""
    config = _merged_mysql_options(source, object_row)
    template = _resolve_template(config.get("template"), str(object_row["object_key"]))
    custom = _resolve_custom_sql(config.get("custom_sql"), str(object_row["object_key"]))
    password = mysql_password or _reveal_mysql_password(conn, config, secrets_key_path)
    mysql_conn = mysql_connect(
        host=config["host"],
        port=int(config["port"]),
        user=config["user"],
        password=password,
        database=config["database"],
        ssl_mode=config.get("ssl_mode", "DISABLED"),
    )
    created = 0
    running_cursor: str | None = None
    row_iter: Iterator[dict[str, Any]] | None = None
    try:
        pk = _mysql_pk(mysql_conn, config, object_row, custom)
        row_iter = _open_mysql_rows(mysql_conn, object_row, template, custom)
        seen = 0
        for batch in iter_row_batches(row_iter, MYSQL_BATCH_SIZE):
            made, batch_cursor = _ingest_mysql_batch(
                conn,
                store,
                embedder,
                rag,
                source,
                object_row,
                collection,
                batch,
                pk,
                template,
            )
            created += made
            running_cursor = _later_cursor(running_cursor, batch_cursor)
            if batch_cursor:
                _write_sync_cursor(conn, str(object_row["object_id"]), running_cursor)
            conn.commit()
            seen += len(batch)
            _touch_item_progress(
                conn,
                str(job["job_id"]),
                str(object_row["object_key"]),
                seen,
                max(seen, 1),
            )
    finally:
        if row_iter is not None:
            closer = getattr(row_iter, "close", None)
            if callable(closer):
                closer()
        mysql_conn.close()
    return created


def _ingest_mysql_batch(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    rag: dict[str, Any],
    source: dict[str, Any],
    object_row: dict[str, Any],
    collection: str,
    batch: list[dict[str, Any]],
    pk: list[str],
    template: dict[str, Any] | None,
) -> tuple[int, str | None]:
    """1 バッチを判定し、変わった行だけ chunk / embed / upsert する。"""
    schema, table = str(object_row["object_key"]).split(".", 1)
    doc_columns = table_columns(conn, "document_versions")
    pending: list[_MysqlPending] = []
    cursor_max: str | None = None
    for row in batch:
        record_id = record_id_from(pk, row)
        digest = row_hash(row)
        updated = _row_updated_at(row, template)
        cursor_max = _later_cursor(cursor_max, updated)
        existing = _latest_record(conn, str(object_row["object_id"]), record_id, doc_columns)
        stored_hash, prefetch = _existing_points(conn, existing, digest)
        doomed = mysql_point_ids_to_delete(stored_hash, digest, prefetch)
        if doomed is None:
            continue
        text, title, rendered_updated, acl = render_record_with_template(
            str(object_row["object_key"]),
            record_id,
            row,
            template,
        )
        if rendered_updated:
            updated = rendered_updated
            cursor_max = _later_cursor(cursor_max, updated)
        document = CanonicalDocument(
            title=title,
            language="ja",
            mime="text/plain",
            blocks=[Block(type="paragraph", text=text)],
            extra={"kind": "mysql", "source_type": "mysql"},
        )
        plain = normalize_text(document.plain_text())
        chunks = chunk_document(document, kind="mysql")
        if not chunks.children:
            continue
        meta: dict[str, Any] = {
            "row_hash": digest,
            "record_id": record_id,
            "mysql_schema": schema,
            "mysql_table": table,
            "mysql_pk": {col: row.get(col) for col in pk},
            "updated_at": _payload_updated_at(updated),
            "mime": "text/plain",
        }
        if acl:
            meta["acl_tag"] = acl
        pending.append(
            _MysqlPending(
                document=document,
                meta=meta,
                chunks=chunks,
                content_hash=sha256_text(plain),
                old_doc_id=None if existing is None else str(existing["doc_version_id"]),
                record_id=record_id,
                row_hash_value=digest,
                doomed_points=doomed,
            )
        )
    if not pending:
        return 0, cursor_max
    # バッチ内の child を 1 回で埋める。次のバッチを読む前に終わらせる。
    texts = [child.text for item in pending for child in item.chunks.children]
    vectors = embedder.encode_documents(texts)
    offset = 0
    now = utcnow()
    last_doc_id: str | None = None
    points = []
    for item in pending:
        width = len(item.chunks.children)
        doc_id = _insert_mysql_version(conn, object_row, item, doc_columns, now)
        points.extend(
            _mysql_points(
                rag,
                source,
                object_row,
                item,
                vectors[offset : offset + width],
            )
        )
        offset += width
        last_doc_id = doc_id
    store.upsert_points(collection, points)
    for item in pending:
        if item.old_doc_id is None:
            continue
        # ファイルと違い、同じ object の他レコードは消さない。この record の旧版だけ。
        store.delete_points(collection, item.doomed_points)
        _drop_mysql_version_rows(conn, item.old_doc_id, doc_columns)
    if last_doc_id is not None:
        conn.execute(
            "UPDATE source_objects SET latest_doc_version_id=?, last_ingested_at=? WHERE object_id=?",
            (last_doc_id, now, object_row["object_id"]),
        )
    return len(pending), cursor_max


def _existing_points(
    conn: sqlite3.Connection,
    existing: sqlite3.Row | None,
    new_hash: str,
) -> tuple[str | None, list[str]]:
    if existing is None:
        return None, []
    stored = None if existing["row_hash"] is None else str(existing["row_hash"])
    if stored is not None and stored == new_hash:
        return stored, []
    return stored, _point_ids_for_doc(conn, str(existing["doc_version_id"]))


def _latest_record(
    conn: sqlite3.Connection,
    object_id: str,
    record_id: str,
    columns: set[str],
) -> sqlite3.Row | None:
    if "record_id" not in columns or "is_latest" not in columns:
        return None
    found = conn.execute(
        """
        SELECT doc_version_id, row_hash
        FROM document_versions
        WHERE object_id=? AND record_id=? AND is_latest=1
        ORDER BY created_at DESC, doc_version_id DESC
        LIMIT 1
        """,
        (object_id, record_id),
    ).fetchone()
    return cast(sqlite3.Row | None, found)


def _point_ids_for_doc(conn: sqlite3.Connection, doc_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT c.qdrant_point_id
        FROM child_chunks c
        JOIN parent_chunks p ON c.parent_chunk_id = p.parent_chunk_id
        WHERE p.doc_version_id=?
        """,
        (doc_id,),
    ).fetchall()
    return [str(row["qdrant_point_id"]) for row in rows if row["qdrant_point_id"]]


def _insert_mysql_version(
    conn: sqlite3.Connection,
    object_row: dict[str, Any],
    item: _MysqlPending,
    columns: set[str],
    now: str,
) -> str:
    doc_id = new_id("docv")
    fields = [
        "doc_version_id",
        "object_id",
        "content_hash",
        "file_hash",
        "row_hash",
        "extracted_bytes",
        "parent_count",
        "child_count",
        "pipeline_fingerprint",
        "created_at",
    ]
    values: list[Any] = [
        doc_id,
        object_row["object_id"],
        item.content_hash,
        None,
        item.row_hash_value,
        len(normalize_text(item.document.plain_text()).encode("utf-8")),
        len(item.chunks.parents),
        len(item.chunks.children),
        PIPELINE_FINGERPRINT,
        now,
    ]
    if "record_id" in columns:
        fields.append("record_id")
        values.append(item.record_id)
    if "is_latest" in columns:
        fields.append("is_latest")
        values.append(1)
    holders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO document_versions({', '.join(fields)}) VALUES ({holders})",
        values,
    )
    for parent in item.chunks.parents:
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
    for child in item.chunks.children:
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
    return doc_id


def _mysql_points(
    rag: dict[str, Any],
    source: dict[str, Any],
    object_row: dict[str, Any],
    item: _MysqlPending,
    vectors: Any,
) -> list[Any]:
    points = []
    for child, vector in zip(item.chunks.children, vectors, strict=True):
        parent = next(p for p in item.chunks.parents if p.parent_chunk_id == child.parent_chunk_id)
        payload = _payload(
            rag=rag,
            source=source,
            object_row=object_row,
            document=item.document,
            parent=parent,
            child=child,
            content_hash=sha256_text(child.text),
            doc_content_hash=item.content_hash,
            extra=item.meta,
        )
        points.append(
            make_point(
                point_id=child.qdrant_point_id,
                dense=vector.tolist(),
                text=child.text,
                payload=payload,
            )
        )
    return points


def _drop_mysql_version_rows(conn: sqlite3.Connection, doc_id: str, columns: set[str]) -> None:
    """旧レコードの child / parent を消し、版行は is_latest=0 で残す。"""
    conn.execute(
        """
        DELETE FROM child_chunks WHERE parent_chunk_id IN (
            SELECT parent_chunk_id FROM parent_chunks WHERE doc_version_id=?
        )
        """,
        (doc_id,),
    )
    conn.execute("DELETE FROM parent_chunks WHERE doc_version_id=?", (doc_id,))
    if "is_latest" in columns:
        conn.execute(
            "UPDATE document_versions SET is_latest=0 WHERE doc_version_id=?",
            (doc_id,),
        )


def _open_mysql_rows(
    mysql_conn: Any,
    object_row: dict[str, Any],
    template: dict[str, Any] | None,
    custom: tuple[str, list[str] | None] | None,
) -> Iterator[dict[str, Any]]:
    if custom is not None:
        return iter_custom_sql_rows(mysql_conn, custom[0])
    schema, table = str(object_row["object_key"]).split(".", 1)
    where_col: str | None = None
    since_value: str | None = None
    updated_column = template.get("updated_at_column") if template else None
    cursor_value = object_row.get("sync_cursor")
    # テンプレートが増分列を指定し、カーソルがあるときだけ絞る。無いときは全件。
    if updated_column and cursor_value:
        where_col = str(updated_column)
        since_value = str(cursor_value)
    lookups = _lookups_from_template(template)
    return iter_table_rows(
        mysql_conn,
        schema,
        table,
        where_col=where_col,
        since_value=since_value,
        lookups=lookups or None,
    )


def _mysql_pk(
    mysql_conn: Any,
    config: dict[str, Any],
    object_row: dict[str, Any],
    custom: tuple[str, list[str] | None] | None,
) -> list[str]:
    if custom is not None and custom[1]:
        return custom[1]
    override = _pk_override(config.get("pk_columns"), str(object_row["object_key"]))
    if override:
        return override
    pk: list[str] = []
    for obj in list_objects(mysql_conn, config["database"]):
        if obj["object_key"] == object_row["object_key"]:
            pk = [str(col) for col in (obj.get("pk_columns") or [])]
            break
    if not pk:
        raise MysqlError("ERR_MYSQL_NO_PK")
    return pk


def _lookups_from_template(template: dict[str, Any] | None) -> list[dict[str, str]]:
    if not template:
        return []
    found: list[dict[str, str]] = []
    for section in template.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for field in section.get("fields") or []:
            if not isinstance(field, dict):
                continue
            column = field.get("column")
            lookup_table = field.get("lookup_table")
            lookup_key = field.get("lookup_key")
            lookup_label = field.get("lookup_label")
            if not (column and lookup_table and lookup_key and lookup_label):
                continue
            found.append(
                {
                    "column": str(column),
                    "lookup_table": str(lookup_table),
                    "lookup_key": str(lookup_key),
                    "lookup_label": str(lookup_label),
                }
            )
    return found


def _row_updated_at(row: dict[str, Any], template: dict[str, Any] | None) -> str | None:
    column: str | None = None
    if template and template.get("updated_at_column"):
        column = str(template["updated_at_column"])
    elif template is None and "updated_at" in row:
        column = "updated_at"
    if column is None:
        return None
    value = row.get(column)
    if value is None:
        return None
    text = normalize_text(str(value))
    return text or None


def _payload_updated_at(value: str | None) -> str | None:
    # Qdrant の日時索引は ISO だけ受ける。カーソル文字列とは別に落とす。
    if value and _ISO_UPDATED_AT.match(value):
        return value
    return None


def _later_cursor(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def _write_sync_cursor(conn: sqlite3.Connection, object_id: str, value: str | None) -> None:
    if not value or "sync_cursor" not in table_columns(conn, "source_objects"):
        return
    conn.execute(
        """
        UPDATE source_objects
        SET sync_cursor=?
        WHERE object_id=? AND (sync_cursor IS NULL OR sync_cursor < ?)
        """,
        (value, object_id, value),
    )


def _merged_mysql_options(source: dict[str, Any], object_row: dict[str, Any]) -> dict[str, Any]:
    merged = _load_json_object(source.get("config_json"))
    overlay = _load_json_object(object_row.get("config_json"))
    for key in _OBJECT_CONFIG_KEYS:
        if key in overlay:
            merged[key] = overlay[key]
    return merged


def _load_json_object(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    loaded = json.loads(str(raw))
    if not isinstance(loaded, dict):
        raise ValueError("config_json must be an object")
    return loaded


def _resolve_template(value: Any, object_key: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if any(key in value for key in ("sections", "title_column", "updated_at_column", "acl_column")):
        return value
    nested = value.get(object_key)
    if isinstance(nested, dict):
        return nested
    return None


def _resolve_custom_sql(value: Any, object_key: str) -> tuple[str, list[str] | None] | None:
    if isinstance(value, str):
        text = value.strip()
        return (text, None) if text else None
    if isinstance(value, dict):
        if isinstance(value.get("sql"), str):
            return str(value["sql"]), _pk_list(value.get("pk_columns"))
        nested = value.get(object_key)
        if nested is not None:
            return _resolve_custom_sql(nested, object_key)
        return None
    if isinstance(value, list):
        chosen: tuple[str, list[str] | None] | None = None
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("sql"), str):
                continue
            key = item.get("object_key")
            if key is not None and key != object_key:
                continue
            if key is None and len(value) != 1:
                continue
            chosen = (str(item["sql"]), _pk_list(item.get("pk_columns")))
            if key == object_key:
                return chosen
        return chosen
    return None


def _pk_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    return [str(item) for item in value]


def _pk_override(value: Any, object_key: str) -> list[str] | None:
    if isinstance(value, dict):
        return _pk_list(value.get(object_key))
    return _pk_list(value)


def _extract_documents(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    object_row: dict[str, Any],
    mysql_password: str | None,
    *,
    secrets_key_path: Path | None = None,
) -> list[tuple[CanonicalDocument, dict[str, Any]]]:
    # MySQL は _process_mysql_item がストリームする。ここはファイルだけ。
    del conn, mysql_password, secrets_key_path
    if source["source_type"] != "file":
        raise ValueError(f"ERR_UNSUPPORTED_SOURCE:{source['source_type']}")
    path = _resolve_file(source, object_row["object_key"])
    if path.is_symlink():
        raise ValueError("ERR_FILE_SYMLINK")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("ERR_FILE_TOO_LARGE")
    document = parse_file(path)
    return [(document, {"file_hash": sha256_file(str(path)), "path": str(path), "mime": mime_for(path)})]


def _verify_and_activate(
    conn: sqlite3.Connection,
    store: QdrantStore,
    rag: dict[str, Any],
    job: dict[str, Any],
    collection: str,
    *,
    switch_alias: bool = True,
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
    if failed == 0 and int(sqlite_children) != int(qcount):
        conn.execute(
            "UPDATE jobs SET status='failed', error_code=?, error_message=?, finished_at=?, progress_pct=100 WHERE job_id=?",
            (
                "ERR_VERIFY_COUNT_MISMATCH",
                f"sqlite={sqlite_children}, qdrant={qcount}",
                now,
                job["job_id"],
            ),
        )
        conn.commit()
        return
    if failed and qcount == 0:
        conn.execute(
            "UPDATE jobs SET status='failed', error_code=?, error_message=?, finished_at=?, progress_pct=100 WHERE job_id=?",
            ("ERR_JOB_ITEMS_FAILED", f"{failed} items failed", now, job["job_id"]),
        )
        conn.commit()
        return
    if switch_alias:
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
            delete_after = (datetime.now(UTC) + timedelta(days=7)).replace(microsecond=0).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO archived_collections(collection_name, rag_id, archived_at, delete_after) VALUES (?, ?, ?, ?)",
                (old, rag["rag_id"], now, delete_after),
            )
    else:
        conn.execute(
            "UPDATE rags SET status='active', updated_at=? WHERE rag_id=?",
            (now, rag["rag_id"]),
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
        "acl_tag": extra.get("acl_tag") or source["acl_tag_default"],
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


def _reuse_object(
    conn: sqlite3.Connection,
    *,
    rag_id: str,
    source_id: str,
    source_type: str,
    object_type: str,
    object_key: str,
) -> str:
    """同じ RAG・種別・キーの object を再利用し、source だけ新しい設定へ付け替える。"""
    row = conn.execute(
        """
        SELECT o.object_id
        FROM source_objects o
        JOIN sources s ON s.source_id = o.source_id
        WHERE s.rag_id=? AND s.source_type=? AND o.object_key=?
        LIMIT 1
        """,
        (rag_id, source_type, object_key),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE source_objects SET source_id=?, object_type=? WHERE object_id=?",
            (source_id, object_type, row["object_id"]),
        )
        return str(row["object_id"])
    object_id = new_id("obj")
    conn.execute(
        """
        INSERT INTO source_objects(object_id, source_id, object_type, object_key)
        VALUES (?, ?, ?, ?)
        """,
        (object_id, source_id, object_type, object_key),
    )
    return object_id


def _seal_mysql_config(
    conn: sqlite3.Connection,
    mysql_config: dict[str, Any],
    secrets_key_path: Path | None,
    now: str,
) -> dict[str, Any]:
    """パスワードを secrets に退避し、config_json へは secret_id だけ残す。"""
    stored = dict(mysql_config)
    password = str(stored.pop("password", "") or "")
    if not password:
        return stored
    if secrets_key_path is None:
        raise ValueError("secrets_key_path required to store mysql password")
    secret_id = new_id("sec")
    fernet = load_or_create_key(secrets_key_path)
    conn.execute(
        "INSERT INTO secrets(secret_id, kind, ciphertext, created_at) VALUES (?, 'mysql_password', ?, ?)",
        (secret_id, encrypt_secret(fernet, password), now),
    )
    stored["secret_id"] = secret_id
    return stored


def _reveal_mysql_password(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    secrets_key_path: Path | None,
) -> str:
    secret_id = config.get("secret_id")
    if not secret_id:
        return str(config.get("password") or "")
    if secrets_key_path is None:
        raise ValueError("ERR_SECRET_KEY_MISSING")
    row = conn.execute("SELECT ciphertext FROM secrets WHERE secret_id=?", (secret_id,)).fetchone()
    if row is None:
        raise ValueError("ERR_SECRET_NOT_FOUND")
    return decrypt_secret(load_or_create_key(secrets_key_path), bytes(row["ciphertext"]))


def _retire_other_versions(
    conn: sqlite3.Connection,
    store: QdrantStore,
    collection: str,
    object_id: str,
    keep_doc_id: str,
) -> None:
    """ファイルは 1 object = 1 文書。置換後の旧版ポイントと行を消す。"""
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT c.qdrant_point_id, c.child_chunk_id, p.parent_chunk_id, d.doc_version_id
            FROM document_versions d
            JOIN parent_chunks p ON p.doc_version_id = d.doc_version_id
            JOIN child_chunks c ON c.parent_chunk_id = p.parent_chunk_id
            WHERE d.object_id=? AND d.doc_version_id!=?
            """,
            (object_id, keep_doc_id),
        ).fetchall()
    )
    point_ids = [str(row["qdrant_point_id"]) for row in rows if row.get("qdrant_point_id")]
    store.delete_points(collection, point_ids)
    old_docs = {
        str(row["doc_version_id"])
        for row in rows_to_dicts(
            conn.execute(
                "SELECT doc_version_id FROM document_versions WHERE object_id=? AND doc_version_id!=?",
                (object_id, keep_doc_id),
            ).fetchall()
        )
    }
    for doc_id in old_docs:
        conn.execute(
            """
            DELETE FROM child_chunks WHERE parent_chunk_id IN (
                SELECT parent_chunk_id FROM parent_chunks WHERE doc_version_id=?
            )
            """,
            (doc_id,),
        )
        conn.execute("DELETE FROM parent_chunks WHERE doc_version_id=?", (doc_id,))
        conn.execute("DELETE FROM document_versions WHERE doc_version_id=?", (doc_id,))


def _reupsert_document(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    rag: dict[str, Any],
    source: dict[str, Any],
    object_row: dict[str, Any],
    doc_version_id: str,
    collection: str,
) -> None:
    """内容不変の文書を、新しい collection へ同じ point id で入れ直す。"""
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT c.child_chunk_id, c.text, c.qdrant_point_id, c.child_index,
                   p.parent_chunk_id, p.section_path, p.parent_index, p.page_from, p.page_to,
                   d.content_hash
            FROM child_chunks c
            JOIN parent_chunks p ON c.parent_chunk_id = p.parent_chunk_id
            JOIN document_versions d ON p.doc_version_id = d.doc_version_id
            WHERE d.doc_version_id=?
            ORDER BY c.child_index
            """,
            (doc_version_id,),
        ).fetchall()
    )
    if not rows:
        return
    vectors = embedder.encode_documents([str(row["text"]) for row in rows])
    points = []
    for row, vector in zip(rows, vectors, strict=True):
        points.append(
            make_point(
                point_id=str(row["qdrant_point_id"]),
                dense=vector.tolist(),
                text=str(row["text"]),
                payload=_stored_payload(rag, source, object_row, row),
            )
        )
    store.upsert_points(collection, points)


def _reembed_rag_children(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    rag: dict[str, Any],
    collection: str,
) -> None:
    """reindex 先の空 collection に、RAG の現行 child をすべて入れ直す。"""
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT c.child_chunk_id, c.text, c.qdrant_point_id, c.child_index,
                   p.parent_chunk_id, p.section_path, p.parent_index, p.page_from, p.page_to,
                   d.content_hash, o.object_id, o.object_key, o.object_type,
                   s.source_type, s.source_name, s.acl_tag_default
            FROM child_chunks c
            JOIN parent_chunks p ON c.parent_chunk_id = p.parent_chunk_id
            JOIN document_versions d ON p.doc_version_id = d.doc_version_id
            JOIN source_objects o ON d.object_id = o.object_id
            JOIN sources s ON o.source_id = s.source_id
            WHERE s.rag_id=?
            ORDER BY c.child_index
            """,
            (rag["rag_id"],),
        ).fetchall()
    )
    if not rows:
        return
    vectors = embedder.encode_documents([str(row["text"]) for row in rows])
    points = []
    for row, vector in zip(rows, vectors, strict=True):
        object_row = {"object_id": row["object_id"], "object_key": row["object_key"]}
        source = {
            "source_type": row["source_type"],
            "source_name": row["source_name"],
            "acl_tag_default": row["acl_tag_default"],
        }
        points.append(
            make_point(
                point_id=str(row["qdrant_point_id"]),
                dense=vector.tolist(),
                text=str(row["text"]),
                payload=_stored_payload(rag, source, object_row, row),
            )
        )
    store.upsert_points(collection, points)


def _stored_payload(
    rag: dict[str, Any],
    source: dict[str, Any],
    object_row: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any]:
    now = utcnow()
    return {
        "schema_version": SCHEMA_VERSION,
        "rag_id": rag["rag_id"],
        "source_type": source["source_type"],
        "source_name": source.get("source_name"),
        "object_id": object_row["object_id"],
        "record_id": object_row.get("object_key"),
        "title": object_row.get("object_key"),
        "section_path": row.get("section_path"),
        "page_from": row.get("page_from"),
        "page_to": row.get("page_to"),
        "parent_chunk_id": row["parent_chunk_id"],
        "child_chunk_id": row["child_chunk_id"],
        "parent_index": row.get("parent_index"),
        "child_index": row.get("child_index"),
        "created_at": now,
        "content_hash": row.get("content_hash"),
        "doc_content_hash": row.get("content_hash"),
        "acl_tag": source.get("acl_tag_default"),
        "language": "ja",
    }


def gc_archived_collections(conn: sqlite3.Connection, store: QdrantStore) -> None:
    """delete_after を過ぎた旧 collection を消す。"""
    now = utcnow()
    rows = conn.execute(
        "SELECT collection_name FROM archived_collections WHERE delete_after <= ?",
        (now,),
    ).fetchall()
    for row in rows:
        name = str(row["collection_name"])
        store.delete_collection(name)
        conn.execute("DELETE FROM archived_collections WHERE collection_name=?", (name,))
    if rows:
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
