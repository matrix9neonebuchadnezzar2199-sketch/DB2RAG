# 01 要求仕様

| 項目 | 内容 |
|---|---|
| **目的** | DB2RAG の機能・非機能要求、非目標、能力状態、試験トレーサビリティを固定する |
| **対象読者** | プロダクトオーナー、設計者、実装者、QA |
| **この文書が決めること** | FR/NFR、用語定義、非目標、能力マトリクス、要求↔試験ID対応 |
| **この文書が決めないこと** | 実装詳細、UI配色、コード構成、AISSS内部改修 |

---

## 1. プロダクト概要

DB2RAG は、社内 MySQL 業務DB とローカル文書ファイルを **ローカル永続 RAG 知識ベース** に変換するツールである。チャット UI や生成モデルは持たず、変換・検証・エクスポートまでを担当する。

### 1.1 固定制約（変更不可）

| 項目 | 固定値 |
|---|---|
| Embedding モデル | `cl-nagoya/ruri-v3-310m@18b60fb8c2b9df296fb4212bb7d23ef94e579cd3` |
| ベクトル次元 | 768 |
| 文書 prefix | `検索文書: ` |
| クエリ prefix | `検索クエリ: ` |
| 実行環境 | オンプレ／ローカル、日本語、NVIDIA RTX A6000 (48GB) 想定 |
| 外部 Embedding API | **禁止**（データはローカル完結） |
| Reranker | 本ツールスコープ外（将来拡張として言及のみ可） |

---

## 2. 用語混同防止表

| ユーザーが言う | 設計上の実体 | 混同注意 |
|---|---|---|
| RAG | `rag_id` で識別される論理コレクション（Qdrant alias 経由） | AISSS の active embedding collection とは別物 |
| 知識ベース / KB | 上記 RAG の永続データ一式（SQLite 制御 + Qdrant vectors + 任意 export） | チャット履歴ではない |
| 取り込み / インポート | `ingestion job` による discover→activate パイプライン | 単発ファイルコピーではない |
| チャンク | **child chunk**（検索単位）。parent は文脈保持用 | 固定文字数分割ではない |
| 再取り込み | 同一 `object_id` + `content_hash` による idempotent upsert | 常に全削除再構築ではない |
| 検索プレビュー | DB2RAG 内蔵の簡易 hybrid 検索 UI | AISSS 本番チャットではない |
| エクスポート | `manifest.json` + JSONL + Qdrant snapshot | OpenAI embeddings API 提供ではない |

---

## 3. 機能要求（FR）

| ID | 要求 | 受入基準（要約） | 試験ID |
|---|---|---|---|
| FR-001 | MySQL 接続（READ ONLY） | 8.4/9.x LTS、接続テスト成功、書込み不可 | T-CONN-001, T-MYSQL-001 |
| FR-002 | テーブル／ビュー選択取り込み | PK ベース論理文書化、JOIN 定義反映 | T-MYSQL-010〜012 |
| FR-003 | SQL 結果取り込み | SELECT/CTE のみ、行数上限・タイムアウト遵守 | T-MYSQL-020 |
| FR-004 | ファイル D&D 取り込み | PDF/DOCX/XLSX/PPTX/CSV/TXT/MD/HTML 対応 | T-FILE-001〜008 |
| FR-005 | フォルダ再帰取り込み | symlink 拒否、除外パターン適用 | T-FILE-010 |
| FR-006 | 新規 RAG 作成 | `rag_id` 発行、空 collection + alias 作成 | T-RAG-001 |
| FR-007 | 既存 RAG 追記 | 同一 `rag_id` に source 追加、重複排除 | T-RAG-002, T-DEDUP-001 |
| FR-008 | 変換ジョブ管理 | 作成・取消・再開・item 単位 retry | T-JOB-001〜005 |
| FR-009 | 進捗表示 | 進捗率・経過・ETA・件数・現在処理名 | T-PROG-001〜003 |
| FR-010 | 容量見積 | 原文・chunk・vector・総容量の事前/事後表示 | T-CAP-001 |
| FR-011 | 検索プレビュー | dense+BM25+RRF、filter、highlight | T-SEARCH-001〜005 |
| FR-012 | エクスポート | manifest + JSONL.zst + snapshot、SHA-256 | T-EXPORT-001 |
| FR-013 | ローカル AI 連携 API | vendor-neutral 検索 API、接続仕様公開 | T-INT-001 |
| FR-014 | ACL タグ付与 | `acl_tag` による filter 検索 | T-ACL-001 |
| FR-015 | 差分再同期 | `updated_at` + `row_hash` による増分更新 | T-MYSQL-030 |
| FR-016 | OCR（条件付き） | PDF 低品質ページのみローカル日本語 OCR | T-FILE-020 |
| FR-017 | Hybrid 検索 | dense Cosine + BM25 sparse + RRF | T-SEARCH-010 |
| FR-018 | 世代切替 | blue-green alias 切替で無停止 activate | T-VEC-010 |
| FR-019 | 評価セット実行 | 120 問 eval gate 実行・レポート出力 | T-EVAL-001 |
| FR-020 | RAG 削除 | secure purge（vectors + SQLite + export） | T-SEC-010 |
| FR-021 | 統合取り込みウィザード | 5画面構成内で新規/既存 × MySQL/ファイルの4分岐を完走。独立INGESTION画面を持たない | T-UI-001〜005 |
| FR-022 | 工程の段階開示 | 確定工程を要約帯へ畳み次工程を展開。▼で再編集し、変更時は後続を再確認。W1は明示確定、W2は選択時に進行 | T-UI-006〜009 |

---

## 4. 非機能要求（NFR）

| ID | 要求 | 目標値（初期） | 試験ID |
|---|---|---|---|
| NFR-001 | 再現性 | 同一入力 + 同一 model revision → 同一 chunk hash / vector（±FP32 許容差 1e-5） | T-EMB-001 |
| NFR-002 | 単一 GPU スループット | A6000: embedding ≥ 80 chunk/s（512 token 相当、FP32） | T-PERF-001 |
| NFR-003 | 検索レイテンシ（プレビュー） | p95 ≤ 800ms（10万 child chunk、filter なし） | T-PERF-010 |
| NFR-004 | 可用性 | 単一ホスト、ジョブ lease 30s、worker 再起動で再開 | T-ERR-020 |
| NFR-005 | データ残留 | 削除後 Qdrant + SQLite から復元不可（secure purge） | T-SEC-010 |
| NFR-006 | オフライン動作 | 初回 model 取得後、外部ネットワーク不要 | T-SEC-020 |
| NFR-007 | 監査 | 全 job に `created_by` / `pipeline_fingerprint` 記録 | T-OPS-001 |
| NFR-008 | 日本語精度 | Recall@5 ≥ 0.75（調整 40 問）、盲検 80 問で Gate | T-EVAL-001 |
| NFR-009 | UI 応答 | API p95 ≤ 300ms（進捗 poll 除く） | T-PERF-020 |
| NFR-010 | 容量見積誤差 | 実測との誤差 ≤ ±20%（quick 係数使用時） | T-CAP-001 |

---

## 5. 非目標（Explicit Non-Goals）

| 項目 | 理由 |
|---|---|
| チャット UI / 生成回答 | AISSS 等ローカル AI の責務 |
| LLM / Embedding モデルの学習・fine-tune | スコープ外。固定モデル利用のみ |
| クラウド Vector DB（Pinecone 等） | ローカル永続が要件 |
| Reranker 統合 | 本ツールでは候補検索まで。rerank は消費側または将来 |
| 画像理解（マルチモーダル embedding） | 初期対象外。PDF はテキスト層 + OCR |
| OpenAI 互換 embeddings endpoint 提供 | ベクトル空間は Ruri 固定。変換不可 |
| AISSS 内部 collection への直接書込み | 権限・モデル不一致。連携は API / export |
| MySQL 書込み・CDC バイナリログ購読 | READ ONLY スナップショットのみ |
| リアルタイム同期（秒単位） | バッチジョブ方式。最短 5 分ポーリングは将来 |

---

## 6. 能力状態マトリクス

| 能力 | MVP (PHASE 1) | GA (PHASE 2) | 将来 |
|---|---|---|---|
| MySQL テーブル取り込み | ✓ | ✓ | — |
| MySQL ビュー取り込み | ✓ | ✓ | — |
| 任意 SQL 取り込み | 制限付き ✓ | ✓ | — |
| PDF テキスト層 | ✓ | ✓ | — |
| PDF OCR | 条件付き ✓ | ✓ | — |
| DOCX/PPTX/HTML | ✓ | ✓ | — |
| XLSX/CSV 行構造 | ✓ | ✓ | — |
| Hybrid (dense+BM25) | ✓ | ✓ | — |
| 検索プレビュー | ✓ | ✓ | — |
| JSONL + snapshot export | ✓ | ✓ | — |
| 評価 120 問 Gate | — | ✓ | — |
| LAN TLS | — | ✓ | — |
| Reranker | — | — | 検討のみ |
| 増分 CDC | — | — | 検討 |

---

## 7. 対象ファイル形式（初期）

| 形式 | MIME / 拡張子 | 優先度 |
|---|---|---|
| PDF | `application/pdf`, `.pdf` | P0 |
| Word | `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `.docx` | P0 |
| Excel | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `.xlsx` | P0 |
| CSV | `text/csv`, `.csv` | P0 |
| テキスト | `text/plain`, `.txt` | P0 |
| Markdown | `text/markdown`, `.md` | P0 |
| PowerPoint | `.pptx` | P1 |
| HTML | `text/html`, `.html` | P1 |

---

## 8. 要求トレーサビリティ（試験スイート索引）

| 試験スイート | 対象 FR/NFR | 詳細文書 |
|---|---|---|
| T-CONN | FR-001 | `09_mysql_connector.md` |
| T-MYSQL | FR-002, FR-003, FR-015 | `09_mysql_connector.md` |
| T-FILE | FR-004, FR-005, FR-016 | `10_file_ingest.md` |
| T-RAG | FR-006, FR-007 | `03_data_model.md` |
| T-JOB | FR-008 | `04_ingestion_pipeline.md`, `16_error_handling.md` |
| T-PROG | FR-009 | `12_progress_and_capacity.md` |
| T-CAP | FR-010, NFR-010 | `12_progress_and_capacity.md` |
| T-SEARCH | FR-011, FR-017 | `13_search_preview.md` |
| T-EXPORT | FR-012 | `14_local_ai_integration.md` |
| T-INT | FR-013 | `14_local_ai_integration.md` |
| T-ACL | FR-014 | `07_metadata_schema.md`, `15_security_and_ops.md` |
| T-VEC | FR-018 | `08_vector_db_design.md` |
| T-EMB | NFR-001 | `06_embedding_strategy.md` |
| T-PERF | NFR-002, NFR-003, NFR-009 | `18_test_and_eval.md` |
| T-ERR | NFR-004 | `16_error_handling.md` |
| T-SEC | NFR-005, NFR-006, FR-020 | `15_security_and_ops.md` |
| T-OPS | NFR-007 | `15_security_and_ops.md` |
| T-EVAL | NFR-008, FR-019 | `18_test_and_eval.md` |
| T-DEDUP | FR-007 | `04_ingestion_pipeline.md` |
| T-UI | FR-021, FR-022 | `11_ui_ux.md`, `18_test_and_eval.md` |

---

## 9. 関連文書

- `00_overview.md` — 全体要約
- `17_adr.md` — 設計決定記録
- `20_open_questions.md` — 外部依存の未決事項のみ
