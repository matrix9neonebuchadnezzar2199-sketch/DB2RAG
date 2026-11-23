"""Phase 3 のパーサとチャンク。ネットワークは使わない。"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from db2rag.chunking import (
    ChunkParams,
    _overlap_tail,
    _split_child,
    chunk_document,
    infer_doc_kind,
)
from db2rag.constants import MAX_CHILD_CHARS
from db2rag.parsers import parse_file
from db2rag.parsers.canonical import Block, CanonicalDocument
from db2rag.parsers.csvxlsx import MERGED_CELL_MAX_BYTES, parse_csv, parse_xlsx
from db2rag.parsers.office import parse_html, parse_pdf
from db2rag.parsers.txtmd import parse_md, parse_txt


def test_shift_jis_csv_and_txt(tmp_path: Path) -> None:
    csv_path = tmp_path / "people.csv"
    csv_path.write_bytes("名前,値\n,\n山田,総務\n".encode("shift_jis"))
    csv_doc = parse_csv(csv_path)
    csv_text = csv_doc.plain_text()
    assert "山田" in csv_text
    assert "総務" in csv_text
    assert "行1: 名前=山田, 値=総務" in csv_text
    assert "名前=, 値=" not in csv_text

    txt_path = tmp_path / "note.txt"
    txt_path.write_bytes("在宅勤務は週2日まで。".encode("shift_jis"))
    assert parse_txt(txt_path).plain_text() == "在宅勤務は週2日まで。"


def test_markdown_table_and_code_preserved(tmp_path: Path) -> None:
    md = tmp_path / "note.md"
    md.write_text(
        "\n".join(
            [
                "# 見出し",
                "",
                "| 名前 | 値 |",
                "| --- | --- |",
                "| 通勤手当 | 15000 |",
                "",
                "```python",
                "print('keep')",
                "```",
                "",
                "```",
                "plain_code()",
                "```",
                "",
                "    indented_code()",
                "",
                "- 一番目",
                "- 二番目",
                "",
            ]
        ),
        encoding="utf-8",
    )
    doc = parse_md(md)
    tables = [block for block in doc.blocks if block.type == "table"]
    codes = [block for block in doc.blocks if block.type == "code"]
    assert tables
    assert "【表: 見出し】" in tables[0].text
    assert "通勤手当" in tables[0].text
    assert "15000" in tables[0].text
    assert any(block.text.startswith("[python]\n") and "print('keep')" in block.text for block in codes)
    assert any(block.text.strip() == "plain_code()" for block in codes)
    assert any("indented_code()" in block.text for block in codes)
    assert any(block.type == "list" and "一番目" in block.text for block in doc.blocks)
    assert any(block.type == "heading" for block in doc.blocks)


def test_xlsx_merged_cells_and_blank_rows(tmp_path: Path) -> None:
    path = tmp_path / "merged.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "手当"
    sheet["A1"] = "項目"
    sheet["B1"] = "金額"
    sheet.merge_cells("A2:A3")
    sheet["A2"] = "通勤"
    sheet["B2"] = 100
    sheet["B3"] = 200
    sheet["A4"] = " "
    sheet["B4"] = None
    sheet["A5"] = "住宅"
    sheet["B5"] = 300
    empty = workbook.create_sheet("空")
    empty["A1"] = "見出しだけ"
    workbook.save(path)
    workbook.close()

    text = parse_xlsx(path).plain_text()
    assert "行1: 項目=通勤, 金額=100" in text
    assert "行2: 項目=通勤, 金額=200" in text
    assert "行3: 項目=住宅, 金額=300" in text
    assert text.count("通勤") == 2
    assert "項目=, 金額=" not in text
    assert "シート: 空" not in text
    assert "見出しだけ" not in text


def test_xlsx_over_size_limit_skips_merged_cells(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "large.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet["A1"] = "項目"
    sheet["B1"] = "金額"
    sheet.merge_cells("A2:A3")
    sheet["A2"] = "通勤"
    sheet["B2"] = 100
    sheet["B3"] = 200
    workbook.save(path)
    workbook.close()

    class _Stat:
        st_size = MERGED_CELL_MAX_BYTES + 1

    monkeypatch.setattr(Path, "stat", lambda _self: _Stat())
    with caplog.at_level("WARNING"):
        text = parse_xlsx(path).plain_text()
    assert "行1: 項目=通勤, 金額=100" in text
    assert "行2: 項目=, 金額=200" in text
    assert text.count("通勤") == 1
    assert "結合セルは展開しません" in caplog.text


def test_html_with_urls_and_table(tmp_path: Path) -> None:
    path = tmp_path / "local.html"
    path.write_text(
        "\n".join(
            [
                "<!DOCTYPE html>",
                "<html><body>",
                "<p>手順は https://example.com を参照。</p>",
                "<table>",
                "<tr><th>項目</th><th>値</th></tr>",
                "<tr><td><p>通勤手当</p></td><td>15000</td></tr>",
                "</table>",
                "<ul><li>表の外の項目</li></ul>",
                "</body></html>",
            ]
        ),
        encoding="utf-8",
    )
    doc = parse_html(path)
    text = doc.plain_text()
    assert "https://example.com" in text
    assert any(block.type == "paragraph" and "https://example.com" in block.text for block in doc.blocks)
    assert any(block.type == "table" and "通勤手当" in block.text for block in doc.blocks)
    assert not any(block.type == "paragraph" and "通勤手当" in block.text for block in doc.blocks)
    assert text.count("通勤手当") == 1
    assert text.count("15000") == 1
    assert any(block.type == "paragraph" and "表の外の項目" in block.text for block in doc.blocks)
    with pytest.raises(ValueError, match="ERR_FILE_REMOTE_HTML"):
        parse_html(Path("https://example.com/policy.html"))
    with pytest.raises(ValueError, match="ERR_FILE_REMOTE_HTML"):
        parse_html(Path("http://example.com/policy.html"))


def test_pdf_heading_detection(tmp_path: Path) -> None:
    path = tmp_path / "rules.pdf"
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    drawing = canvas.Canvas(str(path))
    drawing.setFont("HeiseiKakuGo-W5", 12)
    y = 800
    for line in (
        "第1章 総則",
        "この規程は社内に適用する。",
        "第2節 定義",
        "用語は次のとおりとする。",
        "第2条 適用範囲",
        "全従業員に適用する。",
        "1.1 細目です",
        "## 付則",
    ):
        drawing.drawString(72, y, line)
        y -= 24
    drawing.save()

    doc = parse_pdf(path)
    headings = [block for block in doc.blocks if block.type == "heading"]
    assert any(block.text == "第1章 総則" and block.level == 1 for block in headings)
    assert any(block.text == "第2節 定義" and block.level == 2 for block in headings)
    assert any(block.text == "第2条 適用範囲" and block.level == 3 for block in headings)
    assert any(block.text == "1.1 細目です" and block.level == 2 for block in headings)
    assert any(block.text == "## 付則" and block.level == 2 for block in headings)
    assert any(block.type == "paragraph" and "社内に適用" in block.text for block in doc.blocks)
    paths = [parent.section_path for parent in chunk_document(doc).parents]
    assert any("第1章 総則" in section for section in paths)
    assert any("第2条 適用範囲" in section for section in paths)


def test_macro_office_rejected(tmp_path: Path) -> None:
    for suffix in (".docm", ".xlsm", ".pptm"):
        path = tmp_path / f"macro{suffix}"
        path.write_bytes(b"not-a-real-office-file")
        with pytest.raises(ValueError, match=f"ERR_FILE_MACRO:\\{suffix}"):
            parse_file(path)


def test_chunk_overlap_does_not_truncate_body() -> None:
    assert _overlap_tail("AAAA。BBBB。CCCC", 9) == "CCCC"
    assert _overlap_tail("ABC\nDEFGHIJ", 8) == "DEFGHIJ"
    assert _overlap_tail("ABCDEFGH", 3) == "FGH"
    assert _overlap_tail("ABCDEFG。", 4) == "EFG。"

    marker = "末尾マーカー"
    body = ("後段の本文です。" * 120)[: 860 - len(marker)] + marker
    prev = "前段の文章です。" * 6
    assert len(body) + 80 > MAX_CHILD_CHARS
    assert len(body) <= MAX_CHILD_CHARS
    params = ChunkParams(
        child_target=200,
        child_min=40,
        child_max=900,
        overlap=80,
        parent_max=20000,
    )
    chunks = _split_child(f"{prev}\n\n{body}", params)
    assert len(chunks) >= 2
    assert chunks[-1].endswith(body)
    assert len(chunks[-1]) > len(body)
    assert len(chunks[-1]) <= MAX_CHILD_CHARS


def test_infer_doc_kind_mysql_priority() -> None:
    assert infer_doc_kind("text/plain", {"source_type": "mysql"}) == "mysql"
    assert infer_doc_kind("text/csv", {"source_type": "mysql"}) == "mysql"
    assert infer_doc_kind("text/plain", None) == "txtmd"
    assert infer_doc_kind("text/csv", {}) == "table"


def test_later_children_receive_section_label() -> None:
    sentence = "これは後続チャンクへ節名を残すための文章です。"
    doc = CanonicalDocument(
        title="規程",
        language="ja",
        mime="text/markdown",
        blocks=[
            Block(type="heading", text="第1章 総則", level=1),
            Block(type="paragraph", text=sentence * 40),
        ],
    )
    result = chunk_document(doc, kind="txtmd")
    later = [child for child in result.children if child.child_index > 0]
    assert later
    label = "【規程>第1章 総則】\n"
    assert all(child.text.startswith(label) for child in later)
    assert not result.children[0].text.startswith("【")
    assert all(len(child.text) <= MAX_CHILD_CHARS for child in result.children)
    assert sentence in result.children[-1].text
