# 12 進捗・容量見積

| 項目 | 内容 |
|---|---|
| **目的** | 重み付き進捗、ETA 算出、容量見積式を定義する |
| **対象読者** | 実装者 |
| **この文書が決めること** | work unit 重み、ETA アルゴリズム、容量式、更新間隔 |
| **この文書が決めないこと** | UI 配色 |

---

## 1. Work Unit 重み

各 JobItem は処理前に `work_unit` を見積もる。

| ソース | 重み式 |
|---|---|
| PDF | `pages × 1.0` |
| DOCX/PPTX/HTML | `estimated_pages × 0.8`（1 ページ ≈ 3000 文字） |
| TXT/MD | `bytes / 3000` |
| XLSX/CSV | `rows × 0.3` |
| MySQL row | `1.0` per row |
| MySQL 論理文書 | `long_text_chars / 400`（min 1.0） |

**GPU embed 追加重み**: `child_count × 0.05`

```
item_work_unit = base_weight + child_estimate × 0.05
job_work_units_total = sum(item_work_unit)
```

---

## 2. 進捗率

```
progress_pct = (work_units_done / work_units_total) × 100
```

ステージ別 done 加算:

| ステージ完了 | 加算率（item 内） |
|---|---|
| extract | 15% |
| normalize | 25% |
| chunk | 40% |
| embed | 80% |
| upsert | 95% |
| verified | 100% |

---

## 3. ETA 算出

| 条件 | 表示 |
|---|---|
| 完了 item < 5 **かつ** 経過 < 15s | `ETA: 算出中...` |
| 以降 | EWMA ベース |

```python
# EWMA rate (work_units per second)
alpha = 0.3
rate_ewma = alpha * instant_rate + (1 - alpha) * rate_ewma_prev
eta_seconds = (work_units_total - work_units_done) / rate_ewma
```

| パラメータ | 値 |
|---|---|
| 更新間隔 | 2s（SSE push） |
| ETA 最小表示 | `約 1 分`（< 60s 切上げ） |
| ETA 最大表示 | `約 24 時間以上`（上限 cap） |

---

## 4. 表示項目

| 項目 | 説明 |
|---|---|
| `progress_pct` | 0.0〜100.0、小数 1 桁 |
| `elapsed` | `HH:MM:SS` |
| `eta` | 上記アルゴリズム |
| `items_done / items_total` | 件数 |
| `current_item_name` | ファイル名 or `schema.table` |
| `current_substep` | 例: `page 45/120`, `embed batch 12/30` |
| `original_size_bytes` | 原文合計 |
| `estimated_rag_bytes` | 見積 RAG サイズ |
| `actual_rag_bytes` | 完了後実測 |

---

## 5. 容量見積式

### 5.1 抽出テキスト

```
extracted_text_bytes = sum(file_bytes × extraction_ratio)
```

| 形式 | extraction_ratio（初期） |
|---|---|
| PDF/DOCX | 0.15 |
| XLSX/CSV | 0.30 |
| TXT/MD | 0.95 |
| MySQL | `avg_row_bytes × row_count` |

### 5.2 Chunk 数

```
estimated_chunks = extracted_text_bytes / avg_chunk_bytes
avg_chunk_bytes = 420  # 初期係数
```

### 5.3 Vector サイズ

```
dense_bytes = N_chunks × 768 × 4
sparse_bytes = N_chunks × 200
```

### 5.4 インデックスオーバーヘッド

```
hnsw_overhead = dense_bytes × 0.5
payload_bytes = N_chunks × 1_500
sqlite_bytes = N_chunks × 800
snapshot_reserve = (dense_bytes + sparse_bytes) × 0.2
```

### 5.5 総計

```
raw_total = extracted_text_bytes + dense_bytes + sparse_bytes
           + hnsw_overhead + payload_bytes + sqlite_bytes + snapshot_reserve

estimated_rag_bytes = raw_total × quick_factor
quick_factor = 1.6  # UI クイック見積用
```

---

## 6. 実例

**入力**: 500 PDF ページ + 10,000 MySQL rows + 50 XLSX files (5,000 rows)

| 項目 | 見積 |
|---|---|
| extracted_text | ~180 MB |
| chunks | ~430,000 |
| dense | 430,000 × 768 × 4 = **1.32 GB** |
| sparse | ~86 MB |
| overhead | ~0.85 GB |
| **quick 総計** | **(1.32+0.18+0.86+0.85) × 1.6 ≈ 5.4 GB** |

完了後に実測値を表示し、見積誤差を記録（NFR-010: ±20% 以内）。

---

## 7. 関連文書

- `11_ui_ux.md` — S7 表示
- `08_vector_db_design.md` — vector サイズ
- `01_requirements.md` — NFR-010
