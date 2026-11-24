"""Dify External Knowledge API の入出力へ DB2RAG 検索を合わせる。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

ALLOWED_FIELDS = frozenset({"acl_tag", "source_type", "section_path"})
ALLOWED_OPERATORS = frozenset({"is", "in", "contains"})


class DifyRetrievalSetting(BaseModel):
    top_k: int = Field(default=5, ge=1, le=50)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)


class DifyMetadataConditionItem(BaseModel):
    # Dify の Condition.name は文字列。設計の list も同じ1フィールドとして受ける。
    name: str | list[str]
    comparison_operator: str
    value: Any = None


class DifyMetadataCondition(BaseModel):
    logical_operator: str = "and"
    conditions: list[DifyMetadataConditionItem] | None = None


class DifyRetrievalRequest(BaseModel):
    knowledge_id: str = ""
    query: str = ""
    retrieval_setting: DifyRetrievalSetting = Field(default_factory=DifyRetrievalSetting)
    metadata_condition: DifyMetadataCondition | None = None


class SearchFilters(BaseModel):
    acl_tags: list[str]
    source_types: list[str] | None = None
    section_contains: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def metadata_to_filters(
    condition: DifyMetadataCondition | None,
    *,
    default_acl: str,
) -> SearchFilters:
    """許可フィールドだけを検索フィルタにする。ACL 未指定は既定 ACL を強制する。

    未知フィールドは検索条件にせず warnings に残す。Dify の自動 metadata が
    任意名を送っても retrieval 全体を止めない。

    Raises:
        ValueError: 未知の演算子、または複数条件の or。
    """
    items = [] if condition is None or not condition.conditions else list(condition.conditions)
    operator = (condition.logical_operator if condition else "and").strip().lower()
    if operator not in {"and", "or"}:
        raise ValueError(f"unsupported logical_operator: {operator}")
    if operator == "or" and len(items) > 1:
        raise ValueError("logical_operator or is not supported")

    acl_sets: list[set[str]] = []
    source_sets: list[set[str]] = []
    sections: list[str] = []
    warnings: list[str] = []
    for item in items:
        try:
            field = _field_name(item.name)
        except ValueError as exc:
            if not str(exc).startswith("unsupported metadata field"):
                raise
            warnings.append(str(exc))
            continue
        op = item.comparison_operator.strip().lower()
        if op not in ALLOWED_OPERATORS:
            raise ValueError(f"unsupported comparison_operator: {item.comparison_operator}")
        if op == "contains" and field != "section_path":
            raise ValueError("contains is only supported for section_path")
        if op != "contains" and field == "section_path":
            raise ValueError("section_path only supports contains")
        if field == "section_path":
            text = _scalar(item.value)
            if text:
                sections.append(text)
            continue
        values = _values(item.value, op)
        if field == "acl_tag":
            acl_sets.append(set(values))
        else:
            source_sets.append(set(values))

    acl_tags = _intersect(acl_sets) if acl_sets else [default_acl]
    source_types = _intersect(source_sets) if source_sets else None
    return SearchFilters(
        acl_tags=acl_tags,
        source_types=source_types,
        section_contains=sections,
        warnings=warnings,
    )


def to_dify_records(hits: list[dict[str, Any]], *, rag_id: str, content_mode: str) -> list[dict[str, Any]]:
    """検索ヒットを Dify の records 配列にする。score は 0 から 1 に収める。"""
    records: list[dict[str, Any]] = []
    for hit in hits:
        parent = _text(hit.get("parent_text"))
        snippet = _text(hit.get("snippet"))
        content = parent if content_mode == "parent" and parent else snippet or parent
        title = _text(hit.get("title"))
        section = _text(hit.get("section_path"))
        shown = f"{title} / {section}" if title and section else title or section
        score = _unit_score(hit.get("score"))
        records.append(
            {
                "content": content,
                "score": score,
                "title": shown,
                "metadata": {
                    "rag_id": rag_id,
                    "source_type": hit.get("source_type"),
                    "acl_tag": hit.get("acl_tag"),
                    "section_path": section,
                    "record_id": hit.get("record_id"),
                    "parent_chunk_id": hit.get("parent_chunk_id"),
                    "child_chunk_id": hit.get("child_chunk_id"),
                    "dense_score": hit.get("dense_score"),
                },
            }
        )
    return records


def filter_section(records: list[dict[str, Any]], needles: list[str]) -> list[dict[str, Any]]:
    """section_path がすべての部分文字列を含むレコードだけ残す。"""
    if not needles:
        return records
    kept: list[dict[str, Any]] = []
    for record in records:
        metadata = record.get("metadata")
        section = ""
        if isinstance(metadata, dict):
            section = _text(metadata.get("section_path"))
        if all(needle in section for needle in needles):
            kept.append(record)
    return kept


def _field_name(raw: str | list[str]) -> str:
    if isinstance(raw, str):
        name = raw.strip()
    elif len(raw) == 1 and isinstance(raw[0], str):
        name = raw[0].strip()
    else:
        raise ValueError("metadata field name must be a single string")
    if name not in ALLOWED_FIELDS:
        raise ValueError(f"unsupported metadata field: {name}")
    return name


def _values(value: Any, operator: str) -> list[str]:
    if operator == "is":
        text = _scalar(value)
        if not text:
            raise ValueError("operator is requires a value")
        return [text]
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError("operator in expects a list or comma-separated string")
    if not parts:
        raise ValueError("operator in requires a value")
    return parts


def _intersect(groups: list[set[str]]) -> list[str]:
    if not groups:
        return []
    common = set(groups[0])
    for group in groups[1:]:
        common &= group
    if not common:
        # 空の一致を「フィルタなし」に広げない。
        return ["__db2rag_no_match__"]
    return sorted(common)


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("expected a single value")
        return str(value[0]).strip()
    return str(value).strip()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _unit_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return round(score, 4)
