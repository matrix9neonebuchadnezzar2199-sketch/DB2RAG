# 05 チャンキング戦略

| 項目 | 内容 |
|---|---|
| **目的** | 文書種別ごとの Parent-Child 分割規則と数値初期値を定義する |
| **対象読者** | 実装者、QA |
| **この文書が決めること** | target/min/max 文字、overlap、親単位、表・リスト規則 |
| **この文書が決めないこと** | embedding 推論、UI 表示 |

---

## 1. 戦略概要

| 役割 | 戦略 |
|---|---|
| **主** | Parent-Child（child = 検索単位、parent = 文脈復元） |
| **補** | 見出し階層 metadata（`section_path`） |
| **不採用** | Late Chunking、固定 token 単独分割 |

**Ruri token 上限**: 8192 token（モデル上限）。chunk **target は token 上限の 15〜25%** を目安に文字数で近似。

**Tokenizer 推定**: 日本語業務文書は **1 token ≈ 1.8 文字**（初期係数）。

---

## 2. 文書種別パラメータ表

| 文書種別 | parent 単位 | child target 文字 | child min | child max | overlap 文字 | 親 max 文字 |
|---|---|---|---|---|---|---|
| 規程・条例 | `第N章` or `第N節` 見出し | 400 | 200 | 600 | 80 | 8,000 |
| マニュアル | `##` 相当見出し | 450 | 250 | 700 | 90 | 10,000 |
| 議事録 | 議題単位 or 発言者ブロック | 350 | 150 | 550 | 60 | 6,000 |
| FAQ | 1 Q&A ペア | 300 | 100 | 450 | 40 | 2,000 |
| MySQL 論理 record | 1 record | 380 | 150 | 550 | 0 | 4,000 |
| XLSX/CSV 行 | 1 行（ヘッダ繰返し） | 250 | 80 | 400 | 0 | 1,500 |
| PDF 表 | 1 表（ヘッダ+最大20行/子） | 350 | 150 | 500 | 50 | 5,000 |
| 一般 DOCX | 見出し階層レベル2 | 420 | 200 | 650 | 85 | 9,000 |
| TXT/MD | `#` 見出し or 空行段落 | 400 | 200 | 600 | 80 | 8,000 |

---

## 3. 分割アルゴリズム（Child）

```
1. parent テキストを取得
2. 段落（\n\n）でセグメント分割
3. セグメント結合: target 文字まで貪欲結合
4. max 超過時: 句点（。．！？）境界で分割
5. 句点なし: 読点（、，）境界
6. それでも超過: 文字数 hard split（max）
7. overlap: 前 child 末尾 overlap 文字を次 child 先頭に重複
8. min 未満 child は前後と結合（表・FAQ 除く）
```

**Hard limit**: child は **Ruri 512 token 相当（≈900 文字）を超えない**（prefix 込みで 550 token 以内を推奨）。

---

## 4. Parent 生成規則

| 規則 | 説明 |
|---|---|
| 見出し開始 | 新 parent 開始、`section_path` 更新 |
| 親 max 超過 | 同一 section 内で parent 分割（`-part2` suffix を metadata に） |
| 表 | 表全体を 1 parent。20 行超は child 複数（ヘッダ各行繰返し） |
| リスト | 親項目 + 子項目を同一 parent。child は項目単位 |
| 脚注 | 直前 parent に付属。独立 parent にしない |

---

## 5. 表の扱い

```
| 列A | 列B |
|-----|-----|
| 値1 | 値2 |
```
→ child text:
```
【表: {section_path}】
列: 列A, 列B
行1: 列A=値1, 列B=値2
```

- 結合セル: 左上の値を展開、結合範囲を `extra.merged_cells` に記録
- 20 行超: 20 行ごとに child 分割、各 child にヘッダ行を前置

---

## 6. MySQL 論理文書

**列連結禁止**。テンプレート例:

```markdown
# {title}（{record_id}）

## 基本情報
- 顧客名: {customer_name}
- 契約日: {contract_date}

## 詳細
{description_long_text}

## ステータス
コード {status_code} → {status_label}
```

- コード値列は `コード {code} → {label}` 形式で人間可読化
- 長文列のみ child 分割対象。メタ列は parent 先頭に集約

---

## 7. 日本語 RAG アンチパターン（禁止）

| # | アンチパターン | 本設計の対策 |
|---|---|---|
| AP-01 | 固定 800 token 機械分割 | 文書種別 target + 句点境界 |
| AP-02 | MySQL 列を `\|` 連結 | テンプレート論理文書 |
| AP-03 | 見出しを捨てる | `section_path` 必須 |
| AP-04 | 表をプレーンテキスト化 | 構造化表記 + ヘッダ繰返し |
| AP-05 | 脚注・注釈の無視 | parent 内に保持 |
| AP-06 | 英語 chunker を日本語に流用 | 句読点境界 + Sudachi 系トークン |
| AP-07 | parent なし child のみ | Parent-Child 必須 |
| AP-08 | OCR 全文一律適用 | 低品質ページのみ |
| AP-09 | 重複 embed 無制限 | content_hash dedup |
| AP-10 | クエリと doc で同一 prefix | Ruri prefix 厳守 |

---

## 8. 関連文書

- `06_embedding_strategy.md` — token budget
- `09_mysql_connector.md` — record template
- `10_file_ingest.md` — CanonicalDocument 入力
