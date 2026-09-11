# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- RAG 一覧からの削除（secure purge: ジョブ停止、Qdrant collection/alias、SQLite 関連行、uploads/exports）
- 変換ジョブ表の実行作業列（新規/追加タグ + 名称 + job_id）と開始/終了/経過の 3 段表示

### Changed

- RAG 一覧のシステム状態を上部に統合
- ジョブ状態ラベルを「モデル起動中 / RAG変換中 / 完了」に変更

## [0.1.0] - 2026-09-10

### Added

- FastAPI (`:8781`) + SQLite WAL + 単一 worker（lease 30s / heartbeat 10s）
- Qdrant 1.19 dense 768 + BM25 sparse + クライアント側 RRF
- Ruri-v3-310m FP32（prefix 固定、CPU 時も量子化しない）
- PDF / DOCX / PPTX / HTML / XLSX / CSV / TXT / MD 取り込み
- MySQL 8.4 READ ONLY snapshot と論理文書テンプレート
- 承認済み 5 画面 UI（一覧 / ウィザード / ジョブ / 進捗 / 検索）
- JSONL.zst export と `TEST.py`（P1〜P6、時間計測）
