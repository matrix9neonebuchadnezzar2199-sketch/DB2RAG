"""Phase 6: 評価メトリクス、未測定は不合格、ベースラインの件数。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from db2rag.config import repo_root
from db2rag.db import connect, migrate, utcnow
from db2rag.eval import EvalDataset, EvalQuestion, load_dataset, run_evaluation


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
    assert package_version() == project["project"]["version"] == "0.2.0"


def test_baseline_has_25_questions_and_8_hard_negatives() -> None:
    dataset = load_dataset(repo_root() / "testdata" / "eval_baseline.json")
    answered = [item for item in dataset.questions if item.has_answer]
    hard = [item for item in dataset.questions if item.category == "hard_negative"]
    assert len(answered) >= 25
    assert len(hard) >= 8
    assert all(item.expected_needles for item in answered)
    assert all(item.forbidden_needles for item in hard)
