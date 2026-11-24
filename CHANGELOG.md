# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-11-24

### Fixed

- Dify の `score_threshold` を返却スコア（RRF）に適用する。section 絞り込みの前に候補を多めに取る
- MySQL 増分は `>=` と `ORDER BY` にし、カーソルはオブジェクト完了後にだけ進める
- エクスポートは `is_latest=1` だけを出す
- 評価データセットのパスを `testdata/` と `data/eval/` に限定する
- `serve --port` を `DB2RAG_API_PORT` に渡し、Dify 案内 URL のポートを合わせる
- Markdown も文字コードフォールバックで読む

### Added

- Dify 連携ダイアログに SSRF 許可設定（`SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=host.docker.internal`）を表示する
- RAG 作成時の ACL を Dify 未指定時の既定 ACL にする
- 未知の Dify metadata フィールドは 400 にせず warnings で無視する
- LAN で API キーが必要なとき、画面はタブ内の Bearer を送る
- 評価の合否は Hard-negative の 1 位率を見る。`TEST.py` は評価を API で実行する
- 全件走査オプションで、MySQL から消えた行を RAG から外せる
- SQLite schema version を 3 にする

## [0.2.0] - 2026-11-23

### Added

- Dify External Knowledge API `POST /api/v1/dify/retrieval`（Dify 本体は変更しない）
- `DB2RAG_API_KEY` と非 loopback 起動時のフェイルクローズ、LAN 検索の ACL 必須
- MySQL のテンプレート、主キー上書き、READ ONLY カスタム SQL、行単位の増分
- 検索の IDF、絶対スケール RRF、BM25 単独ヒットの救済、親チャンク重複統合、任意 Reranker
- 追記時に既存ポイントを残す。件数不一致では alias を切り替えない
- MySQL パスワードの Fernet 保存と purge 時の secret 削除
- `db2rag eval` と `testdata/eval_baseline.json`（Recall@5 / MRR@10 / nDCG@10 / Zero-result / HN-FP@5）
- 画面の版表示、Dify 連携コピー、MySQL 詳細設定、検索トグル
- `docs/user-stories/` と All_Status ランナー
- RAG 一覧からの削除（secure purge）
- 変換ジョブ表の実行作業列と開始/終了/経過

### Changed

- RAG 一覧のシステム状態を上部に統合
- ジョブ状態ラベルを「モデル起動中 / RAG変換中 / 完了」に変更
- 版の正本を `pyproject.toml` に統一し、`GET /api/v1/version` と CLI `--version` がそれを読む

## [0.1.0] - 2026-09-10

### Added

- FastAPI (`:8781`) + SQLite WAL + 単一 worker（lease 30s / heartbeat 10s）
- Qdrant 1.19 dense 768 + BM25 sparse + クライアント側 RRF
- Ruri-v3-310m FP32（prefix 固定、CPU 時も量子化しない）
- PDF / DOCX / PPTX / HTML / XLSX / CSV / TXT / MD 取り込み
- MySQL 8.4 READ ONLY snapshot と論理文書テンプレート
- 承認済み 5 画面 UI（一覧 / ウィザード / ジョブ / 進捗 / 検索）
- JSONL.zst export と `TEST.py`（P1〜P6、時間計測）
