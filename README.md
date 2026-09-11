# DB2RAG

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%2011-lightgrey.svg)](https://github.com/matrix9neonebuchadnezzar2199-sketch/DB2RAG)
[![Qdrant](https://img.shields.io/badge/Qdrant-1.19-DC244C.svg)](https://qdrant.tech/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Status](https://img.shields.io/badge/status-active-brightgreen.svg)](CHANGELOG.md)

社内 MySQL とローカル文書を、**ローカル永続 RAG 知識ベース**へ変換するツールです。チャット UI・外部 Embedding API・クラウド Vector DB は持ちません。

取り込みから Parent-Child chunk、日本語 Embedding（Ruri-v3-310m FP32）、Qdrant 1.19 の hybrid 検索（dense + BM25 + RRF）、検索プレビュー、JSONL.zst export、secure purge までを単一 worker で処理します。

## 読者別導線

| 役割 | 最初に読む |
|------|------------|
| 使う人 | [インストール](#インストール) → [使用例](#使用例) → [画面](#画面) → [終了](#終了) |
| 運用する人 | [動作環境](#動作環境) → [設定](#設定) → [トラブルシューティング](#トラブルシューティング) |
| 開発する人 | [概要](#概要) → [アーキテクチャ](#アーキテクチャ) → [検証](#検証) → [設計書](#設計書) |

## 目次

- [概要](#概要)
- [アーキテクチャ](#アーキテクチャ)
- [動作環境](#動作環境)
- [インストール](#インストール)
- [使用例](#使用例)
- [画面](#画面)
- [API](#api)
- [CLI](#cli)
- [設定](#設定)
- [終了](#終了)
- [検証](#検証)
- [トラブルシューティング](#トラブルシューティング)
- [設計書](#設計書)
- [ライセンス](#ライセンス)

## 概要

DB2RAG は「業務データを検索可能な知識ベースにする」ことに特化しています。LLM に聞けるチャットではなく、**変換パイプライン + 検索プレビュー + 可搬 export** です。

| やること | やらないこと |
|----------|--------------|
| ファイル / MySQL の取り込みと変換 | チャット UI |
| Parent-Child chunk と Ruri embedding | LLM 学習 / fine-tune |
| Qdrant hybrid 検索プレビュー | クラウド Vector DB |
| JSONL.zst export（他システム再 ingest 用） | OpenAI embeddings 互換 API |
| RAG の削除（secure purge） | 本番向け reranker |

処理の流れ:

```text
Source 登録 → Job 作成 → discover → extract → normalize
  → parent/child chunk → Ruri embed + BM25 → Qdrant upsert
  → verify → alias activate → 検索プレビュー / Export / Purge
```

## アーキテクチャ

```text
Browser  ──REST──►  FastAPI (:8781)
                      │
          ┌───────────┼────────────┐
          ▼           ▼            ▼
      SQLite WAL   Worker×1     Qdrant 1.19
      (制御正本)   Ruri FP32    dense 768 + BM25
                      │
                      ▼
                 MySQL READ ONLY snapshot
```

| 層 | 技術 | 固定値 |
|----|------|--------|
| UI | 静的 HTML + `live.js`（サイバーテーマ） | 同一オリジン `:8781` |
| API | FastAPI / Uvicorn | 既定 `127.0.0.1:8781` |
| 制御 DB | SQLite WAL | `data/db2rag.db` |
| Vector DB | Qdrant 1.19.0 | Docker `:6333`。未起動時は埋め込み Qdrant に縮退 |
| Embedding | `cl-nagoya/ruri-v3-310m` | revision `18b60fb8c2b9df296fb4212bb7d23ef94e579cd3`、768dim、**FP32（量子化しない）** |
| Prefix | 文書 / クエリ | `検索文書: ` / `検索クエリ: ` |
| 検索融合 | RRF | prefetch 40+40、k=60 |
| Worker | 単一 GPU/CPU + lease | TTL 30s / heartbeat 10s |

ベクトルは Qdrant だけが持ちます。SQLite は RAG / source / job / chunk メタデータと checkpoint の正本です。

## 動作環境

### 必須

- **Python**: 3.12（`.python-version`。3.11 以上）
- **OS**: テスト済み Windows 11
- **[uv](https://docs.astral.sh/uv/)**: 依存同期
- **ディスク**: 初回に Ruri モデル（約 1GB 超）を Hugging Face から取得

### 外部ツール

- **Docker 24+**（推奨）: Qdrant 1.19.0 と試験用 MySQL 8.4
- Docker が無い / 未起動のときは埋め込み Qdrant に縮退します。検索は動きますが、本番相当の永続化は Docker 側を使ってください

### GPU

- CUDA があれば `cuda:0`
- 無ければ **CPU FP32**。WARN は出しますが、量子化へは落としません

### Python 依存

詳細は [`pyproject.toml`](pyproject.toml)。主要なもの:

- FastAPI / Uvicorn / Pydantic
- qdrant-client / mysql-connector-python
- sentence-transformers / torch
- pypdf / python-docx / python-pptx / openpyxl

## インストール

1. **リポジトリを取得する**

```powershell
git clone https://github.com/matrix9neonebuchadnezzar2199-sketch/DB2RAG.git
cd DB2RAG
```

2. **Python 依存を同期する**

```powershell
uv sync
```

3. **試験データとコンテナを用意する**（任意だが推奨）

```powershell
uv run db2rag testdata
docker compose up -d
```

4. **動作確認する**

```powershell
uv run python TEST.py --skip-docker
```

初回起動時、Hugging Face から Ruri を取得します。オフラインにする場合はモデル取得後に `DB2RAG_HF_OFFLINE=true` を使います。

## 使用例

### 最小例

```powershell
uv run db2rag serve
```

ブラウザで [http://127.0.0.1:8781/](http://127.0.0.1:8781/) を開きます。API と UI は同じプロセスです。

### 典型例

ファイルから知識ベースを作り、検索する:

```powershell
uv run db2rag testdata
docker compose up -d
uv run db2rag serve
```

1. 画面「新規・追加」で RAG 名称と ACL を決める
2. ファイルをドロップ、または試験用 MySQL `127.0.0.1:3307` に接続する
3. 「変換ジョブ」で進捗を見る（状態は モデル起動中 → RAG変換中 → 完了）
4. 「検索プレビュー」でクエリを打つ

CLI からファイル投入する場合:

```powershell
uv run db2rag ingest-files --name "社内規程" testdata/files
```

### 応用例

検索 API（score は類似度であり確率ではありません）:

```powershell
uv run python -c "import httpx; r=httpx.post('http://127.0.0.1:8781/api/v1/search', json={'rag_id':'rag_...','query':'有給休暇の申請期限','top_k':10,'score_threshold':0.0,'filters':{'acl_tags':['internal']}}); print(r.json())"
```

Export:

```powershell
uv run python -c "import httpx; print(httpx.post('http://127.0.0.1:8781/api/v1/export', json={'rag_id':'rag_...'}).json())"
```

試験用 MySQL（`docker compose` が作る lab ユーザ。本番の認証情報ではない）:

| 項目 | 値 |
|------|-----|
| host / port | `127.0.0.1` / `3307` |
| user / password | `db2rag_ro` / `db2rag_ro` |
| database | `hr` |
| ssl | `DISABLED` |

読み取り専用ユーザです。root パスワード `db2rag_lab` は **ラボ専用**で、公開ネットワークに出さないでください。

## 画面

UI は承認済みサイバーテーマです。実装とモックは `web/` にあります。

| パス | 画面 | できること |
|------|------|------------|
| `/` | RAG 一覧 | 知識ベースの状態、検索 / 出力 / 削除 |
| `/wizard` | 新規・追加 | 名称・ACL・ファイルまたは MySQL |
| `/jobs` | 変換ジョブ | 実行作業、状態、開始/終了/経過、詳細ホバー |
| `/search` | 検索プレビュー | hybrid 検索、ACL、親文脈 |
| `/progress` | — | `/jobs` へ 307 |

削除は確認ダイアログのあと `DELETE /api/v1/rags/{id}` です。Qdrant・SQLite 関連行・`data/uploads` と `data/exports` を落とします。ウィザードで指定した **uploads 以外の元ファイルは残します**。復元できません。

OCR・下書き保存・評価セット保存は未実装です。画面上は disabled です。

## API

ベース URL: `http://127.0.0.1:8781`

| メソッド | パス | 用途 |
|----------|------|------|
| GET | `/api/v1/health` | SQLite / Qdrant / GPU |
| GET | `/api/v1/rags` | RAG 一覧（`purged` 除外） |
| GET | `/api/v1/rags/{id}` | RAG 詳細 |
| DELETE | `/api/v1/rags/{id}` | secure purge |
| POST | `/api/v1/jobs` | 変換ジョブ作成 |
| GET | `/api/v1/jobs` | ジョブ一覧 |
| GET | `/api/v1/jobs/{id}` | ジョブ + items |
| POST | `/api/v1/jobs/{id}/cancel` | 取消 |
| POST | `/api/v1/jobs/{id}/resume` | 再開 |
| POST | `/api/v1/search` | hybrid 検索 |
| POST | `/api/v1/mysql/test` | MySQL 接続試験 |
| POST | `/api/v1/mysql/objects` | テーブル/ビュー一覧 |
| POST | `/api/v1/files/upload` | ブラウザからのファイル投入 |
| POST | `/api/v1/export` | JSONL.zst + manifest |
| POST | `/api/v1/embed/golden` | Ruri golden vector |
| POST | `/api/v1/capacity/estimate` | ファイル件数からの容量見積 |

リクエスト／レスポンスの型は OpenAPI（`/docs`）を見てください。

## CLI

```powershell
uv run db2rag --help
uv run db2rag serve --help
```

| コマンド | 用途 |
|----------|------|
| `serve` | API + UI + worker。既定 `127.0.0.1:8781` |
| `worker` | worker だけ（通常は serve 内で動く） |
| `testdata` | SQL / PDF / XLSX / CSV / DOCX / MD の試験データ |
| `ingest-files` | パスからジョブを切る |
| `schema-version` | SQLite schema 版 |

全フラグは `--help` が正本です。

## 設定

環境変数は `DB2RAG_` プレフィックスです。

| 変数 | 既定 | 意味 |
|------|------|------|
| `DB2RAG_DATA_DIR` | `data/` | SQLite / モデル / uploads / exports |
| `DB2RAG_HOST` | `127.0.0.1` | bind。`0.0.0.0` は `DB2RAG_ALLOW_BIND_ALL=true` が必要 |
| `DB2RAG_API_PORT` | `8781` | API+UI |
| `DB2RAG_QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant HTTP |
| `DB2RAG_QDRANT_PATH` | 空 | 指定時は埋め込み Qdrant |
| `DB2RAG_EMBEDDING_BACKEND` | `ruri` | `hash` は単体試験用（検索品質は見ない） |
| `DB2RAG_HF_OFFLINE` | `false` | モデル取得後のオフライン |
| `DB2RAG_ALLOW_BIND_ALL` | `false` | loopback 以外への bind 許可 |

`data/` は git 管理外です。ベクトル・SQLite・アップロードをリポジトリに混ぜません。

## 終了

- `db2rag serve` は **Ctrl+C** で止めます。**ブラウザタブを閉じてもサーバは残ります**
- Docker: `docker compose down`。volume まで消すときだけ `-v` を明示します
- 停止後、ポート **8781**（API）と **6333**（Qdrant）が空いていることを確認します

```powershell
Get-NetTCPConnection -LocalPort 8781,6333 -ErrorAction SilentlyContinue |
  Select-Object LocalPort, State, OwningProcess
```

残っていたら、該当 PID を止めてから再起動してください。二重起動するとポートが衝突します。

## 検証

単位試験:

```powershell
uv run python -m pytest -q
```

運用プローブ（Qdrant / Ruri golden / ingest → search / UI ルート）:

```powershell
uv run python TEST.py
uv run python TEST.py --skip-docker
uv run python TEST.py --hash-embed
```

| Probe | 見るもの |
|-------|----------|
| P1 | Qdrant 1.19 |
| P2 | Ruri golden vector |
| P3 | SQLite schema |
| P4 | ファイル（と任意で MySQL）ingest → 検索 |
| P5 | `/api/v1/health` |
| P6 | `/` `/wizard` `/jobs` `/search` とウィザード契約 |

`--hash-embed` は Ruri 無しの煙テストです。検索品質は見ません。

## トラブルシューティング

| 症状 | 原因 | 対処 |
|------|------|------|
| Qdrant に繋がらない | Docker 未起動 | Docker Desktop を起動し `docker compose up -d` |
| `TEST.py` の P2 golden 失敗 | モデル revision / prefix 不一致 | revision `18b60fb8…` と `検索文書: ` / `検索クエリ: ` を変えない |
| GPU WARN | CUDA が無い | 仕様です。CPU FP32 のまま使う |
| 検索 0 件 | ACL フィルタ、alias 切替前の失敗ジョブ | ACL を合わせる。ジョブが完了しているか見る |
| MySQL 1045 | パスワード誤り、または `caching_sha2` の平文 full auth | 試験用は `db2rag_ro`。再起動直後は TLS 再試行が入る |
| ポート 8781 使用中 | 前の `serve` が残っている | Ctrl+C または PID を止める。タブを閉じただけでは止まらない |
| 削除できない / 404 | 古い JS キャッシュ、または既に purged | `/` を Ctrl+F5。一覧に出ない RAG は削除済み |
| 変換が「モデル起動中」のまま | embedder 初回ロード | 初回はウェイト読み込みに時間がかかる |

### デバッグ

1. `/api/v1/health` の `warnings` を見る
2. ジョブ詳細ホバーで item の error_code を見る
3. `uv run python TEST.py --hash-embed` でパイプラインだけ切る

## 設計書

正本は `docs/` の 21 文書です。入口は [`docs/00_overview.md`](docs/00_overview.md)。

| # | 文書 | 内容 |
|---|------|------|
| 00 | `00_overview.md` | 全体要約 |
| 01 | `01_requirements.md` | FR / NFR |
| 02 | `02_architecture.md` | コンポーネントと信頼境界 |
| 03–10 | chunk / embed / Qdrant / MySQL / ファイル | 変換の詳細 |
| 11–13 | UI / 進捗 / 検索 | 画面と検索 |
| 14–16 | 連携 / セキュリティ / エラー | export、purge、lease |
| 17 | `17_adr.md` | 設計決定 |
| 18–20 | 試験 / backlog / 未決 | Gate と残課題 |

実装と設計が食い違う箇所（例: UI ポートは文書上 8780、実装は **8781 に UI も載せる**）は **実装を正** としてください。

## ライセンス

MIT License. See [LICENSE](LICENSE).

Embedding モデル `cl-nagoya/ruri-v3-310m` は上流のライセンスに従ってください。

## GitHub Topics

推奨タグ: `rag`, `qdrant`, `fastapi`, `mysql`, `embedding`, `japanese`, `sqlite`, `hybrid-search`
