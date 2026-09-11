# 02 アーキテクチャ

| 項目 | 内容 |
|---|---|
| **目的** | DB2RAG のコンポーネント境界、データフロー、信頼境界、正常/失敗経路を定義する |
| **対象読者** | 設計者、実装者、運用者 |
| **この文書が決めること** | プロセス構成、API 境界、ストレージ役割、ネットワーク既定 |
| **この文書が決めないこと** | chunk 数値、metadata 全フィールド、UI ワイヤ詳細 |

---

## 1. 論理構成図

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser (localhost:8780)                                       │
│  ┌──────────────┐  REST/SSE    ┌─────────────────────────────┐  │
│  │  Web GUI SPA │◄────────────►│  FastAPI API (:8781)        │  │
│  └──────────────┘              │  - RAG / Job / Search / Export│
│                                └───────────┬─────────────────┘  │
└────────────────────────────────────────────┼────────────────────┘
                                             │
         ┌───────────────────────────────────┼───────────────────────────┐
         │                                   │                           │
         ▼                                   ▼                           ▼
┌─────────────────┐              ┌─────────────────────┐      ┌──────────────────┐
│ SQLite WAL      │              │ Job Broker          │      │ Qdrant 1.19      │
│ (制御正本)       │◄────────────►│ (SQLite内蔵)        │      │ (:6333 Docker)   │
│ rag/source/job  │              │ lease / checkpoint  │      │ dense + sparse   │
└─────────────────┘              └──────────┬──────────┘      └──────────────────┘
                                              │
                              ┌───────────────┴───────────────┐
                              ▼                               ▼
                   ┌─────────────────────┐         ┌─────────────────────┐
                   │ CPU Worker          │         │ GPU Worker (×1)     │
                   │ - discover          │         │ - Ruri embed FP32   │
                   │ - parse (Docling)   │         │ - OCR (条件付き)    │
                   │ - chunk             │         │ lease TTL 30s       │
                   │ - BM25 tokenize     │         └─────────────────────┘
                   └──────────┬──────────┘
                              │
                              ▼
                   ┌─────────────────────┐
                   │ MySQL Reader        │
                   │ READ ONLY snapshot  │
                   └─────────────────────┘
```

---

## 2. コンポーネント責務

| コンポーネント | 責務 | 非責務 |
|---|---|---|
| Web GUI SPA | RAG 管理、D&D、進捗表示、検索プレビュー | embedding 推論、DB 直接接続 |
| FastAPI API | REST、認可チェック、SQLite/Qdrant オーケストレーション | 長時間 GPU 処理のインライン実行 |
| SQLite WAL | RAG/source/job/chunk metadata、checkpoint、secret 参照 | ベクトル保存 |
| Job Broker | lease 付与、heartbeat、checkpoint 復旧 | パースロジック |
| CPU Worker | discover/extract/normalize/chunk/BM25 index 準備 | GPU embedding |
| GPU Worker | Ruri embedding、条件付き OCR | MySQL 接続 |
| Qdrant | dense 768 + sparse BM25、検索、snapshot | 業務ロジック |
| MySQL Reader | consistent snapshot SELECT | 書込み |

---

## 3. ネットワークと信頼境界

| 接続 | 既定 | 備考 |
|---|---|---|
| Browser ↔ API | `127.0.0.1:8781` | loopback 限定 |
| API ↔ SQLite | ローカルファイル `data/db2rag.db` | WAL |
| API ↔ Qdrant | `127.0.0.1:6333` | Docker Compose |
| Worker ↔ MySQL | 社内 DB ホスト:3306 | READ ONLY ユーザ |
| 外部インターネット | 初回 model 取得後 **遮断可** | オフライン運用 |

**LAN モード（GA）**: `--bind 0.0.0.0` + TLS（OQ-005）。API key 必須。

---

## 4. 正常フロー（変換ジョブ）

```
1. UI: source 登録（MySQL 接続 or ファイル D&D）
2. API: job 作成 → SQLite status=pending
3. CPU Worker: discover → snapshot → extract → normalize
4. CPU Worker: parent/child chunk → checkpoint=chunked
5. GPU Worker: dense embed + sparse tokenize → checkpoint=embedded
6. CPU Worker: Qdrant upsert（staging collection）
7. CPU Worker: verify（件数・sample search）
8. API: alias 切替 → status=active
9. UI: 完了表示 + 容量実測値更新
```

---

## 5. 失敗フロー

| 失敗点 | 挙動 | ユーザー操作 |
|---|---|---|
| MySQL 接続失敗 | job item `failed`、retryable | 接続修正 → item retry |
| パース失敗（単一ファイル） | item `failed`、他は継続 | ファイル修正 → item retry |
| GPU OOM | job `paused`、batch 半減して再開 | 自動（最大 3 回） |
| Qdrant upsert 失敗 | checkpoint から upsert 再開 | 自動 retry |
| verify 失敗 | alias 切替 **しない**、staging 保持 | 調査 → 全件 retry |
| worker クラッシュ | lease 失効（30s）→ 他 worker が再開 | 自動 |

---

## 6. ディレクトリ構成（データ）

```
DB2RAG/
├── data/
│   ├── db2rag.db          # SQLite 制御正本
│   ├── models/            # Ruri + OCR オフラインモデル
│   ├── exports/           # manifest + JSONL + snapshot
│   ├── staging/           # 一時パース成果物
│   └── logs/              # マスク済みログ
├── docker-compose.yml     # Qdrant 1.19.0 固定
└── docs/                  # 本設計書群
```

---

## 7. API 境界（概要）

| グループ | エンドポイント例 | 説明 |
|---|---|---|
| `/api/v1/rags` | CRUD | RAG 論理管理 |
| `/api/v1/rags/{id}/sources` | POST | MySQL / file source 登録 |
| `/api/v1/jobs` | CRUD + cancel/resume | 変換ジョブ |
| `/api/v1/jobs/{id}/items/{item_id}/retry` | POST | item 単位 retry |
| `/api/v1/search` | POST | hybrid 検索プレビュー |
| `/api/v1/export` | POST | manifest + snapshot |
| `/api/v1/progress/{job_id}` | GET/SSE | 進捗ストリーム |

詳細は `14_local_ai_integration.md`。

---

## 8. 同時実行制約

| リソース | 制約 |
|---|---|
| GPU Worker | **1 プロセスのみ**（A6000 排他） |
| CPU Worker | 最大 2 プロセス（パース並列、GPU と独立） |
| Qdrant upsert | 同一 `rag_id` で 1 ジョブのみ（SQLite ロック） |
| MySQL 接続 | source ごと最大 1 接続（unbuffered） |

---

## 9. 関連文書

- `03_data_model.md` — エンティティと状態遷移
- `04_ingestion_pipeline.md` — パイプライン DAG
- `08_vector_db_design.md` — Qdrant 契約
- `15_security_and_ops.md` — セキュリティ境界
