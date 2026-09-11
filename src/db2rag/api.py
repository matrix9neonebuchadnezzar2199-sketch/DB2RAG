"""FastAPI: RAG / Job / Search / Export + 承認済み5画面。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db2rag.config import Settings, get_settings, repo_root
from db2rag.db import connect, migrate, row_to_dict, rows_to_dicts, schema_version, utcnow
from db2rag.embedding import Embedder
from db2rag.export import export_rag
from db2rag.mysql_reader import connect as mysql_connect
from db2rag.mysql_reader import list_objects, test_connection
from db2rag.pipeline import (
    create_job_from_files,
    create_job_from_mysql,
    estimate_paths,
    list_rags,
)
from db2rag.purge import purge_rag
from db2rag.qdrant_store import QdrantStore
from db2rag.search import search as run_search
from db2rag.worker import run_worker_loop

WEB_DIR = repo_root() / "web"
DOCS_DIR = repo_root() / "docs"




class AppState:
    settings: Settings
    conn: sqlite3.Connection
    store: QdrantStore
    embedder: Embedder
    stop: threading.Event
    worker: threading.Thread | None = None


STATE = AppState()


class JobCreateRequest(BaseModel):
    mode: str = "create"
    rag_id: str | None = None
    display_name: str = "新規RAG"
    acl_tag: str = "internal"
    source_type: str = "file"
    paths: list[str] = Field(default_factory=list)
    mysql: dict[str, Any] | None = None
    tables: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    rag_id: str
    query: str
    top_k: int = 10
    score_threshold: float = 0.60
    filters: dict[str, Any] = Field(default_factory=dict)
    expand_parent: bool = True
    hybrid: bool = True


class MysqlTestRequest(BaseModel):
    host: str
    port: int = 3306
    user: str
    password: str
    database: str
    ssl_mode: str = "DISABLED"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    STATE.settings = settings
    STATE.conn = connect(settings.sqlite_path())
    migrate(STATE.conn)
    if settings.qdrant_path:
        STATE.store = QdrantStore(url=settings.qdrant_url, path=settings.qdrant_path)
    else:
        STATE.store = QdrantStore(url=settings.qdrant_url)
    STATE.embedder = Embedder(
        backend=settings.embedding_backend,
        models_dir=settings.models_dir(),
        hf_offline=settings.hf_offline,
    )
    STATE.stop = threading.Event()
    STATE.worker = threading.Thread(
        target=run_worker_loop,
        args=(STATE.conn, STATE.store, STATE.embedder, settings, STATE.stop),
        name="db2rag-worker",
        daemon=True,
    )
    STATE.worker.start()
    try:
        yield
    finally:
        STATE.stop.set()
        if STATE.worker:
            STATE.worker.join(timeout=15)
        STATE.conn.close()


def create_app() -> FastAPI:
    app = FastAPI(title="DB2RAG", version="0.1.0", lifespan=lifespan)
    _register_api(app)
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        def _page(name: str, media: str = "text/html") -> FileResponse:
            return FileResponse(
                WEB_DIR / name,
                media_type=media,
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/")
        def rag_list_page() -> FileResponse:
            return _page("index.html")

        @app.get("/wizard")
        def wizard_page() -> FileResponse:
            return _page("wizard.html")

        @app.get("/jobs")
        def jobs_page() -> FileResponse:
            return _page("jobs.html")

        @app.get("/progress")
        def progress_page() -> RedirectResponse:
            return RedirectResponse("/jobs", status_code=307)

        @app.get("/search")
        def search_page() -> FileResponse:
            return _page("search.html")

        @app.get("/mockup_cyber.css")
        def css() -> FileResponse:
            return FileResponse(WEB_DIR / "mockup_cyber.css", headers={"Cache-Control": "no-store"})

        @app.get("/mockup_cyber.js")
        def js() -> FileResponse:
            return FileResponse(WEB_DIR / "mockup_cyber.js", headers={"Cache-Control": "no-store"})

        @app.get("/live.js")
        def live() -> FileResponse:
            return FileResponse(WEB_DIR / "live.js", headers={"Cache-Control": "no-store"})

    diary = DOCS_DIR / "2026-09-10_dev-diary.html"
    if diary.is_file():

        @app.get("/dev-diary")
        def dev_diary_page() -> FileResponse:
            return FileResponse(
                diary,
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

    return app


def _register_api(app: FastAPI) -> None:
    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        qdrant = STATE.store.health()
        device = STATE.embedder.describe_device()
        sqlite_ver = schema_version(STATE.conn)
        gpu_ok = bool(device.get("cuda"))
        status = "ok"
        warnings: list[str] = []
        if not qdrant.get("ok"):
            status = "degraded"
        if not gpu_ok:
            warnings.append("GPU unavailable; embedding runs FP32 on CPU")
            status = "degraded" if status == "ok" else status
        version = str(qdrant.get("version") or "")
        if version and not version.startswith("1.19"):
            warnings.append(f"Qdrant version is {version}, expected 1.19.x")
        return {
            "ok": qdrant.get("ok", False) and sqlite_ver >= 1,
            "status": status,
            "sqlite": {"ok": True, "schema_version": sqlite_ver},
            "qdrant": qdrant,
            "gpu": device,
            "warnings": warnings,
            "time": utcnow(),
        }

    @app.get("/api/v1/rags")
    def rags() -> dict[str, Any]:
        return {"rags": list_rags(STATE.conn)}

    @app.get("/api/v1/rags/{rag_id}")
    def rag_detail(rag_id: str) -> dict[str, Any]:
        row = row_to_dict(STATE.conn.execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
        if not row:
            raise HTTPException(404, "rag not found")
        return row

    @app.delete("/api/v1/rags/{rag_id}")
    def rag_delete(rag_id: str) -> dict[str, Any]:
        try:
            return purge_rag(
                STATE.conn,
                STATE.store,
                rag_id,
                uploads_dir=STATE.settings.uploads_dir(),
                exports_dir=STATE.settings.exports_dir(),
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/v1/jobs")
    def create_job(body: JobCreateRequest) -> dict[str, Any]:
        rag = None
        if body.rag_id:
            rag = row_to_dict(
                STATE.conn.execute("SELECT * FROM rags WHERE rag_id=?", (body.rag_id,)).fetchone()
            )
            if rag is None:
                raise HTTPException(404, "rag not found")
        if body.source_type == "mysql":
            if not body.mysql:
                raise HTTPException(400, "mysql config required")
            if not body.tables:
                raise HTTPException(400, "tables required: select at least one MySQL object")
            try:
                return create_job_from_mysql(
                    STATE.conn,
                    rag=rag,
                    display_name=body.display_name,
                    acl_tag=_acl(body.acl_tag),
                    mysql_config=body.mysql,
                    tables=body.tables,
                    mode=body.mode,
                )
            except Exception as exc:
                raise HTTPException(400, str(exc)) from exc
        paths = [Path(p) for p in body.paths]
        if not paths:
            raise HTTPException(400, "paths required")
        return create_job_from_files(
            STATE.conn,
            rag=rag,
            display_name=body.display_name,
            acl_tag=_acl(body.acl_tag),
            paths=paths,
            mode=body.mode,
        )

    @app.get("/api/v1/jobs")
    def jobs() -> dict[str, Any]:
        rows = rows_to_dicts(
            STATE.conn.execute(
                """
                SELECT jobs.*, rags.display_name
                FROM jobs
                LEFT JOIN rags ON rags.rag_id = jobs.rag_id
                ORDER BY jobs.created_at DESC
                LIMIT 100
                """
            ).fetchall()
        )
        return {"jobs": rows}

    @app.get("/api/v1/jobs/{job_id}")
    def job_detail(job_id: str) -> dict[str, Any]:
        job = row_to_dict(
            STATE.conn.execute(
                """
                SELECT jobs.*, rags.display_name
                FROM jobs
                LEFT JOIN rags ON rags.rag_id = jobs.rag_id
                WHERE jobs.job_id=?
                """,
                (job_id,),
            ).fetchone()
        )
        if not job:
            raise HTTPException(404, "job not found")
        items = rows_to_dicts(
            STATE.conn.execute(
                """
                SELECT ji.*, so.object_key, so.object_type
                FROM job_items ji
                LEFT JOIN source_objects so ON so.object_id = ji.object_id
                WHERE ji.job_id=?
                ORDER BY ji.job_item_id
                """,
                (job_id,),
            ).fetchall()
        )
        job["items"] = items
        return job

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        STATE.conn.execute(
            "UPDATE jobs SET status='cancelled', finished_at=? WHERE job_id=?",
            (utcnow(), job_id),
        )
        STATE.conn.commit()
        return {"job_id": job_id, "status": "cancelled"}

    @app.post("/api/v1/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict[str, Any]:
        STATE.conn.execute(
            "UPDATE jobs SET status='pending', lease_owner=NULL, lease_expires_at=NULL WHERE job_id=?",
            (job_id,),
        )
        STATE.conn.commit()
        return {"job_id": job_id, "status": "pending"}

    @app.post("/api/v1/search")
    def search_api(body: SearchRequest) -> dict[str, Any]:
        filters = body.filters or {}
        raw_sources = filters.get("source_types") if "source_types" in filters else filters.get("source_type")
        if isinstance(raw_sources, str):
            source_types = [raw_sources] if raw_sources and raw_sources not in {"すべて", "all"} else None
        elif isinstance(raw_sources, list):
            source_types = [str(item) for item in raw_sources if item] or None
        else:
            source_types = None
        try:
            return run_search(
                STATE.conn,
                STATE.store,
                STATE.embedder,
                rag_id=body.rag_id,
                query=body.query,
                top_k=body.top_k,
                score_threshold=body.score_threshold,
                acl_tags=filters.get("acl_tags"),
                source_types=source_types,
                expand_parent=body.expand_parent,
                hybrid=body.hybrid,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/v1/mysql/test")
    def mysql_test(body: MysqlTestRequest) -> dict[str, Any]:
        try:
            conn = mysql_connect(
                host=body.host,
                port=body.port,
                user=body.user,
                password=body.password,
                database=body.database,
                ssl_mode=body.ssl_mode,
            )
            result = test_connection(conn)
            conn.close()
            return result
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/v1/mysql/objects")
    def mysql_objects(body: MysqlTestRequest) -> dict[str, Any]:
        # 接続失敗を 500 にせず、理由を toast に出せる 400 で返す
        try:
            conn = mysql_connect(
                host=body.host,
                port=body.port,
                user=body.user,
                password=body.password,
                database=body.database,
                ssl_mode=body.ssl_mode,
            )
            objects = list_objects(conn, body.database)
            conn.close()
        except Exception as exc:
            raise HTTPException(400, f"MySQL接続失敗 ({body.host}:{body.port}): {exc}") from exc
        return {"objects": objects}


    @app.post("/api/v1/capacity/estimate")
    def capacity(body: JobCreateRequest) -> dict[str, Any]:
        if body.source_type == "file":
            return estimate_paths([Path(p) for p in body.paths])
        return {"est_chunks": None, "note": "mysql estimate after object listing"}

    @app.post("/api/v1/files/upload")
    async def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        dest_dir = STATE.settings.uploads_dir() / utcnow().replace(":", "")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_root = dest_dir.resolve()
        saved: list[str] = []
        renamed = 0
        for file in files:
            # フォルダ投入の相対パスを保持し、同名ファイルの後勝ち上書きを防ぐ
            raw = (file.filename or "upload.bin").replace("\\", "/")
            parts = [p for p in raw.split("/") if p and p not in {".", ".."}]
            target = dest_dir.joinpath(*parts) if parts else dest_dir / "upload.bin"
            if not target.resolve().is_relative_to(dest_root):
                raise HTTPException(400, f"invalid upload path: {raw}")
            target.parent.mkdir(parents=True, exist_ok=True)
            stem, suffix, n = target.stem, target.suffix, 1
            while target.exists():
                target = target.with_name(f"{stem}_{n}{suffix}")
                n += 1
                renamed += 1
            target.write_bytes(await file.read())
            saved.append(str(target))
        return {"paths": saved}

    @app.post("/api/v1/export")
    def export_api(body: dict[str, str]) -> dict[str, Any]:
        rag_id = body.get("rag_id")
        if not rag_id:
            raise HTTPException(400, "rag_id required")
        try:
            return export_rag(STATE.conn, rag_id, STATE.settings.exports_dir())
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/v1/embed/golden")
    def golden() -> dict[str, Any]:
        return STATE.embedder.golden_check()

    @app.get("/api/v1/wizard-contract")
    def wizard_contract() -> dict[str, Any]:
        return {
            "screens": ["rag-list", "rag-wizard", "job-list", "search-preview"],
            "job_progress": "hover-on-job-list",
            "w1": {"auto_next": False, "confirm_required": True},
            "w2": {"auto_next": True},
        }


def _acl(raw: str) -> str:
    value = raw.split("—")[0].split("-")[0].strip()
    if value.startswith("dept:"):
        return value
    if value in {"public", "internal", "private"}:
        return value
    if "internal" in raw:
        return "internal"
    if "private" in raw:
        return "private"
    return "internal"


app = create_app()
