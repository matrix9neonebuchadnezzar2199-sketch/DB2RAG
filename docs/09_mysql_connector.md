# 09 MySQL コネクタ

| 項目 | 内容 |
|---|---|
| **目的** | MySQL 業務 DB からの READ ONLY 取り込み、論理文書化、差分同期を定義する |
| **対象読者** | 実装者、DBA |
| **この文書が決めること** | 接続、スナップショット、cursor、テンプレート、row hash、JOIN |
| **この文書が決めないこと** | MySQL サーバ設定変更、書込み |

---

## 1. 対応バージョン

| MySQL | サポート | 備考 |
|---|---|---|
| 8.4 LTS | ✓ 推奨 | 社内標準候補 |
| 9.7 LTS | ✓ | 新規環境 |
| 8.0.x | ✓ 限定的 | 接続時 warning |
| 5.7 | ✗ | 非サポート |

Connector: `mysql-connector-python` ≥ 9.0

---

## 2. 接続設定

| パラメータ | 値 |
|---|---|
| ユーザ | READ ONLY 専用（`SELECT` のみ GRANT） |
| `autocommit` | `False` |
| `charset` | `utf8mb4` |
| `use_unicode` | `True` |
| `connection_timeout` | 30s |
| `read_timeout` | 3600s（大テーブル） |
| SSL | 社内 DB は `REQUIRED`（証明書パス設定可） |

**接続テスト（T-CONN-001）**: `SELECT 1` + `SHOW VARIABLES LIKE 'version'` + 書込み拒否確認

---

## 3. Consistent Snapshot

```python
# 概念フロー
conn.start_transaction(isolation_level='REPEATABLE READ', readonly=True)
cursor = conn.cursor(buffered=False)  # unbuffered
cursor.execute("SELECT ... FROM large_table ...")
for row in cursor:
    process(row)
conn.commit()  # read-only commit で snapshot 解放
```

| 要件 | 説明 |
|---|---|
| READ ONLY | `START TRANSACTION READ ONLY` |
| Unbuffered | 行単位 fetch、メモリ OOM 防止 |
| 単一接続 | source あたり 1 接続 |

---

## 4. 取り込み対象

| 種別 | 設定 | object_key |
|---|---|---|
| テーブル | schema + table + PK 列指定 | `{schema}.{table}` |
| ビュー | schema + view | `{schema}.{view}` |
| SQL | 保存済み SELECT（パラメータなし） | `sql:{sql_id}` |

---

## 5. SQL 制限

| 許可 | 禁止 |
|---|---|
| `SELECT` | `INSERT/UPDATE/DELETE` |
| `WITH` (CTE) | ストアドプロシージャ呼出し |
| `JOIN` | 複数ステートメント (`;`) |
| `WHERE` | `INTO OUTFILE` |
| サブクエリ | `FOR UPDATE` |

| 制限 | 値 |
|---|---|
| 最大行数 / SQL | 500,000（超過で warning + 上限で truncate） |
| 実行タイムアウト | 30 分 |
| 最大カラム数 | 100 |

---

## 6. 安定 Record Key

```
record_id = "|".join(f"{col}={value}" for col, value in sorted(pk_columns))
```

- PK 未定義テーブルは取り込み **拒否**（`ERR_MYSQL_NO_PK`）
- 複合 PK は列名アルファベット順

---

## 7. Row Hash

```
row_hash = SHA256(
  "|".join(
    f"{col}={normalize(value)}"
    for col in template_column_order
  )
)
```

`normalize(value)`:
- `NULL` → 空文字
- `datetime` → ISO8601 UTC
- `bytes` → base64
- その他 → `str()` + NFKC

---

## 8. 論理文書テンプレート

### 設定 JSON 例

```json
{
  "template_id": "customer_contract_v1",
  "title_column": "contract_name",
  "updated_at_column": "updated_at",
  "sections": [
    {
      "heading": "基本情報",
      "fields": [
        { "label": "顧客名", "column": "customer_name" },
        { "label": "契約日", "column": "contract_date", "format": "date_ja" }
      ]
    },
    {
      "heading": "詳細",
      "fields": [
        { "label": "説明", "column": "description", "type": "long_text" }
      ]
    },
    {
      "heading": "ステータス",
      "fields": [
        { "label": "ステータス", "column": "status_code", "lookup_table": "status_master", "lookup_key": "code", "lookup_label": "label" }
      ]
    }
  ]
}
```

**禁止**: `CONCAT(col1, '|', col2)` による無構造連結

---

## 9. JOIN 定義

| 用途 | 方式 |
|---|---|
| コード値ラベル | `lookup_table` で LEFT JOIN（connector 内部生成） |
| 1:N 子テーブル | 別 object として取り込み、`extra.parent_record_id` で関連 |
| N:M | 中間テーブル経由で 2 object に分割 |

複雑 JOIN は SQL object として定義（1 行 = 1 論理文書）。

---

## 10. 差分同期（T-MYSQL-030）

| モード | 動作 |
|---|---|
| 初回 | 全行スキャン |
| 増分 | `WHERE updated_at > :last_sync`（列必須） |
| row_hash 一致 | skip |
| row_hash 不一致 | re-chunk + re-embed |
| 新規行 | 追加 |

`updated_at` 列がないテーブルは **全件スキャン**（warning 表示）。

---

## 11. エラーコード

| コード | 意味 | Retry |
|---|---|---|
| `ERR_MYSQL_CONN` | 接続失敗 | ✓ |
| `ERR_MYSQL_TIMEOUT` | タイムアウト | ✓ |
| `ERR_MYSQL_NO_PK` | PK なし | ✗ |
| `ERR_MYSQL_FORBIDDEN_SQL` | 禁止 SQL | ✗ |
| `ERR_MYSQL_ROW_LIMIT` | 行数超過 | 部分完了 |

---

## 12. 増分カーソル（v0.2.1）

- 増分列がある表は、初回から `ORDER BY updated_at, pk` で読む。
- 続きは `updated_at >= cursor`。同じ時刻の後書きを落とさない。既存行は row_hash でスキップする。
- `sync_cursor` は、そのオブジェクトのバッチがすべて成功したあとだけ書く。途中失敗では進めない。
- `reconcile_deletes` は全件走査のときだけ、今回見えなかった最新行を外す。

---

## 13. 関連文書

- `04_ingestion_pipeline.md`
- `05_chunking_strategy.md` — MySQL record chunk
- `16_error_handling.md`
