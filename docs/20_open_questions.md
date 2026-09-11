# 20 未決事項（外部依存のみ）

| 項目 | 内容 |
|---|---|
| **目的** | DB2RAG 設計の外部事実依存のみを記録し、内部設計の TBD を残さない |
| **対象読者** | プロダクトオーナー、AISSS チーム、DB2RAG 実装者 |
| **この文書が決めること** | 外部 OQ の暫定動作、決定者、証拠、Gate、未決時影響 |
| **この文書が決めないこと** | DB2RAG 内部実装の未決（内部は全て他文書で決定済み） |

---

> **規約**: 本ファイルに載せるのは **DB2RAG 単独では決められない外部事実** のみ。chunk サイズ・Qdrant パラメータ等の内部設計 TBD は禁止（`05_chunking_strategy.md` 等に記載済み）。

---

## OQ-001: AISSS 外部 retrieval provider 契約

| 項目 | 内容 |
|---|---|
| **質問** | AISSS が DB2RAG 検索 API を正式な external retrieval provider として取り込む API 契約・認可モデルはいつ確定するか |
| **暫定安全動作** | DB2RAG は standalone 検索 API + JSONL export を提供。AISSS 側統合は手動 |
| **決定者** | AISSS プロダクトオーナー + DB2RAG PO |
| **必要証拠** | AISSS `24-external-data-integration-rag.md` の Phase 3 generic_db / external provider 仕様確定版 |
| **期限 Gate** | GA リリース 60 日前 |
| **未決時の影響** | AISSS チャットからの直接 RAG 参照は不可。export 再 ingest または API 手動配線が必要 |

---

## OQ-002: AISSS embedding モデル将来変更

| 項目 | 内容 |
|---|---|
| **質問** | AISSS が将来 Ruri 以外の embedding に変更した場合、DB2RAG export の再 ingest ポリシーは誰が承認するか |
| **暫定安全動作** | DB2RAG は Ruri 固定。AISSS 側はテキスト + metadata のみ再 ingest（ベクトルは AISSS 側で再計算） |
| **決定者** | AISSS アーキテクト |
| **必要証拠** | AISSS embedding 変更時の reindex 手順（Obsidian `aisss-embedding-model-change-reindex.md` 運用実績） |
| **期限 Gate** | AISSS embedding 変更告知時 |
| **未決時の影響** | Qdrant snapshot 直読み連携は不可。JSONL テキスト再 ingest が唯一の安全経路 |

---

## OQ-003: 社内 MySQL バージョン標準

| 項目 | 内容 |
|---|---|
| **質問** | 本番 MySQL の標準バージョンは 8.4 LTS か 9.x か（部門差異の統一） |
| **暫定安全動作** | connector は 8.4 / 9.7 両対応で実装。接続テストでバージョン警告表示 |
| **決定者** | インフラチーム |
| **必要証拠** | 全接続先 DB のバージョン一覧 |
| **期限 Gate** | MVP 完了 30 日前 |
| **未決時の影響** | SQL 方言差（`ONLY_FULL_GROUP_BY` 等）のテストカバレッジが不足する可能性 |

---

## OQ-004: 社内 ACL / 権限ソース・オブ・トゥルース

| 項目 | 内容 |
|---|---|
| **質問** | `acl_tag` の正本は Active Directory グループか、MySQL 業務テーブルか、手動設定か |
| **暫定安全動作** | 取り込み時に手動 `acl_tag` 指定 + MySQL 列マッピング（任意）。検索時 filter 必須オプション |
| **決定者** | 情報セキュリティ + 各業務部門 PO |
| **必要証拠** | 既存 AISSS `viewing_range_ids` 運用との整合性レビュー |
| **期限 Gate** | GA リリース 45 日前 |
| **未決時の影響** | 過剰公開リスク。既定は `acl_tag=private` + filter 必須で運用 |

---

## OQ-005: LAN 公開時の TLS 証明書供給

| 項目 | 内容 |
|---|---|
| **質問** | loopback 以外（LAN 共有）時の TLS 証明書は社内 CA か自己署名か |
| **暫定安全動作** | 既定 loopback only。LAN 有効化時は自己署名 + 初回 fingerprint 確認 UI |
| **決定者** | インフラ / セキュリティチーム |
| **必要証拠** | 社内 Web サービス TLS 標準 |
| **期限 Gate** | LAN モード GA 時 |
| **未決時の影響** | LAN 公開は自己署名のみ（ブラウザ警告あり） |

---

## OQ-006: Docling 日本語 OCR モデル配布経路

| 項目 | 内容 |
|---|---|
| **質問** | RapidOCR 日本語モデルの社内配布・更新承認フロー |
| **暫定安全動作** | 初回セットアップ wizard でオフライン model バンドル同梱。更新は手動 |
| **決定者** | DB2RAG PO + セキュリティ（外部モデル取込承認） |
| **必要証拠** | モデルライセンス確認（Apache/MIT 等） |
| **期限 Gate** | MVP リリース前 |
| **未決時の影響** | OCR 品質ページは `ocr_skipped` フラグで取り込み、手動再処理 |

---

## OQ 管理ルール

1. 新規 OQ は PO 承認後のみ追加（内部 TBD の逃げ道にしない）
2. 決定後は該当設計文書へ反映し、本ファイルの OQ を **Resolved** セクションへ移動
3. Gate 期限超過時は暫定安全動作で GA をブロックするか PO が明示 waiver

## Resolved（空）

決定済み OQ はここへ移動する。
