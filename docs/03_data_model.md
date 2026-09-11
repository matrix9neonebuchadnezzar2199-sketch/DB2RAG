# 03 データモデル

| 項目 | 内容 |
|---|---|
| **目的** | DB2RAG の永続エンティティ、ID 規則、状態遷移を定義する |
| **対象読者** | 実装者、DB 設計者 |
| **この文書が決めること** | SQLite スキーマ概念、ID 形式、状態機械、Qdrant point との対応 |
| **この文書が決めないこと** | SQL DDL 実装、ORM クラス名 |

---

## 1. エンティティ概要

```
Rag
 └── Source (mysql | file)
      └── SourceObject (table | view | sql | file_path)
           └── DocumentVersion (immutable, content_hash)
                └── ParentChunk
                     └── ChildChunk ──► Qdrant Point
Job
 └── JobItem (1 source object 単位)
Export
EvaluationRun
```

---

## 2. ID 規則

| エンティティ | ID 形式 | 例 |
|---|---|---|
| `rag_id` | `rag_{ulid}` | `rag_01HQXYZ...` |
| `source_id` | `src_{ulid}` | `src_01HQ...` |
| `object_id` | `obj_{ulid}` | `obj_01HQ...` |
| `doc_version_id` | `docv_{ulid}` | `docv_01HQ...` |
| `parent_chunk_id` | `par_{ulid}` | `par_01HQ...` |
| `child_chunk_id` | `chk_{ulid}` | `chk_01HQ...` |
| `job_id` | `job_{ulid}` | `job_01HQ...` |
| `job_item_id` | `item_{ulid}` | `item_01HQ...` |
| Qdrant point ID | UUID v5(`child_chunk_id`) | 決定的再生成可能 |

---

## 3. Rag

| フィールド | 型 | 説明 |
|---|---|---|
| `rag_id` | TEXT PK | 論理 RAG 識別子 |
| `display_name` | TEXT | UI 表示名 |
| `status` | ENUM | `draft` / `active` / `archived` / `purged` |
| `qdrant_alias` | TEXT | `db2rag_{rag_id}` |
| `active_collection` | TEXT | 現在の physical collection 名 |
| `staging_collection` | TEXT NULL | 変換中の staging |
| `schema_version` | INT | metadata schema 版（初期 `1`） |
| `created_at` | ISO8601 | |
| `updated_at` | ISO8601 | |

**状態遷移**: `draft` → `active`（初回 activate）→ `archived` → `purged`

---

## 4. Source

| フィールド | 型 | 説明 |
|---|---|---|
| `source_id` | TEXT PK | |
| `rag_id` | TEXT FK | |
| `source_type` | ENUM | `mysql` / `file` |
| `source_name` | TEXT | 接続名 or フォルダ名 |
| `config_json` | JSON | 接続情報参照（secret は別 vault） |
| `acl_tag_default` | TEXT | 既定 ACL |
| `enabled` | BOOL | 無効化可能 |
| `created_at` | ISO8601 | |

---

## 5. SourceObject

| フィールド | 型 | 説明 |
|---|---|---|
| `object_id` | TEXT PK | |
| `source_id` | TEXT FK | |
| `object_type` | ENUM | `table` / `view` / `sql` / `file` |
| `object_key` | TEXT | `schema.table` or ファイル相対パス |
| `latest_doc_version_id` | TEXT NULL | 最新成功版 |
| `last_ingested_at` | ISO8601 NULL | |

---

## 6. DocumentVersion（immutable）

| フィールド | 型 | 説明 |
|---|---|---|
| `doc_version_id` | TEXT PK | |
| `object_id` | TEXT FK | |
| `content_hash` | TEXT | SHA-256(normalized_text) |
| `file_hash` | TEXT NULL | ファイルの SHA-256（file source） |
| `row_hash` | TEXT NULL | MySQL 行ハッシュ（DB source） |
| `extracted_bytes` | INT | 抽出テキストサイズ |
| `parent_count` | INT | |
| `child_count` | INT | |
| `pipeline_fingerprint` | TEXT | parser+chunk+model の合成 hash |
| `created_at` | ISO8601 | |

**重複排除**: 同一 `object_id` + 同一 `content_hash` → skip（idempotent）

---

## 7. ParentChunk / ChildChunk

### ParentChunk

| フィールド | 型 | 説明 |
|---|---|---|
| `parent_chunk_id` | TEXT PK | |
| `doc_version_id` | TEXT FK | |
| `parent_index` | INT | 文書内順序 |
| `text` | TEXT | 親テキスト全文 |
| `section_path` | TEXT | 例: `第3章>第2節>第1款` |
| `page_from` | INT NULL | |
| `page_to` | INT NULL | |
| `token_estimate` | INT | Ruri tokenizer 推定 |

### ChildChunk

| フィールド | 型 | 説明 |
|---|---|---|
| `child_chunk_id` | TEXT PK | |
| `parent_chunk_id` | TEXT FK | |
| `child_index` | INT | 親内順序 |
| `text` | TEXT | 検索用テキスト（prefix 除く） |
| `char_count` | INT | |
| `qdrant_point_id` | TEXT | UUID v5 |
| `embedded_at` | ISO8601 NULL | |

---

## 8. Job / JobItem

### Job

| フィールド | 型 | 説明 |
|---|---|---|
| `job_id` | TEXT PK | |
| `rag_id` | TEXT FK | |
| `mode` | ENUM | `create` / `append` / `reindex` |
| `status` | ENUM | 下表 |
| `progress_pct` | REAL | 0.0〜100.0 |
| `work_units_total` | REAL | 重み付き総量 |
| `work_units_done` | REAL | |
| `eta_seconds` | INT NULL | |
| `current_item_name` | TEXT NULL | |
| `lease_owner` | TEXT NULL | worker ID |
| `lease_expires_at` | ISO8601 NULL | |
| `created_by` | TEXT | OS ユーザ |
| `created_at` | ISO8601 | |
| `finished_at` | ISO8601 NULL | |

**Job status**: `pending` → `running` → `verifying` → `completed` | `failed` | `cancelled` | `paused`

### JobItem

| フィールド | 型 | 説明 |
|---|---|---|
| `job_item_id` | TEXT PK | |
| `job_id` | TEXT FK | |
| `object_id` | TEXT FK | |
| `status` | ENUM | `pending` / `running` / `completed` / `failed` / `skipped` |
| `checkpoint` | ENUM | 下表 |
| `error_code` | TEXT NULL | |
| `error_message` | TEXT NULL | マスク済み |
| `retry_count` | INT | 最大 5 |

**Checkpoint**: `discovered` → `extracted` → `normalized` → `chunked` → `embedded` → `upserted` → `verified`

---

## 9. Export

| フィールド | 型 | 説明 |
|---|---|---|
| `export_id` | TEXT PK | |
| `rag_id` | TEXT FK | |
| `export_path` | TEXT | `data/exports/{export_id}/` |
| `manifest_sha256` | TEXT | |
| `status` | ENUM | `running` / `completed` / `failed` |
| `created_at` | ISO8601 | |

---

## 10. EvaluationRun

| フィールド | 型 | 説明 |
|---|---|---|
| `eval_run_id` | TEXT PK | |
| `rag_id` | TEXT FK | |
| `question_set_version` | TEXT | `eval_v1_120` |
| `metrics_json` | JSON | Recall@5, MRR, etc. |
| `gate_passed` | BOOL | |
| `created_at` | ISO8601 | |

---

## 11. Qdrant との対応

| SQLite | Qdrant |
|---|---|
| `child_chunk_id` | point id (UUID v5) |
| `ChildChunk.text` + metadata | payload |
| embedding vector | named vector `dense` |
| BM25 tokens | named vector `sparse` |
| `rag_id` | payload + collection alias |

1 child chunk = 1 Qdrant point（dense + sparse 同一 point）

---

## 12. 関連文書

- `07_metadata_schema.md` — payload 詳細
- `08_vector_db_design.md` — collection / alias
- `04_ingestion_pipeline.md` — checkpoint 処理
