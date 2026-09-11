# 06 Embedding 戦略

| 項目 | 内容 |
|---|---|
| **目的** | Ruri-v3-310m のローカル推論、prefix、正規化、バッチ、検証を定義する |
| **対象読者** | 実装者 |
| **この文書が決めること** | model revision、入力組立、FP32、batch、golden vector 検証 |
| **この文書が決めないこと** | chunk 分割、Qdrant 設定 |

---

## 1. モデル固定

| 項目 | 値 |
|---|---|
| Model ID | `cl-nagoya/ruri-v3-310m` |
| Revision (commit) | `18b60fb8c2b9df296fb4212bb7d23ef94e579cd3` |
| Library | `sentence-transformers` ≥ 3.0 |
| Runtime | PyTorch 2.x + CUDA 12.x |
| Precision | **FP32**（BF16/INT8 量子化は初期不採用） |
| Dimensions | 768 |
| Max seq length | 8192 token |
| License | Apache-2.0 |

---

## 2. Prefix 規約（厳守）

| 用途 | Prefix | 例 |
|---|---|---|
| Indexing（文書） | `検索文書: ` | `検索文書: 本規程は...` |
| Search（クエリ） | `検索クエリ: ` | `検索クエリ: 有給休暇の申請方法` |
| 未使用 | `トピック: ` | 本ツールでは使用しない |
| 未使用 | `` (empty) | 使用禁止 |

**違反時**: verify で reject、job failed。

---

## 3. 入力組立

```python
# 概念コード（設計のみ）
doc_input = f"検索文書: {child_text}"
query_input = f"検索クエリ: {user_query}"
```

| 前処理 | 規則 |
|---|---|
| child_text | chunk 本文（prefix 除く）。NFKC 済み |
| 最大長 | 550 token 相当（≈990 文字）を超える child は chunk 段階で reject |
| 空文字 | embed しない（item failed） |

---

## 4. 推論パラメータ

```python
model.encode(
    sentences,
    batch_size=<dynamic>,
    show_progress_bar=False,
    convert_to_numpy=True,
    normalize_embeddings=True,  # L2 正規化 — 必須
    precision="float32",
)
```

| パラメータ | 値 |
|---|---|
| `normalize_embeddings` | `True`（Cosine = dot product） |
| `device` | `cuda:0` |
| `num_workers` | 0（GPU 推論） |

---

## 5. 動的 Token-Budget Batch

| 段階 | batch_size | 条件 |
|---|---|---|
| 初期 | 64 | 平均 400 文字 child |
| OOM 発生 | 32 → 16 → 8 | 最大 3 回半減 |
| OOM 8 でも失敗 | job `paused` | 手動介入（モデル再起動） |

**Token budget**: 1 batch の合計 token ≤ 24,000（A6000 48GB 安全マージン）

---

## 6. OOM 縮退禁止事項

| 禁止 | 理由 |
|---|---|
| INT8/FP16 への自動降格 | ベクトル空間変化、再現性破壊 |
| normalize_embeddings=False | Cosine 検索が破綻 |
| prefix 省略 | Ruri 仕様違反 |

OOM 時は **batch 半減のみ**許可。

---

## 7. オフライン Model 配布

| 項目 | 内容 |
|---|---|
| 初回取得 | setup wizard で HuggingFace からダウンロード（社内 proxy 可） |
| 保存先 | `data/models/ruri-v3-310m/` |
| 検証 | `config.json` の `hidden_size==768` + revision hash |
| 更新 | 手動のみ（自動更新禁止） |

---

## 8. Pipeline Fingerprint

```
pipeline_fingerprint = SHA256(
  parser_version + "|" +
  chunk_strategy_version + "|" +
  embedding_model + "@" + embedding_revision + "|" +
  prefix_doc + "|" + prefix_query + "|" +
  normalize=true + "|" + precision=fp32
)
```

DocumentVersion と全 child payload に記録。

---

## 9. Golden Vector 検証（T-EMB-001）

初回セットアップおよび model 更新時に実行:

| 入力 | 期待 |
|---|---|
| `検索文書: テスト文書です。` | vector L2 norm ≈ 1.0（±1e-5） |
| 同上 2 回実行 | cosine similarity == 1.0（±1e-5） |
| dim | 768 |
| query/doc 同一本文 | cosine < 1.0（prefix 効果確認） |

golden vector ファイル: `data/models/ruri-v3-310m/golden_vectors.json`

---

## 10. スループット目標

| 条件 | 目標 |
|---|---|
| A6000, FP32, 平均 400 文字 | ≥ 80 chunk/s |
| 初回 model load | ≤ 30s |

---

## 11. 関連文書

- `05_chunking_strategy.md` — child サイズ
- `08_vector_db_design.md` — dense vector 格納
- `13_search_preview.md` — query embedding
