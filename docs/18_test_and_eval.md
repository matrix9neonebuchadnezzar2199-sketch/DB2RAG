# 18 テスト・評価

| 項目 | 内容 |
|---|---|
| **目的** | 120 問評価セット、メトリクス、Gate 条件、非機能試験を定義する |
| **対象読者** | QA、実装者 |
| **この文書が決めること** | 評価問設計、合格基準、試験スイート、未測定値の扱い |
| **この文書が決めないこと** | 実装コード、本番データ |

---

## 1. 評価セット構成（120 問）

| カテゴリ | 問数 | 内容 |
|---|---|---|
| 規程・条文 | 25 | 章番号、条項参照 |
| 固有名詞 | 20 | 社内制度名、製品名、部署名 |
| 数値・日付 | 20 | 期限、金額、数量 |
| 略語・型番 | 15 | 3〜5 文字略語、型番 |
| 表ファクト | 15 | XLSX/CSV 行の特定値 |
| MySQL ファクト | 15 | DB レコードの属性 |
| Hard-negative | 30 | 似ているが不正解の質問 |

### 分割

| セット | 問数 | 用途 |
|---|---|---|
| 調整セット | 40 | chunk/threshold チューニング（開発中のみ） |
| 盲検セット | 80 | **GA Gate**（チューニング禁止） |

`question_set_version`: `eval_v1_120`

---

## 2. 問い形式

```json
{
  "question_id": "Q-001",
  "query": "有給休暇の申請期限は何日前までか",
  "expected_child_chunk_ids": ["chk_..."],
  "expected_parent_chunk_ids": ["par_..."],
  "category": "regulation",
  "split": "tuning|blind",
  "has_answer": true
}
```

Hard-negative:

```json
{
  "question_id": "HN-001",
  "query": "有給休暇の申請方法",  // 期限ではなく方法
  "forbidden_chunk_ids": ["chk_deadline_chunk"],
  "category": "hard_negative"
}
```

---

## 3. メトリクス

| メトリクス | 定義 | Gate 目標（盲検 80） |
|---|---|---|
| Recall@5 | top5 に正解 chunk 含む率 | ≥ 0.75 |
| Recall@20 | top20 | ≥ 0.90 |
| MRR@10 | Mean Reciprocal Rank | ≥ 0.65 |
| nDCG@10 | 正解ランクの DCG | ≥ 0.70 |
| Zero-result rate | 結果 0 件率 | ≤ 0.05 |
| Hard-negative FP@5 | 禁止 chunk が top5 に入る率 | 参考値。v0.2.1 の合否は 1 位率 |
| Hard-negative top1 | 禁止 chunk が 1 位になる率 | ≤ 0.10 |
| Structural integrity | 表・見出し保持率（手動サンプル 50 件） | ≥ 0.95 |
| ANN recall | HNSW vs brute-force top10 一致率 | ≥ 0.98 |
| Search latency p95 | 10万 chunk | ≤ 800ms |
| Capacity estimate error | 見積 vs 実測 | ≤ ±20% |

**未測定値を合格表示しない**。Gate レポートは実測値のみ。

---

## 4. Gate 条件（GA リリース）

| # | 条件 | 必須 |
|---|---|---|
| G1 | 盲検 Recall@5 ≥ 0.75 | ✓ |
| G2 | 盲検 MRR@10 ≥ 0.65 | ✓ |
| G3 | Zero-result ≤ 5% | ✓ |
| G4 | Hard-negative FP@5 ≤ 10% | ✓ |
| G5 | ANN recall ≥ 0.98 | ✓ |
| G6 | T-CONN〜T-SEC 全スイート pass | ✓ |
| G7 | golden vector 検証 pass | ✓ |
| G8 | 21 設計文書整合性 audit pass | ✓ |
| G9 | 承認済みUIの T-UI-001〜009 pass | ✓ |

1 つでも未達 → GA ブロック。

---

## 5. 機能試験スイート

### T-CONN / T-MYSQL（`09_mysql_connector.md`）

| ID | 内容 |
|---|---|
| T-CONN-001 | READ ONLY 接続成功 |
| T-MYSQL-001 | 書込み拒否 |
| T-MYSQL-010 | テーブル全件取り込み |
| T-MYSQL-011 | JOIN template |
| T-MYSQL-012 | コード値ラベル化 |
| T-MYSQL-020 | SQL 結果取り込み |
| T-MYSQL-030 | 差分同期 |

### T-FILE（`10_file_ingest.md`）

| ID | 内容 |
|---|---|
| T-FILE-001 | PDF テキスト層 |
| T-FILE-002 | DOCX |
| T-FILE-003 | XLSX 行 |
| T-FILE-004 | CSV |
| T-FILE-005 | TXT/MD |
| T-FILE-008 | フォルダ再帰 |
| T-FILE-010 | symlink 拒否 |
| T-FILE-020 | OCR 条件付き |

### T-SEARCH（`13_search_preview.md`）

| ID | 内容 |
|---|---|
| T-SEARCH-001 | hybrid 基本検索 |
| T-SEARCH-005 | ACL filter |
| T-SEARCH-010 | RRF 動作確認 |

### T-UI（`11_ui_ux.md`）

| ID | 内容 |
|---|---|
| T-UI-001 | サイドバーと主導線がS1〜S5の5画面だけで構成され、INGESTION区分がない |
| T-UI-002 | 新規RAG + MySQL分岐をW1〜W5まで完走 |
| T-UI-003 | 新規RAG + ファイル分岐をW1〜W5まで完走 |
| T-UI-004 | 既存RAGへ追加 + MySQL分岐をW1〜W5まで完走 |
| T-UI-005 | 既存RAGへ追加 + ファイル分岐をW1〜W5まで完走 |
| T-UI-006 | 工程確定時に現在工程が要約帯へ畳まれ、次工程だけが展開 |
| T-UI-007 | 完了帯の▼で再展開でき、値変更時に後続工程が再確認状態へ戻る |
| T-UI-008 | W1は新規/既存の選択だけでは進まず、明示確定ボタンでW2へ進む |
| T-UI-009 | W2はMySQL/ファイル選択時にW3へ自動進行 |

### T-EVAL

| ID | 内容 |
|---|---|
| T-EVAL-001 | 120 問自動評価実行 + レポート |

---

## 6. 非機能試験

| ID | 内容 | 目標 |
|---|---|---|
| T-PERF-001 | embedding スループット | ≥ 80 chunk/s |
| T-PERF-010 | 検索 p95 | ≤ 800ms |
| T-PERF-020 | API p95（進捗除く） | ≤ 300ms |
| T-EMB-001 | golden vector | pass |
| T-ERR-020 | worker クラッシュ復旧 | lease 再開 |
| T-SEC-010 | secure purge | 復元不可 |
| T-SEC-020 | オフライン動作 | 外部通信 0 |
| T-ACL-001 | ACL enforcement | 403 確認 |

---

## 7. 評価実行フロー

```
1. eval_v1_120.json を RAG に対して実行
2. 各 query → /api/v1/search
3. expected_child_chunk_ids と照合
4. メトリクス算出 → EvaluationRun 保存
5. Gate 判定 → pass/fail レポート出力
```

---

## 8. 関連文書

- `01_requirements.md` — FR/NFR 対応
- `13_search_preview.md`
- `19_implementation_backlog.md` — PHASE Gate
