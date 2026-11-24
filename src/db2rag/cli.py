"""db2rag CLI。"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import click
import uvicorn

from db2rag import __version__
from db2rag.auth import require_api_key_for_bind, resolve_bind_host
from db2rag.config import get_settings
from db2rag.db import DbManager, connect, migrate, schema_version
from db2rag.embedding import Embedder
from db2rag.eval import load_dataset, run_evaluation
from db2rag.pipeline import create_job_from_files
from db2rag.qdrant_store import QdrantStore
from db2rag.testdata import generate_all
from db2rag.worker import run_worker_loop


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """DB2RAG: MySQL / 文書 → ローカル永続 RAG。"""


@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8781, type=int)
def serve(host: str, port: int) -> None:
    """API と UI を起動する。ブラウザを閉じてもサーバは残る。Ctrl+C で停止。"""
    settings = get_settings()
    bind = resolve_bind_host(host, allow_bind_all=settings.allow_bind_all)
    require_api_key_for_bind(bind, settings.api_key)
    os.environ["DB2RAG_HOST"] = bind
    os.environ["DB2RAG_API_PORT"] = str(port)
    uvicorn.run("db2rag.api:app", host=bind, port=port, reload=False, factory=False)


@cli.command()
def worker() -> None:
    """取り込み worker だけを起動する（serve 内でも動く）。"""
    settings = get_settings()
    db = DbManager(settings.sqlite_path())
    store = QdrantStore(url=settings.qdrant_url, path=settings.qdrant_path)
    embedder = Embedder(backend=settings.embedding_backend, models_dir=settings.models_dir())
    stop = threading.Event()
    try:
        run_worker_loop(db, store, embedder, settings, stop)
    except KeyboardInterrupt:
        stop.set()


@cli.command()
def testdata() -> None:
    """SQL / PDF / Excel / CSV などの試験データを生成する。"""
    written = generate_all()
    for kind, path in written.items():
        click.echo(f"{kind}: {path}")


@cli.command("ingest-files")
@click.option("--name", default="testdata RAG")
@click.option("--acl", default="internal")
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
def ingest_files(name: str, acl: str, paths: tuple[Path, ...]) -> None:
    settings = get_settings()
    conn = connect(settings.sqlite_path())
    migrate(conn)
    result = create_job_from_files(
        conn,
        rag=None,
        display_name=name,
        acl_tag=acl,
        paths=list(paths),
        mode="create",
    )
    click.echo(json_dumps(result))


@cli.command("eval")
@click.option("--rag-id", required=True)
@click.option("--dataset", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--top-k", default=20, type=int)
@click.option("--threshold", default=0.0, type=float)
def eval_cmd(rag_id: str, dataset: Path, top_k: int, threshold: float) -> None:
    """質問セットで Recall / MRR / nDCG を測り、gate を保存する。"""
    settings = get_settings()
    conn = connect(settings.sqlite_path())
    migrate(conn)
    store = QdrantStore(url=settings.qdrant_url, path=settings.qdrant_path)
    embedder = Embedder(backend=settings.embedding_backend, models_dir=settings.models_dir())
    report = run_evaluation(
        conn,
        store,
        embedder,
        rag_id=rag_id,
        dataset=load_dataset(dataset),
        top_k=top_k,
        score_threshold=threshold,
    )
    click.echo(json_dumps(report))
    if not report["gate_passed"]:
        raise SystemExit(1)


@cli.command("schema-version")
def schema_version_cmd() -> None:
    settings = get_settings()
    conn = connect(settings.sqlite_path())
    migrate(conn)
    click.echo(str(schema_version(conn)))


def json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)
