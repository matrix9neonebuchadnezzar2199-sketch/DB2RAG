"""Phase 5: Dify 外部知識 API、認証、LAN の ACL。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from db2rag.api import STATE, app
from db2rag.auth import require_api_key_for_bind
from db2rag.db import utcnow


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: str | None) -> None:
    values: dict[str, str | None] = {
        "DB2RAG_DATA_DIR": str(tmp_path / "data"),
        "DB2RAG_QDRANT_PATH": str(tmp_path / "qdrant"),
        "DB2RAG_EMBEDDING_BACKEND": "hash",
        "DB2RAG_API_KEY": "test-key",
        "DB2RAG_HOST": "127.0.0.1",
    }
    values.update(overrides)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-key"}


def _insert_rag(rag_id: str = "rag-test") -> None:
    now = utcnow()
    STATE.db.connection().execute(
        """
        INSERT INTO rags (
            rag_id, display_name, status, qdrant_alias, schema_version,
            acl_tag_default, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (rag_id, "test", "active", "alias", 1, "internal", now, now),
    )
    STATE.db.connection().commit()


def _hit() -> dict[str, Any]:
    return {
        "score": 0.8421,
        "dense_score": 0.81,
        "title": "年次有給休暇",
        "section_path": "第5章 > 休暇",
        "source_type": "file",
        "acl_tag": "internal",
        "snippet": "子の抜粋",
        "parent_text": "親の本文10日前",
        "child_chunk_id": "child-1",
        "parent_chunk_id": "parent-1",
        "record_id": "hr.regulations:1",
    }


def test_dify_probe_empty_knowledge_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/dify/retrieval",
            headers=_auth(),
            json={"knowledge_id": "", "query": "", "retrieval_setting": {"top_k": 1, "score_threshold": 0.0}},
        )
    assert response.status_code == 200
    assert response.json() == {"records": []}


def test_dify_auth_failure_returns_403_code_1002(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        missing = client.post("/api/v1/dify/retrieval", json={"knowledge_id": "", "query": ""})
        wrong = client.post(
            "/api/v1/dify/retrieval",
            headers={"Authorization": "Bearer nope"},
            json={"knowledge_id": "", "query": ""},
        )
        local_search = client.post("/api/v1/search", json={"rag_id": "missing", "query": "q", "filters": {}})
    assert missing.status_code == 403
    assert missing.json()["error_code"] == 1002
    assert wrong.status_code == 403
    assert wrong.json()["error_msg"] == "Authorization failed"
    assert local_search.status_code != 403


def test_dify_unknown_knowledge_returns_404_code_2001(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/dify/retrieval",
            headers=_auth(),
            json={"knowledge_id": "missing-rag", "query": "有給"},
        )
    assert response.status_code == 404
    assert response.json()["error_code"] == 2001


def test_dify_retrieval_returns_parent_content_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def fake_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"hits": [_hit()]}

    monkeypatch.setattr("db2rag.api.run_search", fake_search)
    with TestClient(app) as client:
        _insert_rag()
        response = client.post(
            "/api/v1/dify/retrieval",
            headers=_auth(),
            json={"knowledge_id": "rag-test", "query": "有給", "retrieval_setting": {"top_k": 5, "score_threshold": 0.0}},
        )
    body = response.json()
    assert response.status_code == 200
    record = body["records"][0]
    assert record["content"] == "親の本文10日前"
    assert 0.0 <= record["score"] <= 1.0
    assert record["title"] == "年次有給休暇 / 第5章 > 休暇"
    assert record["metadata"]["parent_chunk_id"] == "parent-1"
    assert record["metadata"]["dense_score"] == 0.81
    assert captured["dedup_parent"] is True
    assert captured["expand_parent"] is True


def test_dify_metadata_condition_filters_acl_and_source_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, tmp_path)
    seen: list[dict[str, Any]] = []

    def fake_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"hits": [_hit()]}

    monkeypatch.setattr("db2rag.api.run_search", fake_search)
    with TestClient(app) as client:
        _insert_rag()
        default = client.post(
            "/api/v1/dify/retrieval",
            headers=_auth(),
            json={"knowledge_id": "rag-test", "query": "有給"},
        )
        filtered = client.post(
            "/api/v1/dify/retrieval",
            headers=_auth(),
            json={
                "knowledge_id": "rag-test",
                "query": "有給",
                "metadata_condition": {
                    "logical_operator": "and",
                    "conditions": [
                        {"name": "acl_tag", "comparison_operator": "is", "value": "private"},
                        {"name": "source_type", "comparison_operator": "in", "value": "file,mysql"},
                    ],
                },
            },
        )
        rejected = client.post(
            "/api/v1/dify/retrieval",
            headers=_auth(),
            json={
                "knowledge_id": "rag-test",
                "query": "有給",
                "metadata_condition": {
                    "conditions": [{"name": "secret", "comparison_operator": "is", "value": "x"}]
                },
            },
        )
    assert default.status_code == 200
    assert seen[0]["acl_tags"] == ["internal"]
    assert filtered.status_code == 200
    assert seen[1]["acl_tags"] == ["private"]
    assert seen[1]["source_types"] == ["file", "mysql"]
    assert rejected.status_code == 200
    assert "unsupported metadata field" in rejected.json()["warnings"][0]


def test_dify_threshold_uses_returned_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def fake_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        low = dict(_hit())
        low["score"] = 0.2
        high = dict(_hit())
        high["score"] = 0.9
        high["child_chunk_id"] = "child-high"
        return {"hits": [low, high]}

    monkeypatch.setattr("db2rag.api.run_search", fake_search)
    with TestClient(app) as client:
        _insert_rag()
        response = client.post(
            "/api/v1/dify/retrieval",
            headers=_auth(),
            json={"knowledge_id": "rag-test", "query": "有給", "retrieval_setting": {"top_k": 5, "score_threshold": 0.5}},
        )
    assert response.status_code == 200
    assert captured["score_threshold"] == 0.0
    assert captured["top_k"] == 15
    ids = [item["metadata"]["child_chunk_id"] for item in response.json()["records"]]
    assert ids == ["child-high"]


def test_dify_config_includes_ssrf_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    with TestClient(app) as client:
        _insert_rag()
        response = client.get("/api/v1/rags/rag-test/dify-config", headers=_auth())
    hint = response.json()["dify_ssrf_env_hint"]
    assert hint["variable"] == "SSRF_PROXY_ALLOW_PRIVATE_DOMAINS"
    assert hint["example_value"] == "host.docker.internal"


def test_lan_mode_requires_api_key_and_acl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="DB2RAG_API_KEY is required"):
        require_api_key_for_bind("0.0.0.0", None)
    _env(monkeypatch, tmp_path, DB2RAG_HOST="0.0.0.0")

    def fake_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"hits": []}

    monkeypatch.setattr("db2rag.api.run_search", fake_search)
    with TestClient(app) as client:
        _insert_rag()
        blocked = client.post(
            "/api/v1/search",
            headers=_auth(),
            json={"rag_id": "rag-test", "query": "有給", "filters": {}},
        )
        allowed = client.post(
            "/api/v1/search",
            headers=_auth(),
            json={"rag_id": "rag-test", "query": "有給", "filters": {"acl_tags": ["internal"]}},
        )
    assert blocked.status_code == 403
    assert blocked.json()["error_msg"] == "acl_tags is required"
    assert allowed.status_code == 200
