# 16 エラーハンドリング

| 項目 | 内容 |
|---|---|
| **目的** | checkpoint 復旧、retry 分類、lease 回収、部分完了を定義する |
| **対象読者** | 実装者、運用者 |
| **この文書が決めること** | エラー分類、retry 上限、取消・再開、lease |
| **この文書が決めないこと** | 個別 parser 実装 |

---

## 1. エラー分類

| クラス | 説明 | 例 | 自動 Retry |
|---|---|---|---|
| `retryable` | 一時障害 | DB 接続断、GPU OOM（半減後） | ✓ |
| `permanent` | 入力/設定不正 | PK なし、禁止 SQL | ✗ |
| `security` | セキュリティ違反 | 書込み SQL 検出、path traversal | ✗ + audit log |
| `partial` | 一部 item 失敗 | 10 文件中 1 ファイル破損 | 他は継続 |

---

## 2. Retry ポリシー

| レベル | 最大回数 | Backoff |
|---|---|---|
| JobItem | 5 | 指数: 2s, 4s, 8s, 16s, 32s |
| Job（全体） | 3 | 30s 固定 |
| GPU OOM batch 半減 | 3 段階 | 即時 |

**ユーザー手動 retry**: 回数リセット、checkpoint から再開

---

## 3. Checkpoint 復旧

| checkpoint | 再開時の動作 |
|---|---|
| `discovered` | extract から |
| `extracted` | normalize から |
| `normalized` | chunk から |
| `chunked` | embed から |
| `embedded` | upsert から |
| `upserted` | verify から |
| `verified` | activate のみ |

checkpoint は SQLite に **ステージ完了後**即時 commit。

---

## 4. Lease 方式

| パラメータ | 値 |
|---|---|
| TTL | 30s |
| Heartbeat | 10s 間隔 |
| 取得 | `UPDATE job SET lease_owner=?, lease_expires_at=? WHERE lease_owner IS NULL OR lease_expires_at < now()` |
| 解放 | 正常完了 or heartbeat 停止後 30s で失効 |

**競合**: 同一 job に 1 worker のみ lease 可能。

---

## 5. 部分完了

| 条件 | 動作 |
|---|---|
| 一部 item failed | job `status=completed_with_errors` |
| verify 成功分 | activate 実行（成功 item のみ） |
| failed item | UI で一覧 + 個別 retry |

---

## 6. 取消・再開

| 操作 | 動作 |
|---|---|
| Cancel | `status=cancelled`、lease 解放、staging GC |
| Pause | GPU OOM 等で自動。`status=paused` |
| Resume | checkpoint から再開、lease 再取得 |

---

## 7. Batch 二分（GPU OOM）

```
1. batch_size N で OOM
2. N/2 で再試行（最大 3 回）
3. それでも OOM → job paused + 通知
4. FP16/INT8 降格は行わない
```

---

## 8. ログとマスキング

| 禁止ログ | 代替 |
|---|---|
| MySQL パスワード | `***` |
| 行の全文（PII） | `record_id` + hash のみ |
| SQL literal 値 | 列名のみ |
| ファイル絶対パス（LAN） | `object_key` 相対パスのみ |

---

## 9. エラー UI 表示

| フィールド | 内容 |
|---|---|
| `error_code` | 機械可読 |
| `error_message` | ユーザ向け（日本語、マスク済み） |
| `retryable` | bool |
| `checkpoint` | 再開地点 |
| `suggestion` | 推奨操作（例: 「PDF を OCR 対応で再保存」） |

---

## 10. 関連文書

- `04_ingestion_pipeline.md`
- `09_mysql_connector.md`, `10_file_ingest.md`
- `15_security_and_ops.md`
