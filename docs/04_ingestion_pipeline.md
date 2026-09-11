# 04 取り込みパイプライン

| 項目 | 内容 |
|---|---|
| **目的** | discover から activate までの DAG、checkpoint、idempotent 再実行を定義する |
| **対象読者** | 実装者 |
| **この文書が決めること** | ステージ順序、入出力、重複排除、verify 条件 |
| **この文書が決めないこと** | UI 進捗表示式、評価 Gate |

---

## 1. パイプライン DAG

```
discover → snapshot → extract → normalize → chunk → embed → upsert → verify → activate
   │          │          │          │         │        │        │         │          │
   └──────────┴──────────┴──────────┴─────────┴────────┴────────┴─────────┴──────────┘
                         checkpoint（JobItem 単位で永続化）
```

---

## 2. ステージ定義

| ステージ | 入力 | 出力 | Worker | Checkpoint |
|---|---|---|---|---|
| **discover** | source 設定 | object 一覧 | CPU | `discovered` |
| **snapshot** | object | 一貫スナップショット参照 | CPU / MySQL | `discovered` |
| **extract** | snapshot | raw text / structured rows | CPU | `extracted` |
| **normalize** | raw | CanonicalDocument | CPU | `normalized` |
| **chunk** | CanonicalDocument | parent + child chunks | CPU | `chunked` |
| **embed** | child texts | dense vectors + sparse | GPU | `embedded` |
| **upsert** | vectors + payload | Qdrant staging points | CPU | `upserted` |
| **verify** | staging collection | 検証レポート | CPU | `verified` |
| **activate** | verify OK | alias 切替 | API | job `completed` |

---

## 3. Discover

| source_type | 動作 |
|---|---|
| `mysql` | 設定テーブル/ビュー一覧 or SQL 定義の object 生成 |
| `file` | D&D / フォルダ再帰列挙、MIME 判定、除外（`.git`, `node_modules`, `*.tmp`） |

出力: `JobItem` per `SourceObject`

---

## 4. Snapshot

### MySQL
```sql
START TRANSACTION READ ONLY;
-- consistent snapshot (REPEATABLE READ)
-- unbuffered cursor で SELECT
```

### File
- ファイルの `stat` + SHA-256 を `source snapshot` テーブルに記録
- 取り込み中の変更は `file_changed` warning（当該 item retry 推奨）

---

## 5. Extract → Normalize

| 形式 | Extract | Normalize 出力 |
|---|---|---|
| PDF/DOCX/PPTX/HTML | Docling | CanonicalDocument（blocks, tables, headings） |
| XLSX/CSV | 専用 adapter | CanonicalDocument（sheet/row blocks） |
| TXT/MD | 構造 parser | CanonicalDocument（heading + paragraph） |
| MySQL row | template renderer | CanonicalDocument（1 record = 1 logical doc） |

**CanonicalDocument 最小構造**:
```json
{
  "title": "string",
  "language": "ja",
  "blocks": [
    { "type": "heading|paragraph|table|list|footnote", "level": 1, "text": "...", "page": 1 }
  ]
}
```

---

## 6. Chunk

- `05_chunking_strategy.md` に従い Parent-Child 生成
- SQLite に parent/child 永続化後 `checkpoint=chunked`

---

## 7. Embed

- GPU worker が lease 取得
- batch: 動的 token budget（`06_embedding_strategy.md`）
- 各 child: `検索文書: {text}` → dense 768 + BM25 sparse
- `checkpoint=embedded`

---

## 8. Upsert

- staging collection へ batch upsert（100 件）
- payload は `07_metadata_schema.md` 準拠
- 同一 `content_hash` の既存 point は upsert 上書き（idempotent）

---

## 9. Verify（activate 前 Gate）

| チェック | 条件 | 失敗時 |
|---|---|---|
| Point count | SQLite `child_count` == Qdrant count | activate 中止 |
| Null vector | dense/sparse 欠損 0 件 | activate 中止 |
| Sample search | 5 golden queries で top1 ヒット | warning（3/5 未満で中止） |
| Dimension | 全 point dense dim == 768 | activate 中止 |
| Schema | `schema_version == 1` | activate 中止 |

---

## 10. Activate

1. `alias` を staging → active に切替
2. SQLite `Rag.active_collection` 更新
3. staging collection 名を `archived` 登録
4. job `status=completed`

---

## 11. Idempotent 再取り込み

| シナリオ | 動作 |
|---|---|
| 同一 file_hash + content_hash | item `skipped`、既存 point 維持 |
| MySQL row_hash 変更 | 当該 record のみ re-chunk + re-embed + upsert |
| MySQL row 削除 | **自動削除しない**（明示 purge job が必要） |
| 全 reindex | 新 staging collection → alias 切替 |

---

## 12. ジョブモード

| mode | 説明 |
|---|---|
| `create` | 新規 RAG + 初回 collection |
| `append` | 既存 RAG に source 追加 |
| `reindex` | 同一 source 全件再構築（chunk/embedding 設定変更時） |

---

## 13. 関連文書

- `05_chunking_strategy.md`, `06_embedding_strategy.md`
- `09_mysql_connector.md`, `10_file_ingest.md`
- `16_error_handling.md`
