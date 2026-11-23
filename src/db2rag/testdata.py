"""テスト用 SQL / PDF / Excel / CSV / DOCX / MD / TXT を生成する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from db2rag.config import repo_root

LEAVE_BODY = (
    "第5章 休暇\n"
    "第2節 年次有給休暇\n"
    "第12条 年次有給休暇の申請は、取得希望日の10日前までに所属長へ届け出なければならない。\n"
    "第13条 年次有給休暇は入社6ヶ月経過後に10日付与する。\n"
)

CHILDCARE = "育児休業は、子が1歳に達するまで取得できる。延長が認められる場合は最長2歳までとする。"
REMOTE = "在宅勤務は原則として週2日までとする。事前申請は所属長の承認を要する。"


def testdata_root() -> Path:
    return repo_root() / "testdata"


def generate_all(root: Path | None = None) -> dict[str, str]:
    root = root or testdata_root()
    files = root / "files"
    mysql = root / "mysql"
    files.mkdir(parents=True, exist_ok=True)
    mysql.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    written["sql_init"] = str(_write_sql(mysql / "01_init.sql", include_user=True))
    written["sql_seed"] = str(_write_sql(mysql / "db2rag_lab_seed.sql", include_user=False))
    written["md"] = str(_write_text(files / "hr_leave_policy.md", "# 就業規則\n\n## 第5章 休暇\n\n" + LEAVE_BODY))
    written["txt"] = str(_write_text(files / "faq_remote.txt", f"# FAQ\n\nQ. 在宅勤務は何日までですか。\nA. {REMOTE}\n"))
    written["csv"] = str(_write_csv(files / "qualifications.csv"))
    written["xlsx"] = str(_write_xlsx(files / "allowances.xlsx"))
    written["pdf"] = str(_write_pdf(files / "hr_leave_policy.pdf"))
    written["docx"] = str(_write_docx(files / "childcare.docx"))
    written["html"] = str(_write_text(files / "handbook.html", "<html><body><h1>ハンドブック</h1><p>機密情報は社外へ持ち出してはならない。</p></body></html>"))
    return written


def _write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def _sql_rows(rows: list[tuple[Any, ...]]) -> str:
    rendered = []
    for row in rows:
        parts = []
        for cell in row:
            if isinstance(cell, int):
                parts.append(str(cell))
            else:
                parts.append(f"'{_esc(str(cell))}'")
        rendered.append("(" + ", ".join(parts) + ")")
    return ",\n".join(rendered)


def _write_sql(path: Path, *, include_user: bool) -> Path:
    # 検索確認用の固有事実。ダミー行は入れない（BM25 がノイズに埋もれるため）。
    regulations = [
        (1, "HR-001", "年次有給休暇", LEAVE_BODY.replace("\n", " "), "internal"),
        (2, "HR-002", "育児休業", CHILDCARE, "internal"),
        (3, "HR-003", "介護休業", "介護休業は対象家族1人につき通算93日まで、3回を上限として分割取得できる。", "internal"),
        (4, "HR-004", "在宅勤務", REMOTE, "internal"),
        (5, "HR-005", "フレックスタイム", "フレックスタイムのコアタイムは10時から15時とする。清算期間は1ヶ月とする。", "internal"),
        (6, "HR-006", "試用期間", "試用期間は入社日から3ヶ月とする。延長する場合は最大2ヶ月までとする。", "internal"),
        (7, "HR-007", "退職手続", "自己都合退職は、退職希望日の30日前までに退職届を所属長へ提出しなければならない。", "internal"),
        (8, "HR-008", "慶弔休暇", "忌引休暇は、配偶者の場合は10日、父母の場合は7日、兄弟姉妹の場合は3日とする。", "internal"),
        (9, "HR-009", "深夜勤務手当", "22時から翌5時までの勤務には、通常賃金の25パーセントを深夜手当として支給する。", "internal"),
        (10, "HR-010", "時間外勤務", "法定時間外労働の割増率は、月60時間以下が25パーセント、60時間超が50パーセントとする。", "internal"),
        (11, "HR-011", "通勤手当", "通勤手当の上限は月額15000円とし、支給日は毎月25日とする。定期券実費との差額は精算する。", "internal"),
        (12, "HR-012", "資格手当", "TOEIC 800点以上の資格手当は月額8000円とする。日商簿記2級は月額5000円とする。", "internal"),
        (13, "HR-013", "住宅手当", "賃貸居住者の住宅手当は月額20000円とする。持ち家には支給しない。", "internal"),
        (14, "HR-014", "安全衛生", "定期健康診断は毎年4月に実施する。35歳以上は人間ドックを選択できる。", "internal"),
        (15, "HR-015", "機密情報", "機密情報は社外へ持ち出してはならない。私用クラウドへの業務ファイル保存は禁止する。", "internal"),
        (16, "HR-016", "出張旅費", "国内出張の日当は宿泊を伴う場合5000円、日帰りは2000円とする。新幹線は普通車指定席を標準とする。", "internal"),
        (17, "HR-017", "育児短時間", "3歳未満の子を養育する従業員は、1日6時間の短時間勤務を請求できる。", "internal"),
        (18, "HR-018", "有給繰越", "年次有給休暇の繰越は翌年度限りとし、未消化分は入社日から2年で時効消滅する。", "internal"),
        (19, "HR-019", "特別休暇", "結婚休暇は連続5日付与する。配偶者の出産時は2日の配偶者出産休暇を付与する。", "internal"),
        (20, "HR-020", "災害時出勤", "警報発令時は出社を強制しない。安否確認メールへの返信は始業時刻までとする。", "internal"),
    ]
    faqs = [
        (1, "在宅勤務は何日までですか", REMOTE),
        (2, "通勤手当の支給日はいつですか", "通勤手当は毎月25日に支給する。上限は月額15000円。"),
        (3, "有給休暇は何日前までに申請しますか", "取得希望日の10日前までに所属長へ届け出る。"),
        (4, "育児休業はいつまで取れますか", CHILDCARE),
        (5, "コアタイムは何時から何時ですか", "コアタイムは10時から15時です。"),
        (6, "退職届は何日前に出しますか", "退職希望日の30日前までに提出する。"),
        (7, "TOEICの資格手当はいくらですか", "TOEIC 800点以上で月額8000円。"),
        (8, "深夜手当の割増は何パーセントですか", "22時から翌5時は25パーセント。"),
        (9, "忌引は配偶者の場合何日ですか", "配偶者の忌引休暇は10日。"),
        (10, "健康診断はいつ実施しますか", "定期健康診断は毎年4月に実施する。"),
    ]
    allowances = [
        (1, "通勤手当", 15000, 25, "定期券実費との差額調整あり"),
        (2, "住宅手当", 20000, 25, "賃貸のみ"),
        (3, "資格手当TOEIC", 8000, 25, "TOEIC 800点以上"),
        (4, "資格手当簿記", 5000, 25, "日商簿記2級"),
        (5, "役職手当主任", 30000, 25, "主任以上"),
        (6, "家族手当", 8000, 25, "扶養配偶者"),
        (7, "単身赴任手当", 40000, 25, "会社都合の転勤"),
        (8, "在宅勤務手当", 3000, 25, "在宅勤務実施月"),
    ]
    user_sql = ""
    if include_user:
        user_sql = """
CREATE USER IF NOT EXISTS 'db2rag_ro'@'%' IDENTIFIED BY 'db2rag_ro';
GRANT SELECT ON hr.* TO 'db2rag_ro'@'%';
FLUSH PRIVILEGES;
"""
    sql = f"""SET NAMES utf8mb4;
-- DB2RAG 検索試験用シード（lab）。実在の個人情報は含めない。
-- 確認クエリ例:
--   有給休暇の申請期限 → 10日前
--   育児休業はいつまで → 1歳
--   通勤手当 → 15000 / 25日
--   コアタイム → 10時から15時
--   退職届 → 30日前
--   TOEIC → 8000
--   忌引 配偶者 → 10日
--   深夜手当 → 25パーセント

CREATE DATABASE IF NOT EXISTS hr CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE hr;

DROP TABLE IF EXISTS allowances;
DROP TABLE IF EXISTS faq_entries;
DROP TABLE IF EXISTS regulations;

CREATE TABLE regulations (
  id INT PRIMARY KEY,
  code VARCHAR(32) NOT NULL,
  title VARCHAR(128) NOT NULL,
  body TEXT NOT NULL,
  acl_tag VARCHAR(64) NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE faq_entries (
  faq_id INT PRIMARY KEY,
  question VARCHAR(255) NOT NULL,
  answer TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE allowances (
  allowance_id INT PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  monthly_amount INT NOT NULL,
  payday INT NOT NULL,
  note VARCHAR(255) NOT NULL
);

INSERT INTO regulations (id, code, title, body, acl_tag) VALUES
{_sql_rows(regulations)};

INSERT INTO faq_entries (faq_id, question, answer) VALUES
{_sql_rows(faqs)};

INSERT INTO allowances (allowance_id, name, monthly_amount, payday, note) VALUES
{_sql_rows(allowances)};
{user_sql}"""
    path.write_text(sql, encoding="utf-8")
    return path


def _write_csv(path: Path) -> Path:
    lines = ["資格,条件,月額"]
    lines.append("TOEIC,800点以上,8000")
    lines.append("日商簿記,2級,5000")
    for i in range(3, 40):
        lines.append(f"資格{i},条件{i},{1000 + i}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_xlsx(path: Path) -> Path:
    wb = Workbook()
    sheet = wb.active
    sheet.title = "手当"
    sheet.append(["手当名", "月額", "支給日", "備考"])
    sheet.append(["通勤手当", 15000, 25, "定期券実費との差額調整あり"])
    sheet.append(["住宅手当", 20000, 25, "賃貸のみ"])
    for i in range(3, 30):
        sheet.append([f"その他手当{i}", 1000 + i, 25, "通常支給"])
    wb.save(path)
    return path


def _write_pdf(path: Path) -> Path:
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    c = canvas.Canvas(str(path), pagesize=A4)
    text = c.beginText(50, 800)
    text.setFont("HeiseiKakuGo-W5", 12)
    for line in ["就業規則（抜粋）", "", *LEAVE_BODY.split("\n"), "", CHILDCARE]:
        text.textLine(line)
    c.drawText(text)
    c.save()
    return path


def _write_docx(path: Path) -> Path:
    document = Document()
    document.add_heading("育児・介護休業規程", level=1)
    document.add_heading("育児休業", level=2)
    document.add_paragraph(CHILDCARE)
    document.save(str(path))
    return path
