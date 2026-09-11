# 14 ローカル AI 連携

| 項目 | 内容 |
|---|---|
| **目的** | AISSS 等ローカル AI との連携方式、export 形式、互換性境界を定義する |
| **対象読者** | AISSS 開発者、DB2RAG 実装者 |
| **この文書が決めること** | 検索 API、export manifest、Qdrant read-only 条件、非互換点 |
| **この文書が決めないこと** | AISSS 内部改修、OpenAI embeddings 互換 |

---

## 1. 連携パターン

| 優先度 | パターン | 説明 | 推奨度 |
|---|---|---|---|
| P1 | **DB2RAG 検索 API** | HTTP POST `/api/v1/search` | ★★★ 推奨 |
| P2 | **Export 再 ingest** | JSONL + manifest → AISSS ingestion | ★★☆ 可搬 |
| P3 | **Read-only Qdrant** | AISSS が Qdrant を直接検索 | ★☆☆ 条件付き |
| — | OpenAI embeddings API | **提供しない** | ✗ |
| — | AISSS collection 直書き | **行わない** | ✗ |

---

## 2. AISSS 非互換点（明示）

| 項目 | DB2RAG | AISSS 現行 | 影響 |
|---|---|---|---|
| Embedding モデル | Ruri-v3-310m (768) | Ollama 任意モデル | ベクトル空間非互換 |
| Collection 管理 | per-rag alias | single active collection + blue-green | 直接共有不可 |
| 権限モデル | `acl_tag` payload filter | PostgreSQL `viewing_range_ids` 再認可 | 二重設計必要 |
| Chunk 方式 | Parent-Child + 文書種別 | 固定 800 token / 2000 char | 構造差異 |
| Hybrid | dense + BM25 RRF | dense + reranker | 検索パイプライン差異 |

**結論**: Qdrant snapshot を AISSS にそのまま接続するのは **embedding モデル一致時のみ**有効。既定はテキスト + metadata の再 ingest または DB2RAG API 経由。

---

## 3. 検索 API（Vendor-Neutral）

### エンドポイント

```
POST http://127.0.0.1:8781/api/v1/search
Authorization: Bearer <api_key>  # LAN モード時
Content-Type: application/json
```

### リクエスト / レスポンス

`13_search_preview.md` と同一。外部 AI は snippet + metadata をコンテキストとして利用。

### AISSS 統合案（将来）

```
AISSS permissioned-search
  → external_retrieval_provider: db2rag
  → POST /api/v1/search (query, acl_tags from session)
  → 結果を citation として LLM に渡す
```

詳細は `20_open_questions.md` OQ-001。

---

## 4. Export 形式

### ディレクトリ構造

```
data/exports/{export_id}/
├── manifest.json
├── documents.jsonl.zst
├── chunks.jsonl.zst
├── qdrant.snapshot          # optional
└── checksums.sha256
```

### manifest.json

```json
{
  "export_version": "1",
  "export_id": "exp_01HQ...",
  "rag_id": "rag_01HQ...",
  "created_at": "2026-09-10T12:00:00Z",
  "embedding_model": "cl-nagoya/ruri-v3-310m",
  "embedding_revision": "18b60fb8c2b9df296fb4212bb7d23ef94e579cd3",
  "embedding_dim": 768,
  "prefix_doc": "検索文書: ",
  "prefix_query": "検索クエリ: ",
  "schema_version": 1,
  "chunk_strategy": "parent_child_v1",
  "sparse_index": "bm25_multilingual_v1",
  "pipeline_fingerprint": "sha256:...",
  "qdrant_alias": "db2rag_rag_01HQ...",
  "qdrant_collection": "db2rag_rag_01HQ_v3",
  "point_count": 45200,
  "files": {
    "documents.jsonl.zst": { "sha256": "...", "records": 1200 },
    "chunks.jsonl.zst": { "sha256": "...", "records": 45200 },
    "qdrant.snapshot": { "sha256": "...", "optional": true }
  }
}
```

### chunks.jsonl（1 行 1 chunk）

```json
{
  "child_chunk_id": "chk_...",
  "parent_chunk_id": "par_...",
  "text": "本文（prefix 除く）",
  "dense_vector": [0.012, ...],
  "sparse_vector": { "indices": [...], "values": [...] },
  "metadata": { /* 07_metadata_schema 全フィールド */ }
}
```

### documents.jsonl（1 行 1 論理文書）

```json
{
  "doc_version_id": "docv_...",
  "object_id": "obj_...",
  "title": "...",
  "content_hash": "...",
  "parent_chunks": ["par_...", ...]
}
```

---

## 5. Read-Only Qdrant 接続（条件付き）

| 条件 | 必須 |
|---|---|
| 消費側も Ruri-v3-310m@同一 revision | ✓ |
| 消費側が Qdrant 1.19+ | ✓ |
| 書込み権限なし | ✓ |
| `acl_tag` filter を消費側が実装 | ✓ |
| AISSS PostgreSQL 再認可を別途実装 | ✓ |

接続情報（export manifest から）:

```json
{
  "qdrant_url": "http://127.0.0.1:6333",
  "collection": "db2rag_rag_01HQ...",
  "use_alias": true
}
```

---

## 6. 互換性チェック API

```
GET /api/v1/rags/{rag_id}/compatibility
```

```json
{
  "embedding_model": "cl-nagoya/ruri-v3-310m",
  "embedding_revision": "18b60fb8c2b9df296fb4212bb7d23ef94e579cd3",
  "schema_version": 1,
  "direct_qdrant_compatible": true,
  "aisss_direct_compatible": false,
  "recommended_integration": "search_api"
}
```

---

## 7. OpenAI Embeddings 互換を提供しない理由

1. Ruri prefix 規約は OpenAI API に存在しない
2. 768 次元固定。他モデルとの変換は数学的に不可能
3. ベクトル空間の意味的整合はモデル再 embed のみで保証

消費側が embedding を再計算する場合は **テキスト + metadata のみ** export する（`include_vectors: false` オプション）。

---

## 8. 関連文書

- `13_search_preview.md`
- `08_vector_db_design.md`
- `20_open_questions.md`
