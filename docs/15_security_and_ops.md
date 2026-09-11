# 15 セキュリティ・運用

| 項目 | 内容 |
|---|---|
| **目的** | 接続情報保護、ログマスキング、ACL、バックアップ、削除権限を定義する |
| **対象読者** | 実装者、運用者、セキュリティレビュアー |
| **この文書が決めること** | ネットワーク既定、secret 管理、audit、backup/restore、purge |
| **この文書が決めないこと** | 社内 IAM 全体設計 |

---

## 1. ネットワーク

| モード | Bind | TLS | API Key |
|---|---|---|---|
| 既定（MVP） | `127.0.0.1` | なし | なし |
| LAN（GA） | `0.0.0.0` | 必須（OQ-005） | 必須 |

---

## 2. Secret 管理

| Secret | 保存 | 暗号化 |
|---|---|---|
| MySQL パスワード | SQLite `secrets` テーブル | AES-256-GCM（OS keyring 由来キー） |
| API key（LAN） | SQLite + 環境変数 override | bcrypt hash（検証用） |
| Qdrant API key | 環境変数 | 平文（ローカルファイル permissions 600） |

**ログ禁止**: パスワード、API key、connection string の平文

---

## 3. ログマスキング

| 禁止 | 許可 |
|---|---|
| SQL literal 値 | SQL 構造（列名、テーブル名） |
| 行データ全文 | `record_id` + `row_hash` |
| ファイル絶対パス（LAN） | `object_key` 相対 |
| MySQL パスワード | `***` |
| child chunk 全文 | `child_chunk_id` + `content_hash` |

ログレベル: 本番運用は `INFO`。`DEBUG` は開発時のみ。

---

## 4. ACL Enforcement

| ポイント | 動作 |
|---|---|
| 取り込み | `acl_tag` を source 設定 or 列マッピングで付与 |
| 検索 API | 呼び出し元 `acl_tags[]` ∩ payload `acl_tag` |
| Export | manifest に ACL 一覧を含む。消費側責任 |
| 既定 | file D&D = `private`、MySQL = `internal` |

LAN モードでは **filter 未指定の検索を拒否**（403）。

---

## 5. Parser 隔離

| 対策 | 説明 |
|---|---|
| remote HTML 拒否 | SSRF 防止 |
| symlink 拒否 | path traversal 防止 |
| マクロ Office 拒否 | コード実行防止 |
| Docling サンドボックス | 別プロセス、timeout 5min/file |
| ファイルサイズ上限 | 500 MB |

---

## 6. オフライン Model

| 要件 | 内容 |
|---|---|
| 初回後の外部通信 | 不要（model ローカル cache） |
| model 改ざん検出 | revision hash + golden vector 検証 |
| model 更新 | 手動 wizard、自動更新禁止 |

---

## 7. バックアップ

| 対象 | 方式 | 頻度 |
|---|---|---|
| SQLite | ファイルコピー（WAL checkpoint 後） | 日次 |
| Qdrant | snapshot API | ジョブ完了時 + 日次 |
| Export | 手動 or スケジュール | 週次 |
| Model | `data/models/` コピー | 初回のみ |

### 復元手順

1. Qdrant snapshot restore → collection
2. SQLite ファイル置換
3. alias 整合性チェック
4. golden vector + sample search 5 件

---

## 8. Secure Purge（T-SEC-010）

| ステップ | 動作 |
|---|---|
| 1 | job 停止 |
| 2 | Qdrant collection + alias 削除 |
| 3 | SQLite rag 行 `status=purged`、関連行 cascade 削除 |
| 4 | export ディレクトリ削除 |
| 5 | staging 一時ファイル削除 |
| 6 | audit log に purge 記録（データ内容なし） |

**権限**: OS 管理者 or DB2RAG admin role のみ。

---

## 9. Audit Trail

| イベント | 記録 |
|---|---|
| RAG 作成/削除 | `rag_id`, `created_by`, timestamp |
| ジョブ開始/完了 | `job_id`, `mode`, `item_count` |
| Export | `export_id`, `rag_id` |
| 検索（LAN） | `query_hash`（原文なし）, `rag_id`, `result_count` |
| Security 拒否 | `error_code`, `source_ip` |

保存: SQLite `audit_log` テーブル、90 日保持。

---

## 10. 関連文書

- `07_metadata_schema.md` — ACL
- `14_local_ai_integration.md` — export
- `20_open_questions.md` — OQ-004, OQ-005
