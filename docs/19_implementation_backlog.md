# 19 実装バックログ

| 項目 | 内容 |
|---|---|
| **目的** | 設計→実装の PHASE 分解、依存 DAG、受入試験、Gate、rollback を定義する |
| **対象読者** | 実装者、PM |
| **この文書が決めること** | PHASE 00〜10 の成果物、依存、試験 ID、完了条件 |
| **この文書が決めないこと** | スプリント日付、リソース配分 |

**設計状態**: `IMPLEMENTATION READY`（UIモック承認: 2026-09-10）

---

## 1. PHASE 一覧

```
PHASE 00 ─→ PHASE 01 ─→ PHASE 02 ─→ PHASE 03
                │              │
                └──────→ PHASE 04 ─→ PHASE 05 ─→ PHASE 06
                              │
                              └──→ PHASE 07 ─→ PHASE 08 ─→ PHASE 09 ─→ PHASE 10
```

---

## 2. PHASE 詳細

### PHASE 00: プロジェクト骨格

| 項目 | 内容 |
|---|---|
| 依存 | なし |
| 成果物 | リポジトリ scaffold、`docker-compose.yml`（Qdrant 1.19）、`data/` 構成、README |
| 試験 | — |
| Gate | ディレクトリ構成 + Qdrant 起動確認 |
| Rollback | リポジトリ削除 |

### PHASE 01: SQLite + API 骨格

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 00 |
| 成果物 | SQLite schema、`/api/v1/rags` CRUD、health check |
| 試験 | — |
| Gate | RAG 作成→一覧 API が動作 |
| Rollback | DB migration down |

### PHASE 02: Job Broker + Lease

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 01 |
| 成果物 | job/item テーブル、lease、checkpoint、cancel/resume |
| 試験 | T-ERR-020 |
| Gate | worker クラッシュ後 30s で再開 |
| Rollback | job テーブル truncate |

### PHASE 03: File Ingest（CPU）

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 02 |
| 成果物 | D&D API、Docling parser、TXT/MD、XLSX/CSV adapter、CanonicalDocument |
| 試験 | T-FILE-001〜010 |
| Gate | 5 形式パース成功 |
| Rollback | parser 機能フラグ off |

### PHASE 04: MySQL Connector

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 02 |
| 成果物 | READ ONLY 接続、snapshot cursor、template renderer、row hash |
| 試験 | T-CONN-001, T-MYSQL-001〜030 |
| Gate | テーブル 1000 行取り込み成功 |
| Rollback | mysql source type 無効化 |

### PHASE 05: Chunking

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 03, 04 |
| 成果物 | Parent-Child chunker、文書種別パラメータ、section_path |
| 試験 | 構造完全性（T-EVAL subset） |
| Gate | 規程・表・MySQL record の chunk サンプル目視 OK |
| Rollback | 旧 chunk パラメータに戻す（reindex 必要） |

### PHASE 06: Embedding + Qdrant

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 05 |
| 成果物 | Ruri FP32 worker、BM25 sparse、Qdrant upsert、alias |
| 試験 | T-EMB-001, T-PERF-001, T-VEC-010 |
| Gate | golden vector pass + 1万 chunk upsert |
| Rollback | alias を旧 collection に戻す |

### PHASE 07: Pipeline 統合 + Verify

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 06 |
| 成果物 | 全 DAG 統合、verify、activate、dedup |
| 試験 | T-JOB-001〜005, T-DEDUP-001, T-RAG-001〜002 |
| Gate | E2E: file + mysql → search hit |
| Rollback | staging collection 削除 |

### PHASE 08: UI

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 07 |
| 前提 Gate | **PASS（2026-09-10）** — サイバーテーマ静的HTMLモック承認済み |
| 成果物 | 5画面 SPA、折りたたみ式統合ウィザード、進捗 SSE、検索プレビュー |
| 試験 | T-UI-001〜009, T-PROG-001〜003, T-SEARCH-001〜010 |
| Gate | T-UI-001〜009 pass。S1〜S5とウィザード全4分岐を完走 |
| Rollback | API-only 運用に戻す |

### PHASE 09: Export + Local AI API

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 07 |
| 成果物 | manifest + JSONL.zst + snapshot export、compatibility API |
| 試験 | T-EXPORT-001, T-INT-001 |
| Gate | export → manifest SHA-256 一致 |
| Rollback | export 機能フラグ off |

### PHASE 10: Security + Eval Gate

| 項目 | 内容 |
|---|---|
| 依存 | PHASE 08, 09 |
| 成果物 | ACL、audit、secure purge、eval runner、LAN TLS |
| 試験 | T-SEC-010, T-SEC-020, T-ACL-001, T-EVAL-001 |
| Gate | **18_test_and_eval.md G1〜G9 全 pass** |
| Rollback | GA フラグ off、MVP モード維持 |

---

## 3. TEST.py 能力 Probe

実装完了時、`TEST.py` は以下を自動検証:

| Probe | 内容 |
|---|---|
| P1 | Qdrant 接続 + version == 1.19.x |
| P2 | Ruri model load + golden vector |
| P3 | SQLite migration version |
| P4 | Sample file ingest → search hit |
| P5 | API health `/api/v1/health` |
| P6 | UI 5 routes + wizard state contract（W1 explicit / W2 auto） |

---

## 4. 依存 DAG（テキスト）

```
00 → 01 → 02 ─┬→ 03 ─┐
              └→ 04 ─┤
                     ↓
                    05 → 06 → 07 ─┬→ 08 → 10
                                  └→ 09 ──┘
```

PHASE 03 と 04 は並行可能。

---

## 5. カタログ・ドキュメント

| タイミング | 作業 |
|---|---|
| PHASE 00 完了 | `.cursor/カタログ.html` 登録 |
| PHASE 10 完了 | CHANGELOG v0.1.0 |
| 設計完了 | 本 `docs/` 21文書 + 承認済み5画面モック（完了済み） |

---

## 6. 実装引き渡し契約

| 項目 | 固定内容 |
|---|---|
| 実装開始点 | PHASE 00。現在は実装コード未作成 |
| UI正本 | `11_ui_ux.md` + `mockup.html`から始まる承認済み5画面 |
| 画面構成 | RAG一覧 / 統合ウィザード / ジョブ一覧 / 進捗 / 検索プレビュー |
| ウィザード | W1〜W5、新規/既存 × MySQL/ファイルの4分岐 |
| W1 | 選択だけでは進めない。RAG名/追加先を確認して明示確定 |
| W2 | MySQL/ファイル選択で次へ自動進行 |
| 再編集 | 完了帯の▼で展開。変更時は後続を再確認 |
| 禁止 | 独立INGESTIONナビ、独立MySQL接続/対象/ファイル投入ページ |
| 変更管理 | UI構成変更はモック・FR・T-UI・ADR-011を更新し再承認 |

実装者は承認済みモックの画面構成を変更せず、API/状態管理をその構成へ落とす。

---

## 7. 関連文書

- `00_overview.md`
- `18_test_and_eval.md`
- `01_requirements.md`
