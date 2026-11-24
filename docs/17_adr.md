# 17 ADR（Architecture Decision Records）

| 項目 | 内容 |
|---|---|
| **目的** | DB2RAG の重要設計決定を ADR 形式で記録し、代替案との比較理由を固定する |
| **対象読者** | 設計者、実装者、レビュアー |
| **この文書が決めること** | 技術選定、採用理由、捨てた理由、影響範囲 |
| **この文書が決めないこと** | 実装コード、詳細 API パス、UI 配色 |

---

## ADR-001: アプリケーション構成 — ローカル Web GUI + FastAPI + 単一 GPU Worker

| 項目 | 内容 |
|---|---|
| **決定** | ローカル Web GUI（静的 SPA）+ FastAPI API + SQLite WAL（制御正本）+ 単一 GPU worker（lease 方式）の単一ホスト構成を採用する |
| **背景** | 社内オンプレ、単一 A6000、運用者 1〜3 名、バッチ変換が主用途 |
| **選択肢** | (A) Electron デスクトップ (B) FastAPI + Web GUI (C) CLI only (D) Celery + Redis + PostgreSQL |
| **採用理由** | (B) はブラウザで進捗・D&D を自然に実現し、API をローカル AI 連携に再利用できる。SQLite で制御データを単一ファイル化し運用が単純 |
| **捨てた理由** | (A) 配布・更新コスト大。(C) D&D・進捗 UI 要件を満たせない。(D) 初期構成に Redis/Celery/PostgreSQL は過剰。BackgroundTasks は長時間 GPU 処理に不向き |
| **影響** | `02_architecture.md`, `03_data_model.md`, `16_error_handling.md` |

---

## ADR-002: Vector DB — Qdrant 1.19.0（Docker Compose 固定）

| 項目 | 内容 |
|---|---|
| **決定** | Qdrant 1.19.0 を Docker Compose で固定し、`rag_id` ごとに安定 alias + 世代付き physical collection を用いる |
| **背景** | 768 次元 dense + 日本語 BM25 sparse の hybrid、payload filter、snapshot、alias による blue-green が必要 |
| **選択肢** | (A) Qdrant (B) Chroma (C) pgvector (D) Milvus |
| **採用理由** | (A) は named vectors（dense + sparse）、サーバー側 BM25、RRF、snapshot、alias を単一プロダクトで提供。ローカル Docker 運用が成熟 |
| **捨てた理由** | (B) sparse/hybrid と alias が弱い。(C) HNSW + sparse の運用複雑度が高い。(D) 単一ワークステーション運用には重い |
| **影響** | `08_vector_db_design.md`, `13_search_preview.md`, `14_local_ai_integration.md` |

**Qdrant 初期パラメータ（固定）**

| パラメータ | 値 |
|---|---|
| distance | Cosine |
| vector size (dense) | 768 |
| storage | float32、量子化なし |
| HNSW m | 32 |
| HNSW ef_construct | 256 |
| search hnsw_ef | 128 |
| sparse | BM25、tokenizer `multilingual` |

---

## ADR-003: Embedding モデル — ruri-v3-310m（ローカル FP32）

| 項目 | 内容 |
|---|---|
| **決定** | `cl-nagoya/ruri-v3-310m@18b60fb8c2b9df296fb4212bb7d23ef94e579cd3` を sentence-transformers でローカル FP32 推論。prefix・L2 正規化を強制 |
| **背景** | 日本語業務文書 RAG、768 次元、Apache-2.0、オフライン要件 |
| **選択肢** | (A) ruri-v3-310m (B) multilingual-e5-large (C) Ollama 任意モデル (D) 外部 API |
| **採用理由** | (A) は日本語特化、prefix 規約が明確、8192 token 上限、社内要件で固定指定済み |
| **捨てた理由** | (B) 日本語業務文書での社内評価なし。(C) AISSS 既存だがベクトル空間不一致。(D) 外部 API 禁止 |
| **影響** | `06_embedding_strategy.md`, `05_chunking_strategy.md`, `14_local_ai_integration.md` |

**Prefix 規約（固定）**

| 用途 | prefix |
|---|---|
| 文書（indexing） | `検索文書: ` |
| クエリ（search） | `検索クエリ: ` |
| トピック分類（本ツール未使用） | `トピック: ` |

---

## ADR-004: 検索方式 — Dense + BM25 Sparse + RRF（Reranker なし）

| 項目 | 内容 |
|---|---|
| **決定** | 同一 Qdrant collection に dense（Ruri）と BM25 sparse を併設し、RRF で融合する。reranker は入れない |
| **背景** | 日本語の固有名詞・略語・型番・数値は dense 単独で取りこぼしが多い（社内評価・文献根拠） |
| **選択肢** | (A) dense only (B) dense + BM25 + RRF (C) dense + cross-encoder reranker |
| **採用理由** | (B) は reranker なしで固有名詞 recall を改善。Qdrant 1.19 の native BM25 + RRF で追加サービス不要 |
| **捨てた理由** | (A) 日本語業務文書で Recall@5 が不足。(C) スコープ外（reranker 禁止）、レイテンシ・GPU 占有増 |
| **影響** | `08_vector_db_design.md`, `13_search_preview.md`, `18_test_and_eval.md` |

**RRF 初期パラメータ**

| パラメータ | 値 |
|---|---|
| prefetch dense | 40 |
| prefetch sparse | 40 |
| RRF k | 60 |
| 最終 top_k | 10 |

---

## ADR-005: Chunking — Parent-Child（主）+ 見出し階層 metadata（補）

| 項目 | 内容 |
|---|---|
| **決定** | 主戦略は Parent-Child。補助は `section_path` による見出し階層 metadata。Late Chunking は不採用 |
| **背景** | 規程・マニュアル・議事録は見出し構造が検索精度に直結。child は検索単位、parent は文脈復元用 |
| **選択肢** | (A) Parent-Child (B) Late Chunking (C) 固定 token 分割 (D) Heading Hierarchy only |
| **採用理由** | (A) は日本語業務文書で構造保持と検索粒度の両立が実証的に安定。parent 展開で UI プレビュー品質向上 |
| **捨てた理由** | (B) Ruri + 日本語業務文書での再現評価なし。(C) AISSS 既存方式で構造破壊。(D) 単独では長文文脈不足 |
| **影響** | `05_chunking_strategy.md`, `07_metadata_schema.md`, `13_search_preview.md` |

---

## ADR-006: 制御 DB — SQLite WAL

| 項目 | 内容 |
|---|---|
| **決定** | ジョブ・source・document version・checkpoint は SQLite 3（WAL モード）を制御正本とする |
| **背景** | 単一ホスト、バックアップ容易、Qdrant は vector 専用 |
| **選択肢** | (A) SQLite (B) PostgreSQL (C) Qdrant payload only |
| **採用理由** | (A) は単一ファイルバックアップ、WAL で並行読取、初期運用コスト最小 |
| **捨てた理由** | (B) 単一ワークステーションに過剰。(C) 複雑な状態遷移・JOIN に不向き |
| **影響** | `03_data_model.md`, `15_security_and_ops.md` |

---

## ADR-007: 文書解析 — Docling + 形式別 adapter

| 項目 | 内容 |
|---|---|
| **決定** | PDF/DOCX/PPTX/HTML は Docling → CanonicalDocument。XLSX/CSV は行・シート保持の専用 adapter。TXT/MD は構造保持 parser |
| **背景** | 多形式統一、表・見出し構造保持、ローカル OCR |
| **選択肢** | (A) Docling 中心 (B) Apache Tika 単独 (C) 形式ごと完全独自 |
| **採用理由** | (A) は PDF 表構造・OCR・Office 対応が統合。CanonicalDocument へ正規化可能 |
| **捨てた理由** | (B) 表構造・日本語 OCR 品質が不安定。(C) 開発コスト過大 |
| **影響** | `10_file_ingest.md`, `05_chunking_strategy.md` |

---

## ADR-008: MySQL 取り込み — READ ONLY Consistent Snapshot + Unbuffered Cursor

| 項目 | 内容 |
|---|---|
| **決定** | `mysql-connector-python`、READ ONLY トランザクション、consistent snapshot、unbuffered cursor、PK ベース論理文書化 |
| **背景** | 大規模テーブル、本番影響最小、再現可能な行スナップショット |
| **選択肢** | (A) ORM 一括 load (B) snapshot + unbuffered (C) binlog CDC |
| **採用理由** | (B) はメモリ効率・一貫性・READ ONLY 保証を両立 |
| **捨てた理由** | (A) 大テーブルで OOM。(C) スコープ外・運用複雑 |
| **影響** | `09_mysql_connector.md`, `04_ingestion_pipeline.md` |

---

## ADR-009: AISSS 連携境界 — Export/API 優先、直結 Qdrant は条件付き

| 項目 | 内容 |
|---|---|
| **決定** | 推奨: DB2RAG 検索 API。可搬: JSONL + snapshot。条件付き: read-only Qdrant。AISSS collection 直書きは行わない |
| **背景** | AISSS は Ollama embedding・PostgreSQL 再認可・blue-green active collection を前提。Ruri ベクトル空間は非互換 |
| **選択肢** | (A) Qdrant 共有 (B) DB2RAG API (C) JSONL 再 ingest (D) OpenAI embeddings 互換 |
| **採用理由** | (B)+(C) は vendor lock-in 最小。(A) は read-only かつモデル一致時のみ |
| **捨てた理由** | (D) ベクトル空間変換は数学的に不可能。(A) 単独は AISSS 権限モデル不一致 |
| **影響** | `14_local_ai_integration.md`, `20_open_questions.md` |

---

## ADR-010: GPU Worker — Lease 方式単一プロセス

| 項目 | 内容 |
|---|---|
| **決定** | embedding / OCR GPU 処理は単一 worker プロセスが SQLite job lease（30s TTL、heartbeat 10s）で排他実行 |
| **背景** | A6000 1 枚、OOM 回避、ジョブ再開 |
| **選択肢** | (A) FastAPI BackgroundTasks (B) 単一 lease worker (C) 複数 GPU worker |
| **採用理由** | (B) は GPU メモリ予測可能、クラッシュ時 lease 失効で再開可能 |
| **捨てた理由** | (A) プロセス内で長時間 GPU 占有・再起動で消失。(C) 単一 GPU で意味なし |
| **影響** | `02_architecture.md`, `16_error_handling.md` |

---

## ADR-011: UI導線 — 5画面 + 折りたたみ式統合ウィザード

| 項目 | 内容 |
|---|---|
| **決定** | UIを5画面に限定し、MySQL接続・対象選択・ファイル投入を「新規・追加」の折りたたみ式ウィザードへ統合する |
| **背景** | 独立INGESTION画面を順番に移動する構成では、新規作成と既存追加、MySQLとファイルの分岐がサイドバー上で混在した |
| **選択肢** | (A) 8画面をサイドバーで直接移動 (B) 5画面 + 1ページ内統合ウィザード (C) 単一巨大フォーム |
| **採用理由** | (B) は選択済み工程を要約帯に畳みながら次工程だけを広げるため、現在位置と確定内容を保持したまま4分岐を同じ導線で扱える |
| **捨てた理由** | (A) INGESTION項目が主ナビを占有し、工程の飛び越しが起きる。(C) 入力項目が同時に露出し認知負荷が高い |
| **影響** | `01_requirements.md`, `11_ui_ux.md`, `18_test_and_eval.md`, `19_implementation_backlog.md` |

**承認済み挙動**:

- W1は新規/既存の選択だけで自動進行せず、明示確定後に進む
- W2のMySQL/ファイル選択は自動進行する
- 完了済み工程は要約帯の▼で再展開し、変更時は後続工程を再確認状態へ戻す
- 独立したMySQL接続・対象選択・ファイル投入画面とINGESTIONナビを作らない

---

## ADR-012: LAN 画面の API キーはタブ内 Bearer

| 項目 | 内容 |
|---|---|
| **決定** | 非 loopback かつ `DB2RAG_API_KEY` があるとき、bundled UI は `sessionStorage` の Bearer を付ける。鍵は localStorage に残さない |
| **背景** | 鍵ありの LAN では `/api/v1/*` が認証必須なのに、画面がヘッダを送っていなかった |
| **選択肢** | (A) 画面が Bearer を付ける (B) same-origin の Cookie セッション |
| **採用理由** | (A) は既存の Bearer 照合を再利用でき、CSRF 用の新しい境界を足さない |
| **捨てた理由** | (B) は Cookie と CSRF の実装が重い |
| **影響** | `web/live.js`, `auth.py` |

---

## ADR-013: 未知の Dify metadata は無視する

| 項目 | 内容 |
|---|---|
| **決定** | `acl_tag` / `source_type` / `section_path` 以外の metadata 条件は検索に使わず、応答の `warnings` に残す |
| **背景** | Dify の自動 metadata が任意フィールド名を送ると、全体が 400 になって検索が止まる |
| **選択肢** | (A) 400 で拒否 (B) 無視して警告 |
| **採用理由** | (B) は本番の自動フィルタで検索を止めない。未知フィールドは条件に昇格させない |
| **捨てた理由** | (A) は契約違反の早期発見には向くが、Dify 側のフィールド追加で連携が落ちる |
| **影響** | `dify_adapter.py`, `api.py` |

---

## ADR 索引

| ADR | タイトル | 状態 |
|---|---|---|
| ADR-001 | アプリケーション構成 | Accepted |
| ADR-002 | Vector DB — Qdrant | Accepted |
| ADR-003 | Embedding — ruri-v3-310m | Accepted |
| ADR-004 | Hybrid 検索 — RRF | Accepted |
| ADR-005 | Parent-Child Chunking | Accepted |
| ADR-006 | SQLite WAL | Accepted |
| ADR-007 | Docling + adapter | Accepted |
| ADR-008 | MySQL snapshot | Accepted |
| ADR-009 | AISSS 連携境界 | Accepted |
| ADR-010 | GPU Worker lease | Accepted |
| ADR-011 | 5画面 + 統合ウィザード | Accepted (Master approved 2026-09-10) |
| ADR-012 | LAN 画面の Bearer | Accepted |
| ADR-013 | 未知 Dify metadata は無視 | Accepted |
