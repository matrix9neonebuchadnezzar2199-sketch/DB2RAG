# 08 Vector DB 設計（Qdrant）

| 項目 | 内容 |
|---|---|
| **目的** | Qdrant 1.19.0 の collection、alias、point、hybrid vector、世代管理を定義する |
| **対象読者** | 実装者、運用者、ローカル AI 連携開発者 |
| **この文書が決めること** | collection 命名、HNSW パラメータ、dense+sparse、snapshot、削除 |
| **この文書が決めないこと** | embedding 推論、chunk 分割 |

---

## 1. デプロイ固定

| 項目 | 値 |
|---|---|
| Qdrant バージョン | `1.19.0`（Docker image tag 固定） |
| ホストポート | `6333` (HTTP), `6334` (gRPC、内部のみ) |
| 永続 volume | `./data/qdrant_storage` |
| API キー | ローカルは無効。LAN モードは必須 |

---

## 2. Collection 命名

| 種別 | パターン | 例 |
|---|---|---|
| 安定 alias | `db2rag_{rag_id}` | `db2rag_rag_01HQ...` |
| Physical (active) | `db2rag_{rag_id}_v{n}` | `db2rag_rag_01HQ_v3` |
| Physical (staging) | `db2rag_{rag_id}_staging_{job_id}` | 変換中のみ |

**ルール**:
- 検索・export は **alias のみ**参照（physical 名を外部公開しない）
- reindex 完了後 `alias` を atomic 切替 → 旧 physical は 7 日後 GC

---

## 3. Collection 作成パラメータ

```yaml
# 概念設定（実装は Qdrant REST API）
vectors:
  dense:
    size: 768
    distance: Cosine
    on_disk: false          # A6000 + 10万点以下は RAM
sparse_vectors:
  sparse:
    index:
      on_disk: false
hnsw_config:
  m: 32
  ef_construct: 256
  full_scan_threshold: 10000
optimizers_config:
  default_segment_number: 2
quantization_config: null   # 量子化なし（精度優先）
```

---

## 4. Named Vectors

| Name | 型 | 用途 |
|---|---|---|
| `dense` | float32[768] | Ruri embedding（L2 正規化済み） |
| `sparse` | BM25 indices/values | 日本語トークン BM25 |

同一 point に両方必須。片方欠損 point は verify で reject。

---

## 5. Point ID

```
point_id = UUIDv5(NAMESPACE_DB2RAG, child_chunk_id)
NAMESPACE_DB2RAG = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"  # 固定
```

決定的 ID により re-upsert は上書き（重複 point なし）。

---

## 6. Upsert バッチ

| パラメータ | 値 |
|---|---|
| batch size | 100 points |
| wait | `true`（verify 前に反映確認） |
| ordering | strong（単一ジョブ内） |

---

## 7. Hybrid 検索（Qdrant 側）

```yaml
prefetch:
  - query: dense_vector
    using: dense
    limit: 40
  - query: sparse_vector
    using: sparse
    limit: 40
query:
  fusion: rrf
  rrf_k: 60
limit: 10
filter: <payload filter>
params:
  hnsw_ef: 128
```

---

## 8. BM25 Sparse 生成（ローカル）

| 項目 | 値 |
|---|---|
| tokenizer | `multilingual`（Qdrant 1.19） |
| 対象テキスト | child chunk 本文（prefix 除く） |
| 正規化 | NFKC、Sudachi 相当の形態素分割は CPU worker 側でトークン列生成 → Qdrant sparse vector 形式へ変換 |
| IDF modifier | 既定（collection 内 DF） |

---

## 9. Payload Filter 例

```json
{
  "must": [
    { "key": "rag_id", "match": { "value": "rag_01HQ..." } },
    { "key": "acl_tag", "match": { "any": ["internal", "dept:HR"] } }
  ]
}
```

---

## 10. 世代切替（Blue-Green）

```
1. staging_collection へ全 upsert
2. verify: point count == SQLite child_count
3. sample search 5 件（golden query）
4. alias を staging → active に atomic switch
5. 旧 active を archived_collections テーブルに登録
6. 7 日後バッチで旧 collection delete
```

失敗時: alias **切替しない**。staging は手動 or 自動 GC。

---

## 11. Snapshot / Restore

| 操作 | パス | 用途 |
|---|---|---|
| 作成 | `POST /collections/{name}/snapshots` | export、バックアップ |
| 保存 | `data/exports/{export_id}/qdrant.snapshot` | AISSS 連携 |
| 復元 | `PUT /collections/{name}/snapshots/upload` | DR |

snapshot には manifest の `qdrant_snapshot_sha256` と照合必須。

---

## 12. 削除

| 操作 | 範囲 | 手順 |
|---|---|---|
| Object 削除 | 特定 `object_id` の points | filter delete |
| Source 削除 | source 配下全 points | filter delete + SQLite cascade |
| RAG purge | 全 collection + alias | delete collection + alias、SQLite `purged` |

secure purge: snapshot ファイルも SHA-256 verify 後に削除。

---

## 13. 容量見積（Vector 部分）

```
dense_bytes  = N_points × 768 × 4
sparse_bytes ≈ N_points × 200   # 平均 50 token × 4 byte（初期係数）
hnsw_overhead = dense_bytes × 0.5
payload_bytes ≈ N_points × 1_500  # 平均 metadata サイズ
total_qdrant ≈ (dense_bytes + sparse_bytes + hnsw_overhead + payload_bytes) × 1.1
```

詳細係数は `12_progress_and_capacity.md`。

---

## 14. 関連文書

- `06_embedding_strategy.md` — dense vector 生成
- `07_metadata_schema.md` — payload
- `13_search_preview.md` — 検索パラメータ
- `14_local_ai_integration.md` — read-only 接続条件
