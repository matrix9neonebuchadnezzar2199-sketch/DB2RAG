"""Phase 6: 評価メトリクス、未測定は不合格、ベースラインの件数。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
import zstandard as zstd
from fastapi.testclient import TestClient

from db2rag.api import app
from db2rag.config import repo_root
from db2rag.db import connect, migrate, utcnow
from db2rag.eval import (
    EvalDataset,
    EvalPathError,
    EvalQuestion,
    load_dataset,
    resolve_eval_dataset,
    run_evaluation,
)
from db2rag.export import export_rag


def _rag(tmp_path: Path) -> Any:
    conn = connect(tmp_path / "db.db")
    migrate(conn)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO rags (
            rag_id, display_name, status, qdrant_alias, schema_version,
            acl_tag_default, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("rag-eval", "eval", "active", "alias", 1, "internal", now, now),
    )
    conn.commit()
    return conn


def test_metrics_gate_and_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs["query"] == "trap":
            return {"hits": [{"snippet": "別の文書", "child_chunk_id": "other"}]}
        if kwargs["query"] == "empty":
            return {"hits": []}
        return {"hits": [{"snippet": "NEEDLE", "child_chunk_id": "ok", "parent_chunk_id": "parent"}]}

    monkeypatch.setattr("db2rag.eval.search", fake_search)
    conn = _rag(tmp_path)
    dataset = EvalDataset(
        question_set_version="unit-v1",
        questions=[
            EvalQuestion(question_id="Q-1", query="paid", expected_needles=["NEEDLE"]),
            EvalQuestion(question_id="Q-2", query="empty", expected_child_chunk_ids=["ok"]),
            EvalQuestion(
                question_id="HN-1",
                query="trap",
                category="hard_negative",
                has_answer=False,
                forbidden_needles=["FORBIDDEN"],
            ),
        ],
    )
    report = run_evaluation(conn, object(), object(), rag_id="rag-eval", dataset=dataset, top_k=20, score_threshold=0.0)  # type: ignore[arg-type]
    metrics = report["metrics"]
    assert metrics["recall_at_5"] == pytest.approx(0.5)
    assert metrics["mrr_at_10"] == pytest.approx(0.5)
    assert metrics["zero_result_rate"] == pytest.approx(0.5)
    assert metrics["hard_negative_fp_at_5"] == pytest.approx(0.0)
    assert metrics["ndcg_at_10"] == pytest.approx(0.5)
    assert report["gate_passed"] is False
    stored = conn.execute("SELECT gate_passed, metrics_json FROM evaluation_runs").fetchone()
    assert stored["gate_passed"] == 0
    assert json.loads(stored["metrics_json"])["misses"] == ["Q-2"]


def test_unmeasured_hard_negative_does_not_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "db2rag.eval.search",
        lambda *args, **kwargs: {"hits": [{"snippet": "NEEDLE", "child_chunk_id": "ok"}]},
    )
    conn = _rag(tmp_path)
    dataset = EvalDataset(
        questions=[EvalQuestion(question_id="Q-1", query="paid", expected_needles=["NEEDLE"])]
    )
    report = run_evaluation(conn, object(), object(), rag_id="rag-eval", dataset=dataset)  # type: ignore[arg-type]
    assert report["metrics"]["hard_negative_fp_at_5"] is None
    assert report["gate_passed"] is False


def test_package_version_matches_pyproject() -> None:
    import tomllib

    from db2rag.api import package_version

    project = tomllib.loads((repo_root() / "pyproject.toml").read_text(encoding="utf-8"))
    assert package_version() == project["project"]["version"] == "0.2.1"


def test_baseline_has_25_questions_and_8_hard_negatives() -> None:
    dataset = load_dataset(repo_root() / "testdata" / "eval_baseline.json")
    answered = [item for item in dataset.questions if item.has_answer]
    hard = [item for item in dataset.questions if item.category == "hard_negative"]
    assert len(answered) >= 25
    assert len(hard) >= 8
    assert all(item.expected_needles for item in answered)
    assert all(item.forbidden_needles for item in hard)


def test_eval_dataset_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(EvalPathError):
        resolve_eval_dataset("../pyproject.toml")
    with pytest.raises(EvalPathError):
        resolve_eval_dataset(str(tmp_path / "outside.json"))


def test_eval_run_endpoint_rejects_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB2RAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DB2RAG_QDRANT_PATH", str(tmp_path / "qdrant"))
    monkeypatch.setenv("DB2RAG_EMBEDDING_BACKEND", "hash")
    monkeypatch.setenv("DB2RAG_API_KEY", "test-key")
    monkeypatch.setenv("DB2RAG_HOST", "127.0.0.1")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/eval/run",
            headers={"Authorization": "Bearer test-key"},
            json={"rag_id": "missing", "dataset": "../pyproject.toml"},
        )
    assert response.status_code == 400


def test_export_omits_retired_versions(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.db")
    migrate(conn)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO rags (
            rag_id, display_name, status, qdrant_alias, schema_version,
            acl_tag_default, created_at, updated_at
        ) VALUES ('rag-exp', 'exp', 'active', 'alias', 1, 'internal', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO sources (
            source_id, rag_id, source_type, source_name, config_json, acl_tag_default, enabled, created_at
        ) VALUES ('src', 'rag-exp', 'mysql', 'hr', '{}', 'internal', 1, ?)
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT INTO source_objects (object_id, source_id, object_type, object_key)
        VALUES ('obj', 'src', 'table', 'hr.staff')
        """
    )
    for doc_id, latest in (("old", 0), ("new", 1)):
        conn.execute(
            """
            INSERT INTO document_versions (
                doc_version_id, object_id, content_hash, pipeline_fingerprint, created_at, is_latest, record_id
            ) VALUES (?, 'obj', ?, 'fp', ?, ?, ?)
            """,
            (doc_id, doc_id, now, latest, doc_id),
        )
    conn.commit()
    exported = export_rag(conn, "rag-exp", tmp_path / "exports")
    assert exported["manifest_sha256"]
    blob = (tmp_path / "exports").joinpath(exported["export_id"], "documents.jsonl.zst").read_bytes()
    with zstd.ZstdDecompressor().stream_reader(io.BytesIO(blob)) as reader:
        text = reader.read().decode("utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    assert [row["doc_version_id"] for row in rows] == ["new"]
