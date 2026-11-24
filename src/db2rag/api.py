"""FastAPI: RAG / Job / Search / Export + 承認済み5画面。"""

from __future__ import annotations

import threading
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import Response

from db2rag.auth import (
    bearer_matches,
    configured_api_key,
    is_loopback,
    path_needs_auth,
    require_api_key_for_bind,
)
from db2rag.config import Settings, get_settings, repo_root
from db2rag.db import DbManager, row_to_dict, rows_to_dicts, schema_version, utcnow
from db2rag.dify_adapter import (
    DifyRetrievalRequest,
    filter_section,
    metadata_to_filters,
    to_dify_records,
)
from db2rag.embedding import Embedder
from db2rag.eval import EvalPathError, load_dataset, resolve_eval_dataset, run_evaluation
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
from db2rag.reranker import Reranker
from db2rag.search import search as run_search
from db2rag.worker import run_worker_loop

WEB_DIR = repo_root() / "web"
DOCS_DIR = repo_root() / "docs"




class AppState:
    settings: Settings
    db: DbManager
    store: QdrantStore
    embedder: Embedder
    reranker: Reranker
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
    mysql_templates: dict[str, Any] = Field(default_factory=dict)
    mysql_pk_overrides: dict[str, list[str]] = Field(default_factory=dict)
    custom_sql: list[dict[str, Any]] = Field(default_factory=list)
    reconcile_deletes: bool = False


class SearchRequest(BaseModel):
    rag_id: str
    query: str
    top_k: int = 10
    score_threshold: float = 0.60
    filters: dict[str, Any] = Field(default_factory=dict)
    expand_parent: bool = True
    hybrid: bool = True
    dedup_parent: bool = True
    use_reranker: bool | None = None


class EvalRunRequest(BaseModel):
    rag_id: str
    dataset: str | None = None
    top_k: int = 20
    score_threshold: float = 0.0


class MysqlTestRequest(BaseModel):
    host: str
    port: int = 3306
    user: str
    password: str
    database: str
    ssl_mode: str = "DISABLED"


def package_version() -> str:
    """インストール済みパッケージの版。未インストール時は pyproject.toml。"""
    try:
        return version("db2rag")
    except PackageNotFoundError:
        project = tomllib.loads((repo_root() / "pyproject.toml").read_text(encoding="utf-8"))
        return str(project["project"]["version"])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    require_api_key_for_bind(settings.host, settings.api_key)
    STATE.settings = settings
    STATE.db = DbManager(settings.sqlite_path())
    STATE.db.connection()
    if settings.qdrant_path:
        STATE.store = QdrantStore(url=settings.qdrant_url, path=settings.qdrant_path)
    else:
        STATE.store = QdrantStore(url=settings.qdrant_url)
    STATE.embedder = Embedder(
        backend=settings.embedding_backend,
        models_dir=settings.models_dir(),
        hf_offline=settings.hf_offline,
    )
    # 既定は無効。score() までモデルを読まない。
    STATE.reranker = Reranker(
        enabled=settings.reranker_enabled,
        model_id=settings.reranker_model,
        models_dir=settings.models_dir(),
        top_n=settings.reranker_top_n,
    )
    STATE.stop = threading.Event()
    STATE.worker = threading.Thread(
        target=run_worker_loop,
        args=(STATE.db, STATE.store, STATE.embedder, settings, STATE.stop),
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
        STATE.db.close_all()


def mysql_job_config(
    mysql: dict[str, Any],
    *,
    templates: dict[str, Any],
    pk_overrides: dict[str, list[str]],
    custom_sql: list[dict[str, Any]],
    reconcile_deletes: bool = False,
) -> dict[str, Any]:
    """接続設定に、表ごとのテンプレート・PK・カスタム SQL を載せる。

    Args:
        mysql: host などの接続情報。パスワードは後段で暗号化する。
        templates: キーは ``schema.table``。
        pk_overrides: ビューなど主キーを持たないオブジェクトの列。
        custom_sql: ``object_key`` と ``sql`` を持つ READ ONLY 文。

    Returns:
        ``create_job_from_mysql`` に渡す設定。
    """
    stored = dict(mysql)
    if templates:
        stored["template"] = templates
    if pk_overrides:
        stored["pk_columns"] = pk_overrides
    if custom_sql:
        stored["custom_sql"] = custom_sql
    if reconcile_deletes:
        stored["reconcile_deletes"] = True
    return stored


async def auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    settings = getattr(STATE, "settings", None)
    if settings is not None and path_needs_auth(
        request.url.path,
        api_key=settings.api_key,
        host=settings.host,
    ):
        expected = configured_api_key(settings.api_key) or ""
        if not bearer_matches(request.headers.get("authorization"), expected):
            return JSONResponse(
                status_code=403,
                content={"error_code": 1002, "error_msg": "Authorization failed"},
            )
    return await call_next(request)


def create_app() -> FastAPI:
    app = FastAPI(title="DB2RAG", version=package_version(), lifespan=lifespan)
    app.middleware("http")(auth_middleware)
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
        sqlite_ver = schema_version(STATE.db.connection())
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
            "auth": {
                "auth_required": configured_api_key(STATE.settings.api_key) is not None
                and not is_loopback(STATE.settings.host),
                "loopback": is_loopback(STATE.settings.host),
            },
        }

    @app.get("/api/v1/rags")
    def rags() -> dict[str, Any]:
        return {"rags": list_rags(STATE.db.connection())}

    @app.get("/api/v1/rags/{rag_id}")
    def rag_detail(rag_id: str) -> dict[str, Any]:
        row = row_to_dict(STATE.db.connection().execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
        if not row:
            raise HTTPException(404, "rag not found")
        return row

    @app.delete("/api/v1/rags/{rag_id}")
    def rag_delete(rag_id: str) -> dict[str, Any]:
        try:
            return purge_rag(
                STATE.db.connection(),
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
                STATE.db.connection().execute("SELECT * FROM rags WHERE rag_id=?", (body.rag_id,)).fetchone()
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
                    STATE.db.connection(),
                    rag=rag,
                    display_name=body.display_name,
                    acl_tag=_acl(body.acl_tag),
                    mysql_config=mysql_job_config(
                        body.mysql,
                        templates=body.mysql_templates,
                        pk_overrides=body.mysql_pk_overrides,
                        custom_sql=body.custom_sql,
                        reconcile_deletes=body.reconcile_deletes,
                    ),
                    tables=body.tables,
                    mode=body.mode,
                    secrets_key_path=STATE.settings.secrets_key_path(),
                )
            except Exception as exc:
                raise HTTPException(400, str(exc)) from exc
        paths = [Path(p) for p in body.paths]
        if not paths:
            raise HTTPException(400, "paths required")
        return create_job_from_files(
            STATE.db.connection(),
            rag=rag,
            display_name=body.display_name,
            acl_tag=_acl(body.acl_tag),
            paths=paths,
            mode=body.mode,
        )

    @app.get("/api/v1/jobs")
    def jobs() -> dict[str, Any]:
        rows = rows_to_dicts(
            STATE.db.connection().execute(
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
            STATE.db.connection().execute(
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
            STATE.db.connection().execute(
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
        STATE.db.connection().execute(
            "UPDATE jobs SET status='cancelled', finished_at=? WHERE job_id=?",
            (utcnow(), job_id),
        )
        STATE.db.connection().commit()
        return {"job_id": job_id, "status": "cancelled"}

    @app.post("/api/v1/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict[str, Any]:
        STATE.db.connection().execute(
            "UPDATE jobs SET status='pending', lease_owner=NULL, lease_expires_at=NULL WHERE job_id=?",
            (job_id,),
        )
        STATE.db.connection().commit()
        return {"job_id": job_id, "status": "pending"}

    @app.get("/api/v1/version")
    def version_api() -> dict[str, Any]:
        return {
            "name": "DB2RAG",
            "version": package_version(),
            "reranker_enabled": STATE.settings.reranker_enabled,
        }

    @app.get("/api/v1/rags/{rag_id}/dify-config")
    def dify_config(rag_id: str) -> dict[str, Any]:
        row = row_to_dict(STATE.db.connection().execute("SELECT rag_id FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
        if not row:
            raise HTTPException(404, "rag not found")
        port = STATE.settings.api_port
        default_acl = _dify_acl_for(rag_id)
        return {
            "endpoint_docker": f"http://host.docker.internal:{port}/api/v1/dify",
            "endpoint_local": f"http://127.0.0.1:{port}/api/v1/dify",
            "knowledge_id": rag_id,
            "auth_required": configured_api_key(STATE.settings.api_key) is not None,
            "default_acl": default_acl,
            "dify_ssrf_env_hint": {
                "variable": "SSRF_PROXY_ALLOW_PRIVATE_DOMAINS",
                "example_value": "host.docker.internal",
                "restart_note": "Dify の docker/.env に書いて ssrf_proxy を再起動する。未設定だと private 宛の外部知識が遮断される。",
            },
        }

    @app.post("/api/v1/dify/retrieval")
    def dify_retrieval(body: DifyRetrievalRequest) -> Any:
        if body.knowledge_id == "":
            return {"records": []}
        row = row_to_dict(
            STATE.db.connection().execute("SELECT rag_id FROM rags WHERE rag_id=?", (body.knowledge_id,)).fetchone()
        )
        if not row:
            return JSONResponse(
                status_code=404,
                content={"error_code": 2001, "error_msg": "The knowledge does not exist"},
            )
        try:
            filters = metadata_to_filters(body.metadata_condition, default_acl=_dify_acl_for(body.knowledge_id))
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error_code": 400, "error_msg": str(exc)})
        if not body.query.strip():
            return {"records": []}
        # Dify は返却 score（RRF）で閾値判定する。dense 足切りには渡さない。
        fetch_k = min(50, body.retrieval_setting.top_k * 3)
        try:
            found = run_search(
                STATE.db.connection(),
                STATE.store,
                STATE.embedder,
                rag_id=body.knowledge_id,
                query=body.query,
                top_k=fetch_k,
                score_threshold=0.0,
                acl_tags=filters.acl_tags,
                source_types=filters.source_types,
                expand_parent=True,
                hybrid=True,
                dedup_parent=True,
                use_reranker=False,
                reranker=STATE.reranker,
            )
        except ValueError:
            return JSONResponse(
                status_code=404,
                content={"error_code": 2001, "error_msg": "The knowledge does not exist"},
            )
        records = to_dify_records(
            list(found.get("hits") or []),
            rag_id=body.knowledge_id,
            content_mode=STATE.settings.dify_content_mode,
        )
        records = filter_section(records, filters.section_contains)
        threshold = body.retrieval_setting.score_threshold
        records = [record for record in records if float(record["score"]) >= threshold]
        payload: dict[str, Any] = {"records": records[: body.retrieval_setting.top_k]}
        if filters.warnings:
            payload["warnings"] = filters.warnings
        return payload

    @app.post("/api/v1/search")
    def search_api(body: SearchRequest) -> Any:
        filters = body.filters or {}
        if _lan_requires_acl(filters):
            return JSONResponse(
                status_code=403,
                content={"error_code": 403, "error_msg": "acl_tags is required"},
            )
        raw_sources = filters.get("source_types") if "source_types" in filters else filters.get("source_type")
        if isinstance(raw_sources, str):
            source_types = [raw_sources] if raw_sources and raw_sources not in {"すべて", "all"} else None
        elif isinstance(raw_sources, list):
            source_types = [str(item) for item in raw_sources if item] or None
        else:
            source_types = None
        try:
            return run_search(
                STATE.db.connection(),
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
                dedup_parent=body.dedup_parent,
                use_reranker=body.use_reranker,
                reranker=STATE.reranker,
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
    async def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:  # noqa: B008
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

    @app.post("/api/v1/eval/run")
    def eval_run(body: EvalRunRequest) -> dict[str, Any]:
        try:
            path = resolve_eval_dataset(body.dataset)
        except EvalPathError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            return run_evaluation(
                STATE.db.connection(),
                STATE.store,
                STATE.embedder,
                rag_id=body.rag_id,
                dataset=load_dataset(path),
                top_k=body.top_k,
                score_threshold=body.score_threshold,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/v1/export")
    def export_api(body: dict[str, str]) -> dict[str, Any]:
        rag_id = body.get("rag_id")
        if not rag_id:
            raise HTTPException(400, "rag_id required")
        try:
            return export_rag(STATE.db.connection(), rag_id, STATE.settings.exports_dir())
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


def _dify_acl_for(rag_id: str) -> str:
    """RAG 行の Dify 既定 ACL。未設定ならその RAG の acl_tag_default。"""
    row = row_to_dict(STATE.db.connection().execute("SELECT * FROM rags WHERE rag_id=?", (rag_id,)).fetchone())
    if row is None:
        return STATE.settings.dify_default_acl
    custom = row.get("dify_default_acl")
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    tag = row.get("acl_tag_default")
    if isinstance(tag, str) and tag.strip():
        return tag.strip()
    return STATE.settings.dify_default_acl


def _lan_requires_acl(filters: dict[str, Any]) -> bool:
    """LAN では acl_tags なしの検索を拒否する。loopback の画面はそのまま。"""
    settings = getattr(STATE, "settings", None)
    if settings is None or not settings.require_acl_in_lan or is_loopback(settings.host):
        return False
    tags = filters.get("acl_tags")
    if isinstance(tags, str):
        return not tags.strip()
    if isinstance(tags, list):
        return not any(str(item).strip() for item in tags)
    return True


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
