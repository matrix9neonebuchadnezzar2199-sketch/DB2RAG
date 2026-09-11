# 07 メタデータスキーマ

| 項目 | 内容 |
|---|---|
| **目的** | Qdrant payload と SQLite 共通 metadata の必須項目、型、正規化、索引を定義する |
| **対象読者** | 実装者、ローカル AI 連携開発者 |
| **この文書が決めること** | フィールド一覧、schema version、payload index、ACL、provenance |
| **この文書が決めないこと** | chunk 分割ロジック、検索スコア計算 |

---

## 1. Schema Version

| バージョン | 状態 | 変更内容 |
|---|---|---|
| `1` | **現行（初期）** | 本ドキュメントの全必須フィールド |

schema version 変更時は **新 physical collection + 全件 reindex**（alias 切替）。

---

## 2. 必須 Payload フィールド

| フィールド | 型 | Null | 説明 | Payload Index |
|---|---|---|---|---|
| `schema_version` | integer | ✗ | 常に `1` | ✓ keyword |
| `rag_id` | string | ✗ | 論理 RAG ID | ✓ keyword |
| `source_type` | string | ✗ | `mysql` / `file` | ✓ keyword |
| `source_name` | string | ✗ | 接続名 or ルートフォルダ名 | ✓ text |
| `object_id` | string | ✗ | SourceObject ID | ✓ keyword |
| `record_id` | string | ✓ | MySQL PK 合成 or ファイル相対パス | ✓ keyword |
| `path` | string | ✓ | ファイル絶対/相対パス（file source） | — |
| `title` | string | ✗ | 文書タイトル or テーブル論理名 | ✓ text |
| `section_path` | string | ✓ | 見出し階層 `>` 区切り | ✓ text |
| `page_from` | integer | ✓ | PDF 開始ページ（1-based） | ✓ integer |
| `page_to` | integer | ✓ | PDF 終了ページ | ✓ integer |
| `parent_chunk_id` | string | ✗ | 親 chunk 参照 | ✓ keyword |
| `child_chunk_id` | string | ✗ | 子 chunk ID（point と一致） | ✓ keyword |
| `parent_index` | integer | ✗ | 親順序 | — |
| `child_index` | integer | ✗ | 子順序 | — |
| `created_at` | string | ✗ | ISO8601 UTC | ✓ datetime |
| `updated_at` | string | ✓ | ソース側更新日時 | ✓ datetime |
| `content_hash` | string | ✗ | SHA-256(normalized child text) | ✓ keyword |
| `doc_content_hash` | string | ✗ | DocumentVersion hash | ✓ keyword |
| `hash` | string | ✗ | **後方互換 alias** = `content_hash` | — |
| `mime` | string | ✓ | MIME type | ✓ keyword |
| `language` | string | ✗ | 常に `ja`（初期） | ✓ keyword |
| `acl_tag` | string | ✗ | 権限タグ（下表） | ✓ keyword |
| `extra` | object | ✓ | 拡張 JSON（下表） | — |

---

## 3. Provenance / Pipeline フィールド（必須）

| フィールド | 型 | 説明 |
|---|---|---|
| `pipeline_fingerprint` | string | `sha256(parser_ver|chunk_ver|model_rev|prefix_ver)` |
| `embedding_model` | string | `cl-nagoya/ruri-v3-310m` |
| `embedding_revision` | string | `18b60fb8c2b9df296fb4212bb7d23ef94e579cd3` |
| `embedding_dim` | integer | `768` |
| `embedding_prefix_doc` | string | `検索文書: ` |
| `embedding_prefix_query` | string | `検索クエリ: `（検索時のみ使用、payload には doc 側記録） |
| `chunk_strategy` | string | `parent_child_v1` |
| `sparse_index` | string | `bm25_multilingual_v1` |

---

## 4. ACL タグ

| `acl_tag` | 意味 | 既定 |
|---|---|---|
| `public` | 全ユーザ検索可 | — |
| `internal` | 社内一般 | — |
| `dept:{code}` | 部署限定 | 例: `dept:HR` |
| `private` | 作成者のみ | **file 単体 D&D 既定** |
| `mysql:{role}` | DB 列マッピング由来 | 接続設定で指定 |

**Enforcement point**: FastAPI `/api/v1/search` が呼び出し元 `acl_tags[]` と intersection チェック。Qdrant filter に `acl_tag` を必ず付与（LAN モードでは必須）。

---

## 5. `extra` JSON 推奨キー

| キー | 型 | 用途 |
|---|---|---|
| `mysql_schema` | string | DB スキーマ名 |
| `mysql_table` | string | テーブル名 |
| `mysql_pk` | object | PK 列→値 |
| `sheet_name` | string | XLSX シート名 |
| `row_number` | integer | CSV/XLSX 行番号 |
| `ocr_applied` | boolean | OCR 使用フラグ |
| `join_template_id` | string | MySQL JOIN 定義 ID |
| `original_filename` | string | 元ファイル名 |

---

## 6. 正規化ルール

| 対象 | 規則 |
|---|---|
| テキスト hash 前処理 | NFKC 正規化、連続空白→単一スペース、`\r\n`→`\n`、trim |
| `title` | 空不可。取得不能時は `object_key` を使用 |
| `section_path` | 全角 `＞` → `>`、前後空白除去 |
| `record_id`（MySQL） | `{col1}={val1}|{col2}={val2}`（PK 列ソート固定） |
| `record_id`（file） | source root からの POSIX 相対パス |
| `language` | 検出不能時 `ja`（日本語前提プロダクト） |

---

## 7. Payload Index 作成順序

Qdrant collection 作成時、**upsert 前**に以下を作成:

1. `rag_id` — keyword
2. `acl_tag` — keyword
3. `source_type` — keyword
4. `object_id` — keyword
5. `content_hash` — keyword
6. `doc_content_hash` — keyword
7. `title` — text（tokenizer: multilingual）
8. `section_path` — text
9. `created_at` — datetime
10. `updated_at` — datetime

---

## 8. Deduplication キー

| Source | キー | 動作 |
|---|---|---|
| File | `file_hash` (SHA-256) + `content_hash` | 同一内容 skip |
| MySQL row | `{table}:{record_id}:{row_hash}` | 変更時のみ再 embed |
| MySQL 論理文書 | `{object_id}:{doc_content_hash}` | 版管理 |

`row_hash` = SHA-256(concat(normalized column values in template order))

---

## 9. 関連文書

- `03_data_model.md` — エンティティ
- `08_vector_db_design.md` — Qdrant 格納
- `15_security_and_ops.md` — ACL enforcement
