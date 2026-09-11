# 13 検索プレビュー

| 項目 | 内容 |
|---|---|
| **目的** | DB2RAG 内蔵の簡易 hybrid 検索仕様を定義する |
| **対象読者** | 実装者、QA、ローカル AI 連携開発者 |
| **この文書が決めること** | 検索 API、RRF、threshold、filter、highlight、parent 展開 |
| **この文書が決めないこと** | Reranker、生成回答 |

---

## 1. スコープ

- 変換完了後の **検索品質確認** 用
- Reranker **なし**
- 生成（LLM 回答）**なし**

---

## 2. 検索 API

```
POST /api/v1/search
{
  "rag_id": "rag_01HQ...",
  "query": "有給休暇の申請期限",
  "top_k": 10,
  "score_threshold": 0.60,
  "filters": {
    "source_type": ["file", "mysql"],
    "acl_tags": ["internal"],
    "object_id": null
  },
  "expand_parent": true,
  "hybrid": true
}
```

---

## 3. 検索フロー

```
1. query → Ruri embed（検索クエリ: {query}）→ dense_q[768]
2. query → BM25 tokenize → sparse_q
3. Qdrant prefetch: dense limit=40, sparse limit=40
4. RRF fusion (k=60)
5. score_threshold 適用
6. top_k 切り出し
7. expand_parent: parent_chunk テキスト付与
8. highlight 生成
```

---

## 4. パラメータ初期値

| パラメータ | 値 | 説明 |
|---|---|---|
| `top_k` | 10 | 返却件数 |
| `prefetch_dense` | 40 | dense 候補 |
| `prefetch_sparse` | 40 | BM25 候補 |
| `rrf_k` | 60 | RRF 定数 |
| `score_threshold` | 0.60 | dense cosine 下限（RRF 後の正規化 score） |
| `hnsw_ef` | 128 | 検索時 |
| `hybrid` | true | false 時 dense only |

---

## 5. スコア表示

| 規則 | 説明 |
|---|---|
| 表示名 | `score`（0.00〜1.00） |
| 意味 | RRF 正規化類似度。**確率ではない** |
| 注記 | UI に「score は類似度であり確率ではありません」を常時表示 |
| 色分け | ≥0.80 高、0.60〜0.80 中、<0.60 は threshold で非表示 |

---

## 6. Filter

| フィールド | 演算 |
|---|---|
| `rag_id` | must（常に自動付与） |
| `acl_tag` | must any（呼び出し元の ACL と intersection） |
| `source_type` | should any（指定時） |
| `object_id` | must（指定時） |
| `mime` | should any |
| `section_path` | text match（部分一致） |

---

## 7. Parent 展開

`expand_parent=true` 時:

```json
{
  "child_chunk_id": "chk_...",
  "child_text": "...",
  "parent_text": "（親全文）",
  "parent_truncated": false,
  "section_path": "第5章>休暇",
  "page_from": 45,
  "source_name": "manual_v3.pdf"
}
```

parent > 10,000 文字: `parent_truncated=true`、先頭 10,000 文字 + `...`

---

## 8. Highlight

| 項目 | 仕様 |
|---|---|
| 方式 | Sudachi 系トークナイザで query トークン一致 |
| マーカー | `**token**`（Markdown bold） |
| 抜粋長 | child text の一致箇所 ±100 文字 |
| 最大ハイライト | 3 箇所 / 結果 |

---

## 9. Zero-Result

| 条件 | UI 文言 |
|---|---|
| 結果 0 件 | 「該当する文書が見つかりませんでした。クエリを短くするか、filter を緩めてください。」 |
| threshold 除外のみ | 「類似度が基準（0.60）を下回る結果のみでした。」 |
| ACL 除外 | 「アクセス権限のある文書に該当がありません。」 |

---

## 10. レスポンス例

```json
{
  "query": "有給休暇の申請期限",
  "took_ms": 245,
  "total": 3,
  "results": [
    {
      "rank": 1,
      "score": 0.82,
      "title": "就業規則",
      "section_path": "第5章>休暇",
      "snippet": "...申請は休暇開始日の **10日前** までに...",
      "metadata": { "page_from": 45, "source_type": "file" }
    }
  ]
}
```

---

## 11. 関連文書

- `08_vector_db_design.md` — hybrid 設定
- `06_embedding_strategy.md` — query prefix
- `18_test_and_eval.md` — 評価指標
