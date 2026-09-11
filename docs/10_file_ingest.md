# 10 ファイル取り込み

| 項目 | 内容 |
|---|---|
| **目的** | D&D、フォルダ列挙、形式別パース、OCR 条件を定義する |
| **対象読者** | 実装者 |
| **この文書が決めること** | 対応形式、parser 選択、拒否パターン、文字コード |
| **この文書が決めないこと** | chunk 数値、進捗 UI |

---

## 1. 取り込み経路

| 経路 | 動作 |
|---|---|
| ファイル D&D | 1+ ファイル → `SourceObject` 生成 |
| フォルダ D&D | 再帰列挙（深さ max 20） |
| パス指定 | API で絶対/相対パス（allowlist ディレクトリ内） |

---

## 2. 列挙規則

| 規則 | 値 |
|---|---|
| symlink | **拒否**（`ERR_FILE_SYMLINK`） |
| 隠しファイル | `.` 始まりはスキップ（`.gitkeep` 除く設定可） |
| 除外ディレクトリ | `.git`, `node_modules`, `__pycache__`, `$RECYCLE.BIN` |
| 除外拡張子 | `.exe`, `.dll`, `.bin`, `.zip`, `.7z`（初期） |
| 最大ファイルサイズ | 500 MB / ファイル |
| 最大ファイル数 / job | 10,000 |

---

## 3. MIME 判定

1. 拡張子
2. `python-magic` による content sniff
3. 不一致時 warning + 拡張子優先

| 拡張子 | 期待 MIME |
|---|---|
| `.pdf` | `application/pdf` |
| `.docx` | `application/vnd...wordprocessingml.document` |
| `.xlsx` | `application/vnd...spreadsheetml.sheet` |
| `.pptx` | `application/vnd...presentationml.presentation` |
| `.csv` | `text/csv` |
| `.txt` | `text/plain` |
| `.md` | `text/markdown` |
| `.html` | `text/html` |

---

## 4. Parser 割当

| 形式 | Parser | 出力 |
|---|---|---|
| PDF | Docling | CanonicalDocument |
| DOCX | Docling | CanonicalDocument |
| PPTX | Docling | CanonicalDocument |
| HTML | Docling（remote 拒否） | CanonicalDocument |
| XLSX | 専用 adapter（openpyxl） | sheet/row blocks |
| CSV | 専用 adapter | row blocks |
| TXT | 構造 parser | paragraph blocks |
| MD | markdown-it-py + 構造 parser | heading blocks |

---

## 5. PDF 処理

| 段階 | 動作 |
|---|---|
| 1. テキスト層抽出 | Docling 標準 |
| 2. 品質判定 | ページの extractable 文字数 < 50 → 低品質 |
| 3. OCR | 低品質ページのみ RapidOCR（日本語 model） |
| 4. 全ページ低品質 | item `failed` + `ERR_PDF_NO_TEXT` |

**OCR 発火条件（T-FILE-020）**:
- ページ文字数 < 50 **かつ** 画像面積比 > 30%

---

## 6. XLSX / CSV

| 規則 | 説明 |
|---|---|
| シート単位 | 各シートを独立 object 群として処理可 |
| 行単位 | 1 行 = 1 論理文書（ヘッダ行を列名に） |
| 結合セル | 左上値を展開 |
| 空行 | スキップ |
| 文字コード（CSV） | UTF-8 優先 → Shift_JIS フォールバック（chardet） |

---

## 7. HTML 制限

| 許可 | 拒否 |
|---|---|
| ローカルファイル | `http://` / `https://` remote fetch |
| 静的 HTML | JavaScript 実行 |

remote HTML は `ERR_FILE_REMOTE_HTML`（permanent）。

---

## 8. マクロ・埋込オブジェクト

| 形式 | 動作 |
|---|---|
| DOCX マクロ (.docm) | 拒否 |
| XLSM | 拒否 |
| PPT マクロ | 拒否 |
| PDF JavaScript | 無視（テキスト抽出のみ） |

---

## 9. Source Snapshot

| フィールド | 説明 |
|---|---|
| `file_path` | 取り込み時点のパス |
| `file_size` | bytes |
| `file_hash` | SHA-256 |
| `mtime` | 修正日時 |

取り込み中に `mtime` 変更 → `file_changed` warning

---

## 10. 文字コード

| 形式 | 既定 |
|---|---|
| TXT | UTF-8 → chardet → cp932 |
| MD | UTF-8 |
| CSV | UTF-8 → cp932 |
| Office |内部 UTF-8 |

---

## 11. エラーコード

| コード | 意味 | Retry |
|---|---|---|
| `ERR_FILE_NOT_FOUND` | ファイル消失 | ✗ |
| `ERR_FILE_SYMLINK` | symlink | ✗ |
| `ERR_FILE_TOO_LARGE` | サイズ超過 | ✗ |
| `ERR_FILE_PARSE` | パース失敗 | ✓（1回） |
| `ERR_PDF_NO_TEXT` | テキスト抽出不能 | ✗ |
| `ERR_FILE_REMOTE_HTML` | remote HTML | ✗ |
| `ERR_FILE_MACRO` | マクロ付き Office | ✗ |

---

## 12. 関連文書

- `04_ingestion_pipeline.md`
- `05_chunking_strategy.md`
- `16_error_handling.md`
