"""検索の定量評価。未測定の指標は null のまま合格にしない。"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from db2rag.db import utcnow
from db2rag.embedding import Embedder
from db2rag.ids import new_id
from db2rag.qdrant_store import QdrantStore
from db2rag.search import search

RECALL_AT_5_MIN = 0.75
MRR_AT_10_MIN = 0.65
ZERO_RESULT_MAX = 0.05
HARD_NEGATIVE_FP_MAX = 0.10


class EvalQuestion(BaseModel):
    question_id: str
    query: str
    expected_needles: list[str] = Field(default_factory=list)
    expected_child_chunk_ids: list[str] = Field(default_factory=list)
    expected_parent_chunk_ids: list[str] = Field(default_factory=list)
    forbidden_needles: list[str] = Field(default_factory=list)
    forbidden_chunk_ids: list[str] = Field(default_factory=list)
    category: str = "general"
    has_answer: bool = True


class EvalDataset(BaseModel):
    question_set_version: str = "eval_baseline_v1"
    questions: list[EvalQuestion]


class EvalPathError(ValueError):
    """評価データセットのパスが許可ディレクトリの外。"""


def resolve_eval_dataset(raw: str | None) -> Path:
    """testdata/ と data/eval/ の相対パスだけを開く。

    Raises:
        EvalPathError: 絶対パス、``..``、許可外、またはファイルが無いとき。
    """
    from db2rag.config import repo_root

    root = repo_root().resolve()
    allowed = [(root / "testdata").resolve(), (root / "data" / "eval").resolve()]
    if raw is None or not raw.strip():
        path = (root / "testdata" / "eval_baseline.json").resolve()
    else:
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise EvalPathError("dataset path is not allowed")
        path = (root / candidate).resolve()
    if not any(path == base or base in path.parents for base in allowed):
        raise EvalPathError("dataset path is not allowed")
    if not path.is_file():
        raise EvalPathError("dataset not found")
    return path


def load_dataset(path: Path) -> EvalDataset:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return EvalDataset.model_validate(payload)


def run_evaluation(
    conn: sqlite3.Connection,
    store: QdrantStore,
    embedder: Embedder,
    *,
    rag_id: str,
    dataset: EvalDataset,
    top_k: int = 20,
    score_threshold: float = 0.0,
) -> dict[str, Any]:
    """各問を検索し、メトリクスと gate を保存する。"""
    answered_ids = {
        item.question_id for item in dataset.questions if item.has_answer and _has_expected(item)
    }
    hard_ids = {item.question_id for item in dataset.questions if _is_hard_negative(item)}
    ranks: list[int | None] = []
    empty = 0
    false_positives = 0
    top1_false = 0
    latencies: list[float] = []
    misses: list[str] = []

    for item in dataset.questions:
        started = time.perf_counter()
        found = search(
            conn,
            store,
            embedder,
            rag_id=rag_id,
            query=item.query,
            top_k=top_k,
            score_threshold=score_threshold,
            expand_parent=True,
            hybrid=True,
            dedup_parent=False,
        )
        latencies.append((time.perf_counter() - started) * 1000)
        hits = list(found.get("hits") or [])
        if item.question_id in answered_ids:
            rank = _first_relevant_rank(hits, item)
            ranks.append(rank)
            if not hits:
                empty += 1
            if rank is None:
                misses.append(item.question_id)
        if item.question_id in hard_ids and _forbidden_in_top(hits, item, 5):
            false_positives += 1
        if item.question_id in hard_ids and _forbidden_in_top(hits, item, 1):
            top1_false += 1

    metrics: dict[str, Any] = {
        "recall_at_5": _recall(ranks, 5),
        "recall_at_20": _recall(ranks, 20),
        "mrr_at_10": _mrr(ranks, 10),
        "ndcg_at_10": _ndcg(ranks, 10),
        "zero_result_rate": (empty / len(answered_ids)) if answered_ids else None,
        "hard_negative_fp_at_5": (false_positives / len(hard_ids)) if hard_ids else None,
        "hard_negative_top1_rate": (top1_false / len(hard_ids)) if hard_ids else None,
        "latency_p95_ms": _percentile(latencies, 95) if latencies else None,
        "question_count": len(dataset.questions),
        "answered_count": len(answered_ids),
        "hard_negative_count": len(hard_ids),
        "misses": misses,
    }
    passed = _gate(metrics)
    eval_run_id = new_id("eval")
    conn.execute(
        """
        INSERT INTO evaluation_runs (
            eval_run_id, rag_id, question_set_version, metrics_json, gate_passed, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            eval_run_id,
            rag_id,
            dataset.question_set_version,
            json.dumps(metrics, ensure_ascii=False),
            1 if passed else 0,
            utcnow(),
        ),
    )
    conn.commit()
    return {
        "eval_run_id": eval_run_id,
        "rag_id": rag_id,
        "question_set_version": dataset.question_set_version,
        "metrics": metrics,
        "gate_passed": passed,
    }


def _gate(metrics: dict[str, Any]) -> bool:
    recall = metrics.get("recall_at_5")
    mrr = metrics.get("mrr_at_10")
    zero = metrics.get("zero_result_rate")
    hard = metrics.get("hard_negative_top1_rate")
    if recall is None or mrr is None or zero is None or hard is None:
        return False
    return (
        float(recall) >= RECALL_AT_5_MIN
        and float(mrr) >= MRR_AT_10_MIN
        and float(zero) <= ZERO_RESULT_MAX
        and float(hard) <= HARD_NEGATIVE_FP_MAX
    )


def _has_expected(item: EvalQuestion) -> bool:
    return bool(item.expected_needles or item.expected_child_chunk_ids or item.expected_parent_chunk_ids)


def _is_hard_negative(item: EvalQuestion) -> bool:
    return item.category == "hard_negative" or bool(item.forbidden_needles or item.forbidden_chunk_ids)


def _first_relevant_rank(hits: list[dict[str, Any]], item: EvalQuestion) -> int | None:
    for index, hit in enumerate(hits, start=1):
        if _hit_matches_expected(hit, item):
            return index
    return None


def _hit_matches_expected(hit: dict[str, Any], item: EvalQuestion) -> bool:
    child = str(hit.get("child_chunk_id") or "")
    parent = str(hit.get("parent_chunk_id") or "")
    if item.expected_child_chunk_ids and child in item.expected_child_chunk_ids:
        return True
    if item.expected_parent_chunk_ids and parent in item.expected_parent_chunk_ids:
        return True
    blob = _blob(hit)
    return any(needle and needle in blob for needle in item.expected_needles)


def _forbidden_in_top(hits: list[dict[str, Any]], item: EvalQuestion, limit: int) -> bool:
    for hit in hits[:limit]:
        child = str(hit.get("child_chunk_id") or "")
        parent = str(hit.get("parent_chunk_id") or "")
        if child and child in item.forbidden_chunk_ids:
            return True
        if parent and parent in item.forbidden_chunk_ids:
            return True
        blob = _blob(hit)
        if any(needle and needle in blob for needle in item.forbidden_needles):
            return True
    return False


def _blob(hit: dict[str, Any]) -> str:
    parts = [
        hit.get("snippet"),
        hit.get("parent_text"),
        hit.get("title"),
        hit.get("section_path"),
    ]
    return "\n".join(str(part) for part in parts if part)


def _recall(ranks: list[int | None], k: int) -> float | None:
    if not ranks:
        return None
    found = sum(1 for rank in ranks if rank is not None and rank <= k)
    return found / len(ranks)


def _mrr(ranks: list[int | None], k: int) -> float | None:
    if not ranks:
        return None
    total = 0.0
    for rank in ranks:
        if rank is not None and rank <= k:
            total += 1.0 / rank
    return total / len(ranks)


def _ndcg(ranks: list[int | None], k: int) -> float | None:
    """正解は1件の二値適合。理想 DCG は rank 1。"""
    if not ranks:
        return None
    total = 0.0
    for rank in ranks:
        if rank is not None and rank <= k:
            total += 1.0 / math.log2(rank + 1)
    return total / len(ranks)


def _percentile(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (percentile / 100)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight
