"""単位試験。外部 Qdrant / Ruri は TEST.py 側。"""

from __future__ import annotations

from pathlib import Path

from db2rag.chunking import chunk_document
from db2rag.ids import new_id, point_id_for_child
from db2rag.parsers.canonical import Block, CanonicalDocument
from db2rag.parsers.csvxlsx import parse_csv
from db2rag.parsers.txtmd import parse_md, parse_txt
from db2rag.sparse import tokenize, to_sparse
from db2rag.textnorm import highlight_html, normalize_text


def test_ids_stable_point() -> None:
    child = "chk_01TEST"
    assert point_id_for_child(child) == point_id_for_child(child)
    assert new_id("rag").startswith("rag_")


def test_normalize() -> None:
    assert normalize_text("Ａ  Ｂ\r\n") == "A B"


def test_highlight_escapes_html() -> None:
    out = highlight_html("<script>alert(1)</script> 申請期限", "申請期限")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<em>申請期限</em>" in out


def test_chunk_parent_child() -> None:
    doc = CanonicalDocument(
        title="規程",
        language="ja",
        mime="text/markdown",
        blocks=[
            Block(type="heading", text="第5章 休暇", level=1),
            Block(type="paragraph", text="年次有給休暇の申請は、取得希望日の10日前までに所属長へ届け出なければならない。" * 3),
        ],
    )
    result = chunk_document(doc, kind="regulation")
    assert result.parents
    assert result.children
    assert all(child.char_count <= 900 for child in result.children)


def test_sparse_japanese() -> None:
    tokens = tokenize("有給休暇の申請期限")
    assert any("有給" in t or t == "有給" for t in tokens)
    sparse = to_sparse("有給休暇の申請期限")
    assert sparse.indices
    assert len(sparse.indices) == len(set(sparse.indices))


def test_parse_md_and_csv(tmp_path: Path) -> None:
    md = tmp_path / "a.md"
    md.write_text("# 見出し\n\n本文です。", encoding="utf-8")
    csv = tmp_path / "a.csv"
    csv.write_text("名前,値\n通勤手当,15000\n", encoding="utf-8")
    md_doc = parse_md(md)
    csv_doc = parse_csv(csv)
    assert any(b.type == "heading" for b in md_doc.blocks)
    assert "通勤手当" in csv_doc.plain_text()
    txt = tmp_path / "a.txt"
    txt.write_text("在宅勤務は週2日まで。", encoding="utf-8")
    assert "在宅勤務" in parse_txt(txt).plain_text()
