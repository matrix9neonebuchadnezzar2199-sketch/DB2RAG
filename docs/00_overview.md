# 00 DB2RAG 設計概要

| 項目 | 内容 |
|---|---|
| **目的** | DB2RAG 設計書群の全体要約、採用構成、主要数値、文書索引を提供する |
| **対象読者** | 全ステークホルダー（最初に読む文書） |
| **この文書が決めること** | スコープ要約、技術スタック一覧、処理フロー、設計書索引 |
| **この文書が決めないこと** | 各領域の詳細（個別文書を参照） |

---

## 1. プロダクト定義

**DB2RAG** は、社内 MySQL 業務 DB とローカル文書ファイルを **ローカル永続 RAG 知識ベース** に変換するツールである。

| やること | やらないこと |
|---|---|
| 取り込み・変換・埋め込み・検証 | チャット UI |
| 検索プレビュー | LLM 学習/fine-tune |
| Export（AISSS 等連携用） | クラウド Vector DB |
| 進捗・容量見積 | Reranker |
| | OpenAI embeddings API |

---

## 2. 採用構成（確定）

| 層 | 技術 | バージョン/固定値 |
|---|---|---|
| UI | ローカル Web SPA | localhost:8780 |
| API | FastAPI | :8781 |
| 制御 DB | SQLite WAL | `data/db2rag.db` |
| Vector DB | Qdrant | **1.19.0**（Docker） |
| Embedding | ruri-v3-310m | revision `18b60fb8...`、768dim、FP32 |
| Sparse | Qdrant BM25 | multilingual tokenizer |
| 検索融合 | RRF | prefetch 40+40, k=60 |
| 文書解析 | Docling + adapter | PDF/DOCX/PPTX/HTML/XLSX/CSV/TXT/MD |
| MySQL | mysql-connector-python | READ ONLY snapshot |
| GPU | 単一 worker + lease | A6000 48GB |
| Chunk | Parent-Child + section_path | 文書種別パラメータ |

---

## 3. 処理フロー（要約）

```
Source登録 → Job作成 → discover → snapshot → extract → normalize
  → parent/child chunk → Ruri embed + BM25 → Qdrant upsert
  → verify → alias activate → 検索プレビュー / Export
```

---

## 4. 主要数値（初期値）

| 項目 | 値 |
|---|---|
| dense 次元 | 768 |
| distance | Cosine（L2 正規化） |
| HNSW m / ef_construct / ef | 32 / 256 / 128 |
| child target 文字（規程） | 400（min 200, max 600, overlap 80） |
| child target 文字（MySQL record） | 380 |
| Ruri prefix（doc/query） | `検索文書: ` / `検索クエリ: ` |
| search top_k | 10 |
| score threshold | 0.60 |
| ETA 算出開始 | 5 件 or 15 秒後 |
| 容量 quick 係数 | 1.6 |
| eval Gate Recall@5 | ≥ 0.75（盲検 80 問） |
| lease TTL / heartbeat | 30s / 10s |

---

## 5. AISSS 連携境界

| 方式 | 状態 |
|---|---|
| DB2RAG 検索 API | **推奨** |
| JSONL + manifest export | **可搬** |
| Qdrant read-only 直結 | 条件付き（同一 Ruri revision 必須） |
| AISSS collection 直書き | **不可** |
| OpenAI embeddings 互換 | **提供しない** |

詳細: `14_local_ai_integration.md`、`20_open_questions.md`

---

## 6. 設計書索引（全 22 文書）

| # | ファイル | 内容 |
|---|---|---|
| 00 | `00_overview.md` | **本書** — 全体要約 |
| 01 | `01_requirements.md` | FR/NFR、非目標、試験トレーサビリティ |
| 02 | `02_architecture.md` | コンポーネント、フロー、信頼境界 |
| 03 | `03_data_model.md` | エンティティ、ID、状態遷移 |
| 04 | `04_ingestion_pipeline.md` | パイプライン DAG、checkpoint、dedup |
| 05 | `05_chunking_strategy.md` | Parent-Child、文書種別パラメータ |
| 06 | `06_embedding_strategy.md` | Ruri、prefix、FP32、golden vector |
| 07 | `07_metadata_schema.md` | payload 必須フィールド、ACL |
| 08 | `08_vector_db_design.md` | Qdrant collection、alias、hybrid |
| 09 | `09_mysql_connector.md` | MySQL snapshot、template、差分 |
| 10 | `10_file_ingest.md` | D&D、parser、OCR |
| 11 | `11_ui_ux.md` | 5画面 + 折りたたみ式統合ウィザード |
| 12 | `12_progress_and_capacity.md` | 進捗、ETA、容量式 |
| 13 | `13_search_preview.md` | hybrid 検索仕様 |
| 14 | `14_local_ai_integration.md` | AISSS 連携、export |
| 15 | `15_security_and_ops.md` | セキュリティ、backup、purge |
| 16 | `16_error_handling.md` | retry、lease、checkpoint |
| 17 | `17_adr.md` | 設計決定記録（11 ADR） |
| 18 | `18_test_and_eval.md` | 120 問 eval、Gate |
| 19 | `19_implementation_backlog.md` | PHASE 00〜10 実装計画 |
| 20 | `20_open_questions.md` | 外部依存 OQ（6 件） |
| 21 | `21_v02_audit_and_overhaul_plan.md` | v0.2.0 完成度評価・全体改修設計・Phase 1〜6 Grok 実装指示書 |

---

## 7. ADR サマリ

| ADR | 決定 |
|---|---|
| 001 | FastAPI + SQLite + 単一 GPU worker |
| 002 | Qdrant 1.19.0 |
| 003 | ruri-v3-310m FP32 |
| 004 | dense + BM25 + RRF |
| 005 | Parent-Child chunking |
| 006 | SQLite WAL |
| 007 | Docling + adapter |
| 008 | MySQL READ ONLY snapshot |
| 009 | AISSS 連携は API/export 優先 |
| 010 | GPU lease worker |
| 011 | 承認済み5画面 + 折りたたみ式統合ウィザード |

---

## 8. 日本語 RAG 設計の要点

1. **固定文字数分割を使わない** — 文書種別の句点境界分割
2. **MySQL 列連結禁止** — 論理文書テンプレート
3. **Hybrid 必須** — 固有名詞・略語・数値の dense 取りこぼし対策
4. **Ruri prefix 厳守** — doc/query で異なる prefix
5. **Parent-Child** — 検索精度と文脈復元の両立
6. **Idempotent** — content_hash / row_hash による重複排除

---

## 9. 実装への引き渡し

- 設計状態: **IMPLEMENTATION READY（2026-09-10）**
- 実装 PHASE: `19_implementation_backlog.md`（PHASE 00〜10）
- UI Gate: **PASS** — `mockup.html`から始まるサイバーテーマ5画面を承認済み正本とする
- UI 契約: 新規/既存 × MySQL/ファイルの4分岐、W1明示確定、W2自動進行
- GA 条件: `18_test_and_eval.md` G1〜G9 全 pass
- 外部未決: `20_open_questions.md`（6 OQ、内部 TBD なし）

---

*設計書バージョン: 1.1 / UI承認・実装引き渡し確定: 2026-09-10*
