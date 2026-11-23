# 21 DB2RAG v0.2.0 完成度評価・全体改修設計・Phase別実装指示書

| 項目 | 内容 |
|---|---|
| **目的** | `v0.1.0` のコードベース実測に基づく完成度評価、不足アルゴリズム・欠陥の特定、および Grok ワーカー向けの Phase 別詳細実装仕様（Plan 01〜06）を定義する |
| **対象読者** | マスター（承認・優先順位判断）、Architect（設計・管理レビュー）、Builder / Grok（Phase 別実装） |
| **作成日** | 2026-11-23 |
| **対象バージョン** | `v0.1.0` → `v0.2.0` |
| **この文書が決めること** | 監査結果、全体改修アーキテクチャ、Phase 1〜6 の変更範囲・API/DB契約・アルゴリズム・テスト・完了条件 |
| **この文書が決めないこと** | `H:\CURSOR\dify` 本体の改修、チャット UI / 生成 LLM 機能、Ruri モデル変更 |

---

## 0. 開発ブリーフ（6 項目）

| # | 項目 | 内容 |
|---|---|---|
| 1 | **目的** | `v0.1.0` の致命的欠陥（`append` 時のデータ消失・平文秘密情報・SQLite スレッド競合・BM25 切り捨て）を根治し、MySQL 業務 DB および日本語文書の構造化 RAG 精度を引き上げ、**Dify 外部知識 API（External Knowledge API）として直接接続できる完成版（`v0.2.0`）**へ改修する |
| 2 | **repo 構成** | `H:\CURSOR\DB2RAG`（Python 3.11+ / FastAPI / SQLite WAL / Qdrant 1.19 / `cl-nagoya/ruri-v3-310m` FP32 / `web/` 静的 HTML+JS / `tests/` + `TEST.py`） |
| 3 | **変更範囲** | **触ってよい**: `src/db2rag/**`, `web/**`, `tests/**`, `TEST.py`, `pyproject.toml`, `CHANGELOG.md`, `README.md`, `docs/user-stories/**`, `scripts/**`<br>**触らない**: `H:\CURSOR\dify/**`（参照のみ）, `data/` 配下の既存ユーザーデータ, `constants.py` の Ruri モデル ID・リビジョン（`18b60fb8c2b9df296fb4212bb7d23ef94e579cd3`）・次元（`768`）・FP32 ポリシー・`PREFIX_DOC` / `PREFIX_QUERY` |
| 4 | **完了条件** | ① `append` で既存チャンクが保持され、SQLite 最新有効 child 数 == Qdrant point 数が verify で保証される<br>② MySQL パスワードが `secrets` テーブルに暗号化保存され、大テーブルが $O(1)$ メモリでストリーミング取込される<br>③ Hybrid 検索で BM25 単独ヒットが閾値で消えず、絶対スケールスコアと Parent 重複集約が動作する<br>④ `POST /api/v1/dify/retrieval` が Dify 契約どおり 200 JSON (`{"records": [...]}`) を返す<br>⑤ 評価ハーネスでベースライン実測値（Recall@5 / MRR@10 / Hard-negative FP@5）が出力される<br>⑥ `ruff` / `mypy` / `pytest` / `TEST.py` / All_Status スイートがすべて pass |
| 5 | **テスト** | Phase ごとに `uv run ruff check src tests` + `uv run mypy src` + `uv run pytest -q`。統合時に `uv run python TEST.py --skip-docker --hash-embed`、実機 Docker 環境で `uv run python TEST.py`、`uv run python scripts/run-user-story-tests.py` |
| 6 | **優先順位** | **品質（データ安全性・検索精度） > 保守性 > 速度** |

### 役割分担

| 役割 | 担当 | 責務 |
|---|---|---|
| **Architect** | 設計・判断・レビューモデル | 01 要件整理 / 02 Phase 分解 / 05 各 Phase 完了時の管理レビュー・バグ調査と修正 |
| **Builder** | Grok ワーカー | 03 コーディング / 04 単体・統合テスト実行（1 Phase = 1 ブランチ、計画の発明禁止） |
| **人間（マスター）** | プロダクトオーナー | 設計判断・Phase 承認・優先順位変更・最終受入 |

---

## 1. 現状完成度評価（`v0.1.0` コード実測監査）

**総合完成度: 約 45〜50%（垂直スライス MVP。本番運用・Dify 連携には未達）**

設計書 `00`〜`20` のアーキテクチャは妥当だが、実装は検証・暗号化・増分更新・BM25/RRF の重要ロジックが未接続またはバグを抱えたままになっている。

### 1.1 領域別スコアとコード根拠

| 領域 | 完成度 | 重大度 | 実コードで確認した欠陥と該当箇所 |
|---|---|---|---|
| **パイプライン / 整合性** | 35% | **致命** | 1. `pipeline.py:249-256, 521-535`: `mode="append"` でも新規の空 `staging_collection` を作り、`qdrant_alias` を丸ごと差し替えるため、**追加取り込みすると既存の検索データが全消失する**。<br>2. `pipeline.py:501-543`: `_verify_and_activate` で `sqlite_children` を集計しているのに `_ = sqlite_children` で捨てており、件数整合性を一切検証していない。<br>3. `pipeline.py:531-535`: `archived_collections` に `delete_after=now` を入れるだけで、GC（実削除）が未実装。 |
| **重複排除 / 更新** | 20% | **致命** | 1. `pipeline.py:141-148, 217-224`: ジョブごとに毎回新規 `source_id` / `object_id` を発行するため、`pipeline.py:313-318` の `WHERE object_id=? AND content_hash=?` による重複排除が一度も発火しない。<br>2. 文書・レコード更新時に旧 `doc_version_id` の Qdrant point を削除する処理が無く、旧チャンクが検索結果に残留する。 |
| **並行性 / スレッド安全** | 30% | **致命** | `api.py:85, 97-103`, `worker.py:76-82`: FastAPI スレッド、worker スレッド、heartbeat スレッドの 3 系統が単一の `sqlite3.Connection`（`STATE.conn`）をロックなしで共有。同時アクセス時にトランザクション混線・`OperationalError` を起こす。 |
| **セキュリティ** | 25% | **致命** | 1. `secrets.py` と `db.py` の `secrets` テーブルが存在するのに未使用。`pipeline.py:191` で MySQL パスワードが `sources.config_json` に平文保存される。<br>2. `worker.py:84` に `password = None` の仮実装が残っている。<br>3. LAN / Docker 公開時の API キー認証および ACL 強制フィルタが未実装。 |
| **検索アルゴリズム** | 40% | **致命** | 1. `qdrant_store.py:72-74`: `SparseVectorParams` に `modifier=qm.Modifier.IDF` が無く、BM25 の IDF が無効。<br>2. `search.py:57-59`: `if dense_score < score_threshold: continue` で足切りするため、**BM25 単独ヒット（`dense_score=0.0`）が既定閾値 `0.60` で 100% 捨てられる**。<br>3. `qdrant_store.py:201-208`: RRF スコアを `max_score` で割っているため、無関係クエリでも 1 位が必ず `score=1.0` になる。<br>4. `sparse.py:14-29`: 日本語ストップワード除去・文字種分離（漢字/カタカナ/ひらがな）が無く、助詞 2-gram がノイズ化する。<br>5. `search.py`: 同一 `parent_chunk_id` の重複集約がなく、1 つの親文書が `top_k` 枠を占有する。 |
| **MySQL コネクタ** | 40% | **重大** | 1. `pipeline.py:456-479`: `iter_table_rows` の全行を `docs` リストに溜め込むため、大テーブルで OOM する（unbuffered cursor の意味がない）。<br>2. `mysql_reader.py:93`: `test_connection` が `INSERT INTO regulations ...` をハードコードしており、任意 DB では「テーブル不在エラー」を「書込み拒否 OK」と誤認する。<br>3. `mysql_reader.py:33-41`: `assert_readonly_sql` が定義済みだが未使用。カスタム SQL、テンプレート JSON、`lookup_table` JOIN、`updated_at` 増分同期、複合 PK アルファベット順ソート（設計書 `09` §6〜10）がいずれも未実装。 |
| **パーサ / チャンキング** | 50% | **重大** | 1. `parsers/txtmd.py:42-79`: `parse_md` が CommonMark のみで GFM テーブルとコードブロック（`fence`/`code_block`）を黙って捨てる。<br>2. `parsers/csvxlsx.py:22`, `parsers/txtmd.py:18`: `utf-8-sig` 固定で Shift_JIS / CP932 フォールバックが無い。<br>3. `parsers/csvxlsx.py:38-58`: XLSX の空行スキップと結合セル展開が未実装。<br>4. `parsers/office.py:96-118`: `re_remote_ref` が本文中に `http://`/`https://` 文字列があるだけでローカル HTML を拒否する。<br>5. `parsers/office.py:17-37` & `chunking.py:13`: `HEADING_RE` が未使用で PDF の章・条見出しが分割されない。<br>6. `chunking.py:209-214`: overlap が文の途中で切れ、`[:MAX_CHILD_CHARS]` で本体末尾を強制カットする。`infer_doc_kind` で `mime="text/plain"` が `source_type="mysql"` より先に判定される順序バグがある。 |
| **外部 AI（Dify）連携** | 10% | **重大** | Dify External Knowledge API（`POST /retrieval`）に未対応。ファイル経由の再投入は Dify 側で二重分割され精度が落ちる。 |
| **評価・UI・運用規約** | 30% | **中** | `evaluation_runs` テーブル未使用、Recall@5 / MRR@10 評価ハーネス未実装、UI ツール名直下の版表示（`10-versioning.mdc`）未整備、All_Status（`14-all-status-method.mdc`）未作成。 |

---

## 2. Plan 01: 要件整理

### 2.1 機能要件

- **R1（データ整合性・世代管理）**: `create` / `append` / `reindex` が既存データを破壊せず動作し、SQLite 有効 child 数と Qdrant point 数の一致を検証すること。
- **R2（並行安全性・秘密情報保護）**: SQLite アクセスがスレッド安全であり、MySQL パスワードが Fernet 暗号化されて `secrets` テーブルに格納され、平文が `sources.config_json` やログに残らないこと。
- **R3（文書パーサ・チャンキング精度）**: PDF / DOCX / PPTX / HTML / XLSX / CSV / TXT / MD について、表・コード・見出し・Shift_JIS・結合セル・ローカル HTML 内 URL を壊さず構造化し、文境界を尊重した Parent-Child 分割を行うこと。
- **R4（MySQL 構造化取込）**: ストリーミング（バッチ単位）取込、論理文書テンプレート、`lookup_table` LEFT JOIN、保存済み READ ONLY カスタム SQL、`updated_at` 増分同期、ビューへの明示 PK 指定に対応すること。
- **R5（ハイブリッド検索精度）**: Qdrant `Modifier.IDF` + 改良日本語トークナイザ + Qdrant ネイティブ RRF + 絶対スケールスコア + BM25 単独ヒット救済 + Parent 重複集約 + 任意 Cross-Encoder Reranker を実装すること。
- **R6（Dify External Knowledge API 連携）**: `H:\CURSOR\dify\api\services\external_knowledge_service.py` の契約に準拠した `POST /api/v1/dify/retrieval`、Bearer 認証、LAN 公開時フェイルクローズ、`metadata_condition` 変換を提供すること。
- **R7（定量評価・UI・QA 資産化）**: 設計書 `18` 準拠の評価ハーネス、UI の版表示・Dify 接続案内・MySQL 拡張入力、All_Status テスト一式、`TEST.py` 拡張を整備すること。

### 2.2 非機能・制約

- `cl-nagoya/ruri-v3-310m`（revision 固定、FP32、768 次元、`PREFIX_DOC` / `PREFIX_QUERY`）を変更しない。
- チャット UI・LLM 生成機能は持たない（Dify に委譲）。
- ブラウザ通知（`Notification` / Web Push）を実装しない。
- `00-karpathy-guidelines.mdc` §0 に従い、例外の握り潰し・検証の省略（`_ = var`）を禁止する。

---

## 3. Plan 02: 実装計画（Phase 構成と依存 DAG）

```
Phase 1: 基盤バグ修正・整合性・暗号化 (feat/phase1-core-integrity)
   │
   ├──→ Phase 2: 検索アルゴリズム刷新 (feat/phase2-search-algo)
   ├──→ Phase 3: パーサ＆チャンキング改修 (feat/phase3-parsers-chunking)
   └──→ Phase 4: MySQL コネクタ完成 (feat/phase4-mysql-connector)
              │
              ▼ (Phase 2 + Phase 4 完了後)
         Phase 5: Dify 外部知識 API + 認証・ACL (feat/phase5-dify-api-auth)
              │
              ▼ (Phase 1〜5 完了後)
         Phase 6: 評価ハーネス + UI + All_Status + リリース資産化 (feat/phase6-eval-ui-release)
```

| Phase | ブランチ | 依存 | 並列可否 | 主な成果物 |
|---|---|---|---|---|
| **Phase 1** | `feat/phase1-core-integrity` | なし | 単独先行 | スレッド安全 SQLite、`append`/`reindex` 修正、dedup、verify 厳格化、`secrets` 暗号化、`SHOW GRANTS` 検証 |
| **Phase 2** | `feat/phase2-search-algo` | Phase 1 | 3・4 と並列可 | `Modifier.IDF`、日本語トークナイザ、ネイティブ RRF、絶対スコア、BM25 救済、Parent 集約、任意 Reranker |
| **Phase 3** | `feat/phase3-parsers-chunking` | Phase 1 | 2・4 と並列可 | 文字コードフォールバック、MD 表/コード、XLSX 結合セル/空行、HTML 表/URL 許容、PDF 見出し、overlap 修正 |
| **Phase 4** | `feat/phase4-mysql-connector` | Phase 1 | 2・3 と並列可 | schema v2、ストリーミングバッチ、テンプレート + `lookup_table` JOIN、カスタム SQL、増分同期 |
| **Phase 5** | `feat/phase5-dify-api-auth` | Phase 2, 4 | 単独 | `POST /api/v1/dify/retrieval`、Bearer 認証、LAN フェイルクローズ、`metadata_condition` 変換 |
| **Phase 6** | `feat/phase6-eval-ui-release` | Phase 1〜5 | 単独最終 | `eval.py` + `testdata/eval_baseline.json`、UI 更新 + 版表示、All_Status、`TEST.py`、`v0.2.0` 資産化 |

---

## 4. Plan 03: Phase 別コーディング詳細設計（Grok ワーカー実装指示）

> **ワーカー共通規律**
> 1. 各 Phase は指定された変更対象ファイルのみを編集し、「触らない範囲」を改変しない。
> 2. 設計書にない抽象化・リファクタ・パッケージ追加を独断で行わない。
> 3. 公開関数・メソッドは Google スタイル docstring + 型ヒント（`mypy --strict` 通過）を必須とする。
> 4. `except Exception: pass` および未使用変数による検証握り潰しを禁止する。
> 5. Phase 完了報告は「変更ファイル」「実行した検証コマンドと結果」「残課題の有無」の 3 点で簡潔に返す。

---

### Phase 1: 基盤バグ修正・データ整合性・セキュリティ強化

- **ブランチ**: `feat/phase1-core-integrity`
- **変更対象**:
  - `src/db2rag/db.py`
  - `src/db2rag/api.py`
  - `src/db2rag/worker.py`
  - `src/db2rag/pipeline.py`
  - `src/db2rag/qdrant_store.py`
  - `src/db2rag/mysql_reader.py`
  - `src/db2rag/purge.py`
  - `tests/test_phase1_integrity.py`（新規）
- **触らない範囲**: `src/db2rag/parsers/**`, `src/db2rag/sparse.py`, `src/db2rag/search.py`, `src/db2rag/chunking.py`, `web/**`

#### 1.1 SQLite スレッド安全化（`db.py`, `api.py`, `worker.py`）
- `db.py` に `DbManager` クラス（またはスレッドローカル接続キャッシュ + 書込み `threading.RLock()`）を実装する:
  - `DbManager(db_path: Path)`:
    - `connection() -> sqlite3.Connection`: スレッド ID ごとに独立した `sqlite3.Connection`（WAL / `busy_timeout=5000` / `foreign_keys=ON`）を返す。
    - `write_lock() -> AbstractContextManager[None]`: 複数スレッドからの同時書込み競合を防ぐ `RLock` コンテキスト。
    - `close_all() -> None`: 生成した全接続をクローズする（`lifespan` 終了時に呼ぶ）。
- `api.py`: `STATE.db = DbManager(settings.sqlite_path())` を保持し、各エンドポイントは `conn = STATE.db.connection()` を使用する。worker スレッド・heartbeat スレッドも `db.connection()` で自スレッドの接続を取得する。`sqlite3.Connection` をスレッド間で共有しない。

#### 1.2 `create` / `append` / `reindex` とポイント差分削除（`pipeline.py`, `qdrant_store.py`）
- `qdrant_store.py` に以下を追加する:
  - `delete_points(self, collection: str, point_ids: list[str]) -> None`: 空リストなら即 return。100 件バッチで `qm.PointIdsList(points=batch)` を `wait=True` で削除する。
  - `delete_by_object_id(self, collection: str, object_id: str) -> None`: `Filter(must=[FieldCondition(key="object_id", match=MatchValue(value=object_id))])` で削除する。
- `pipeline.py` の `process_job` をモード別に分岐する:
  - **`mode == "append"` かつ `rag["active_collection"]` が存在**:
    - `collection = rag["active_collection"]` をそのままターゲットとする（空の staging を作らない）。
    - `rags.staging_collection` は変更しない。
    - 各 `object_id` の更新時: 新しい `doc_version_id` の points を upsert 成功した後に、同一 `object_id` の旧 `doc_version_id`（新 `doc_version_id` 以外）に属する `child_chunks.qdrant_point_id` を取得し、`store.delete_points(collection, old_point_ids)` で削除し、SQLite 側の旧 `child_chunks` / `parent_chunks` を削除するか、または `source_objects.latest_doc_version_id` 切り替えと整合させる（注: `latest_doc_version_id` 以外の旧 `child_chunks` の Qdrant point を必ず消す）。
  - **`mode == "create"` または `rag["active_collection"] is None`**:
    - `collection = rag.get("staging_collection") or f"db2rag_{rag_id}_staging_{job['job_id']}"` を作成して upsert → verify → `store.set_alias(alias, collection)` → `active_collection = collection, staging_collection = None`。
  - **`mode == "reindex"`**:
    - 新規 `staging_collection` を作成し、対象 RAG の全有効 `source_objects` を再投入 → verify → atomic alias 切替 → 旧 `active_collection` を `archived_collections` に `delete_after = utcnow() + 7 days` で登録する。
- `gc_archived_collections(conn, store)` を `pipeline.py` に実装:
  - `SELECT collection_name FROM archived_collections WHERE delete_after <= ?` を取得し、`store.delete_collection(name)` 成功後に `DELETE FROM archived_collections WHERE collection_name=?` する。
  - worker ループ起動時およびジョブ完了時に呼ぶ。

#### 1.3 `_verify_and_activate` の厳格化（`pipeline.py`）
- 最新有効チャンク数を次クエリで算出する:
  ```sql
  SELECT COUNT(*) AS n
  FROM child_chunks c
  JOIN parent_chunks p ON c.parent_chunk_id = p.parent_chunk_id
  JOIN source_objects o ON p.doc_version_id = o.latest_doc_version_id
  JOIN sources s ON o.source_id = s.source_id
  WHERE s.rag_id = ?
  ```
  （※MySQL の 1 object = 複数 record の場合は Phase 4 で `document_versions.is_latest = 1` に拡張する。Phase 1 時点では、更新時に旧版の `child_chunks` / `parent_chunks` を削除して「テーブル内の残存 `child_chunks` = アクティブ」に統一するか、または `document_versions` の最新集合と一致させる設計にし、必ず `sqlite_children == qcount` が厳密成立するようにする。）
- `failed == 0` かつ `sqlite_children != qcount` の場合:
  - ジョブを `status='failed', error_code='ERR_VERIFY_COUNT_MISMATCH', error_message=f"sqlite={sqlite_children}, qdrant={qcount}"` で終了し、alias を切り替えない。
- `_ = sqlite_children` の握り潰しを削除する。

#### 1.4 `source_objects` の安定化と dedup 有効化（`pipeline.py`）
- ファイル・MySQL とも、`source_objects` 挿入前に同一 `rag_id` 内で `(source_type, object_key)` が既に存在するか確認する:
  - 存在する場合は既存 `object_id` を再利用し、`job_items` に紐づける（新規 `object_id` を作らない）。
  - これにより、`document_versions(object_id, content_hash)` の重複判定が正しく効き、未変更ファイルはスキップ（`job_items.status='skipped'`）される。

#### 1.5 MySQL 秘密情報の暗号化保存（`pipeline.py`, `worker.py`, `secrets.py`, `purge.py`）
- `create_job_from_mysql` に `secrets_key_path: Path` を渡す:
  - `password = str(mysql_config.get("password") or "")` を取り出し、`fernet = load_or_create_key(secrets_key_path)` で暗号化して `secrets(secret_id, kind, ciphertext, created_at)` に `kind='mysql_password'` で INSERT する。
  - `sources.config_json` に保存する辞書からは `"password"` キーを必ず削除し、代わりに `"secret_id": secret_id` を格納する。
- `process_job` / `_extract_documents` に `secrets_key_path: Path` を渡す:
  - `mysql_password` 引数が None のとき、`config.get("secret_id")` から `secrets` テーブルを引いて復号する。
  - ログおよびエラーメッセージにパスワード平文を含めない。
- `purge_rag`:
  - 対象 RAG の `sources.config_json` から `secret_id` を収集し、`DELETE FROM secrets WHERE secret_id=?` を実行する。

#### 1.6 MySQL `test_connection` の書込み拒否判定修正（`mysql_reader.py`）
- ハードコード `INSERT INTO regulations ...` を削除する。
- `SHOW GRANTS FOR CURRENT_USER()` を実行し、返却された全 GRANT 行を検査する:
  - `WRITE_PRIV_RE = re.compile(r"\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|GRANT OPTION|SUPER|FILE)\b", re.IGNORECASE)`
  - いずれかの行に `WRITE_PRIV_RE` がマッチしたら `write_blocked = False`、1 件もマッチせず `SELECT` 権限が確認できたら `write_blocked = True`。
  - `SHOW GRANTS` 自体が権限不足で失敗した場合は `write_blocked = True` かつ `warning` に記録。

#### 1.7 Phase 1 テスト（`tests/test_phase1_integrity.py`）
1. `test_append_preserves_existing_documents`: ファイル A で `create` → ファイル B で `append` → A・B 両方が検索ヒットし、`qdrant.count == sqlite_children`。
2. `test_dedup_skips_unchanged_file`: 同一ファイル A を再投入 → `document_versions` が増えず、Qdrant point 数も変化しない。
3. `test_update_replaces_old_points`: ファイル A の内容を書き換えて再投入 → 旧語句が検索ヒットせず、新語句のみヒットし、`qdrant.count == sqlite_children`。
4. `test_verify_mismatch_fails_job`: Qdrant 側 point を意図的に 1 件消した状態で verify → `ERR_VERIFY_COUNT_MISMATCH` で失敗し alias が切り替わらない。
5. `test_mysql_password_encrypted_in_secrets`: `create_job_from_mysql` 後、`sources.config_json` に平文パスワード文字列が存在せず、`secrets` テーブルから復号できる。`purge_rag` 後に `secrets` 行が消える。
6. `test_mysql_test_connection_uses_show_grants`: モック接続で `SHOW GRANTS` が `GRANT SELECT ON hr.*` のとき `write_blocked=True`、`GRANT SELECT, INSERT ON hr.*` のとき `write_blocked=False`。
7. `test_multithread_sqlite_access`: 4 スレッド同時読み書きで `OperationalError` ゼロ。

---

### Phase 2: 検索アルゴリズム刷新（精度コア改修）

- **ブランチ**: `feat/phase2-search-algo`
- **依存**: Phase 1
- **変更対象**:
  - `src/db2rag/constants.py`
  - `src/db2rag/config.py`
  - `src/db2rag/sparse.py`
  - `src/db2rag/qdrant_store.py`
  - `src/db2rag/search.py`
  - `src/db2rag/reranker.py`（新規）
  - `src/db2rag/api.py`（`SearchRequest` フィールド追加）
  - `tests/test_phase2_search.py`（新規）
- **触らない範囲**: `src/db2rag/parsers/**`, `src/db2rag/mysql_reader.py`, `web/**`

#### 2.1 Qdrant Sparse IDF 有効化（`qdrant_store.py`, `constants.py`）
- `constants.py`:
  - `SPARSE_INDEX = "bm25_multilingual_v2"`
  - `BM25_K1 = 1.2`
  - `SPARSE_MIN_RRF_NORM = 0.35`（BM25 単独ヒット救済の下限。RRF 理論最大値比）
- `qdrant_store.py`:
  - `create_collection` の `sparse_vectors_config`:
    ```python
    sparse_vectors_config={
        "sparse": qm.SparseVectorParams(
            index=qm.SparseIndexParams(on_disk=False),
            modifier=qm.Modifier.IDF,
        )
    }
    ```

#### 2.2 日本語トークナイザ改良（`sparse.py`）
- NFKC 正規化後、文字種ごとにトークン化する:
  - **英数・型番・識別子**: `_ALNUM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./]*")`
    - 小文字化した完全トークン（例: `err-4092-xj`）を追加。
    - `-` / `_` / `/` / `.` で分割した 2 文字以上のサブトークン（例: `err`, `4092`, `xj`）も追加。
  - **漢字連続**: `_KANJI = re.compile(r"[\u4e00-\u9faf々]+")`
    - 1 文字ならそのまま、2〜16 文字なら語全体を追加、さらに長さ 2 以上なら 2-gram を追加。
  - **カタカナ連続**: `_KATA = re.compile(r"[\u30a0-\u30ffー]+")`
    - 長さ 2 以上なら語全体（上限 16 文字）と 2-gram を追加。
  - **ひらがな連続**: `_HIRA = re.compile(r"[\u3040-\u309f]+")`
    - `JA_STOPWORDS`（助詞・助動詞・汎用機能語: `の, は, が, を, に, で, と, も, や, へ, から, まで, より, など, ため, こと, もの, ところ, よう, です, ます, でした, ました, する, した, して, され, される, いる, ある, ない, なる, なっ, でき, できる, れる, られる, この, その, あの, どの, これ, それ, あれ, どれ, また, および, または, について, における, による, として, に対して`）に含まれる語は除外する。
    - 残った 2 文字以上の語のみ語全体と 2-gram を追加する。
- **TF 重み**:
  - $w(t) = \frac{(k_1 + 1) \cdot tf}{tf + k_1}$（$k_1 = 1.2$）。同一ハッシュ index 衝突時は加算。

#### 2.3 Qdrant ネイティブ RRF & 絶対スケールスコア & BM25 単独ヒット救済（`qdrant_store.py`, `search.py`）
- `qdrant_store.py` の `hybrid_search`:
  - `dense_hits`（`limit=PREFETCH_DENSE`）と `sparse_hits`（`limit=PREFETCH_SPARSE`）からそれぞれの `dense_score` / `sparse_score` / `sparse_rank` を保持する。
  - HTTP モードでは Qdrant 1.19 ネイティブ `query_points(prefetch=[...], query=qm.FusionQuery(fusion=qm.Fusion.RRF))` を優先使用するか、または `dense_hits` / `sparse_hits` の両スコアが確実に取れるよう RRF を厳密計算する（重要: `dense_score` と `sparse_rank` の両方が必要。ネイティブ `FusionQuery` は融合後スコアしか返さず `dense_score` が欠落するため、**ネイティブ `FusionQuery` を使う場合は `dense` スコアが不要になるよう `RRF` スコアを直接使うか、あるいは `prefetch` 2 本の結果から決定的に RRF + `dense_score` + `sparse_rank` を算出する**。一貫性とテスト容易性から、本 Phase では `client.query_batch_points` で dense と sparse の 2 クエリを 1 RTT で取得し、RRF 理論最大値正規化と `dense_score`・`sparse_score`・`sparse_rank` を厳密に返す方式を採用する）。
  - **絶対スケール RRF スコア**:
    - 理論最大値: $S_{\max} = \frac{1}{k + 1} + \frac{1}{k + 1} = \frac{2}{k + 1}$（$k = 60$ なら $S_{\max} \approx 0.032787$）。
    - 片側のみの理論最大値: $S_{\text{single}} = \frac{1}{k + 1}$。
    - `rrf_norm = min(1.0, raw_rrf / S_max)`。
    - 返却辞書: `{"id", "score": rrf_norm, "raw_rrf": raw_rrf, "dense_score": dense_score, "sparse_score": sparse_score, "sparse_rank": sparse_rank, "payload": ...}`。
    - 無関係クエリで 1 位が勝手に `1.0` に張り付かない（片側 1 位のみなら `0.50`、両方 1 位で `1.00`）。
- **閾値判定（`search.py`）**:
  - `fetch_limit = max(top_k * 3, 30)` で Qdrant から候補を取得する。
  - `hybrid=False`: `dense_score >= score_threshold`。
  - `hybrid=True` かつ `reranker` 未使用:
    - `score_threshold >= 0.80`（厳格ノイズ遮断モード）: `dense_score >= score_threshold` または (`sparse_rank is not None and sparse_rank <= 3 and dense_score >= score_threshold * 0.65`)。
    - `score_threshold < 0.80`（通常検索）: `dense_score >= score_threshold` **または** (`sparse_rank is not None and sparse_rank <= 10 and sparse_score > 0.0` かつ `rrf_norm >= SPARSE_MIN_RRF_NORM`)。
    - これにより、Ruri dense に弱い型番・略語・固有名詞の BM25 単独ヒットが `score_threshold=0.60` で捨てられず、`0.85` での無関係ノイズ遮断テストも通る。

#### 2.4 Parent 単位の重複集約（`search.py`, `api.py`）
- `search()` と `SearchRequest` に `dedup_parent: bool = True`、`use_reranker: bool | None = None` を追加する。
- `dedup_parent=True` のとき:
  - ヒットをスコア降順に走査し、`parent_chunk_id` が既出なら新規ヒットを作らず、代表ヒットの `matched_children: list[dict[str, Any]]` に `{"child_chunk_id", "score", "dense_score", "snippet"}` を追加する。
  - 未出の `parent_chunk_id` のみ代表ヒットとして採用し、`len(hits) >= top_k` で打ち切る。

#### 2.5 任意 Cross-Encoder Reranker（`reranker.py`, `config.py`, `search.py`）
- `config.py` に追加:
  - `reranker_enabled: bool = False`
  - `reranker_model: str = "cl-nagoya/ruri-v3-reranker-310m"`
  - `reranker_top_n: int = 30`
- `reranker.py`:
  - `Reranker(enabled: bool, model_id: str, models_dir: Path)`:
    - `score(query: str, passages: list[str]) -> list[float]`: 遅延ロード（`sentence_transformers.CrossEncoder`、FP32）、出力 logit を sigmoid $1 / (1 + e^{-x})$ で $[0, 1]$ に較正して返す。`enabled=False` なら呼ばれない。
    - テスト差し替え用に `CrossEncoder` を DI 可能な構造にする。
- `search.py`:
  - `effective_rerank = reranker is not None and reranker.enabled if use_reranker is None else (use_reranker and reranker is not None)`。
  - 有効時は Hybrid 上位 `reranker_top_n` 件の `child_text` を再スコアし、`rerank_score` で降順ソート、閾値判定は `rerank_score >= score_threshold` を適用し、レスポンス `score` に `rerank_score` を反映する（元の `rrf_score` と `dense_score` も残す）。

#### 2.6 Phase 2 テスト（`tests/test_phase2_search.py`）
1. `test_sparse_tokenizer_ja_and_codes`: ストップワード（「の」「です」）除外、型番 `ERR-4092-XJ` の完全一致＋サブトークン生成、漢字/カタカナ分離を確認。
2. `test_bm25_only_hit_survives_default_threshold`: `dense_score=0.0`・`sparse_rank=1` のヒットが `score_threshold=0.60` で消えない。
3. `test_rrf_score_not_pinned_to_one`: 候補が 1 件で片側ヒットのみの場合、`score` が `1.0` にならず `<= 0.5` になる。
4. `test_parent_dedup_aggregates_children`: 同一 `parent_chunk_id` の 3 child が返っても `dedup_parent=True` で 1 件に集約され `matched_children` に残る。
5. `test_reranker_reorders_hits`: ダミー Reranker を注入し、スコア順に順位が入れ替わり `rerank_score` による閾値フィルタが効く。
6. `test_collection_enables_idf_modifier`: `QdrantStore.create_collection` が `Modifier.IDF` を指定していることを検証。

---

### Phase 3: パーサ＆チャンキング精度改修

- **ブランチ**: `feat/phase3-parsers-chunking`
- **依存**: Phase 1
- **変更対象**:
  - `src/db2rag/parsers/__init__.py`
  - `src/db2rag/parsers/txtmd.py`
  - `src/db2rag/parsers/csvxlsx.py`
  - `src/db2rag/parsers/office.py`
  - `src/db2rag/parsers/tables.py`
  - `src/db2rag/chunking.py`
  - `tests/test_phase3_parsers.py`（新規）
- **触らない範囲**: `src/db2rag/mysql_reader.py`, `src/db2rag/qdrant_store.py`, `src/db2rag/search.py`, `web/**`

#### 3.1 文字コードフォールバックとマクロ拒否（`parsers/__init__.py`）
- `MACRO_SUFFIXES = {".docm", ".xlsm", ".pptm"}`: `parse_file` 冒頭で該当拡張子を `ValueError(f"ERR_FILE_MACRO:{suffix}")` で拒否する。
- `read_text_with_fallback(path: Path) -> str` を `parsers/__init__.py`（または `textnorm.py`）に実装:
  - 順序: `("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc_jp")`。
  - すべて `UnicodeDecodeError` なら `ValueError(f"ERR_FILE_ENCODING:{path.name}")`。

#### 3.2 Markdown 表・コードブロック対応（`parsers/txtmd.py`）
- `_MD = MarkdownIt("commonmark").enable("table")` とする。
- `parse_md`:
  - `table_open` 〜 `table_close` を走査し、`thead` から `headers`、`tbody` から `rows` を抽出し、`table_blocks(headers, rows, section_path=current_section)` で `Block(type="table")` を追加する（現在の見出し階層を `section_path` に反映）。
  - `fence` / `code_block` は `info`（言語名）があれば `[{info}]\n{content}` として `Block(type="code", text=...)` に保持する。
- `parse_txt` は `read_text_with_fallback` を使用する。

#### 3.3 CSV / XLSX 改修（`parsers/csvxlsx.py`）
- `parse_csv`: `read_text_with_fallback` で読み、`io.StringIO` を `csv.reader` へ渡す。全セル空の行はスキップする。
- `parse_xlsx`:
  - `MERGED_CELL_MAX_BYTES = 20 * 1024 * 1024`（20 MB）。
  - ファイルサイズ `<= MERGED_CELL_MAX_BYTES` なら `load_workbook(path, read_only=False, data_only=True)` で開き、シートごとに `sheet.merged_cells.ranges` の左上セル値を全結合座標マップ `(row, col) -> value` に展開してから行を読み出す。
  - `> MERGED_CELL_MAX_BYTES` の場合は `read_only=True, data_only=True` にフォールバックし、警告ログを残す。
  - 全セル空の行をスキップする。空シート（ヘッダなし、または有効データ行 0 件）はスキップする。

#### 3.4 HTML / PDF 改修（`parsers/office.py`）
- `parse_html`:
  - `re_remote_ref` を削除する。代わりに文字列引数が `http://` / `https://` で始まる場合のみ `ValueError("ERR_FILE_REMOTE_HTML")` とする（ローカルファイル内のリンク文字列は正常データとして許容する）。
  - `read_text_with_fallback` で読み込む。
  - `<table>` 要素を先に処理して `table_blocks` 化し、他の `h1..h6`, `p`, `li` 抽出時は `node.find_parent("table") is None` で二重抽出を防ぐ（文書順を保つため DOM 走査順で `table` と見出し・段落を処理する）。
- `parse_pdf`:
  - 各ページ本文を行単位で走査し、見出し正規表現 `_PDF_HEADING = re.compile(r"^(第[0-9一二三四五六七八九十百]+[章節款条]|#{1,6}\s+|[0-9]+(\.[0-9]+){1,2}\s+\S)")` にマッチし長さが 60 文字以下の行を `Block(type="heading", text=..., level=..., page=number)` として切り出す（`第N章`=level 1、`第N節`=level 2、`第N条`=level 3、`1.`=level 1、`1.1`=level 2）。
  - 連続する通常行は `Block(type="paragraph", text=..., page=number)` にまとめる。

#### 3.5 Chunker 改修（`chunking.py`）
- `infer_doc_kind`:
  - `if extra.get("source_type") == "mysql": return "mysql"` を `mime` 判定より先に評価する。
- `_split_child`:
  - overlap 抽出関数 `_overlap_tail(prev: str, overlap: int) -> str`:
    - `tail = prev[-overlap:]` を取ったあと、`tail` 内に改行 `\n` または句点 `。．！？` があれば、その直後から切り出して文頭を整える（見つからなければ `tail` そのまま）。
  - overlap 結合時の切り捨て防止:
    - `available = MAX_CHILD_CHARS - len(chunk)`。`available <= 0` なら overlap を付けず `chunk[:MAX_CHILD_CHARS]`、そうでなければ `prefix = _overlap_tail(prev, min(params.overlap, available))` として `prefix + chunk` にする（本体テキストの末尾を落とさない）。
- `section_path` の文脈保持:
  - `flush_parent()` で生成した各 child が複数に分かれた場合（`len(child_texts) > 1`）かつ先頭が `section` を含まない場合、2 番目以降の child に `f"【{section}】\n{child_text}"`（ただし `MAX_CHILD_CHARS` 以内に収まるよう調整）を付与して、単体チャンクで文脈が失われないようにする。

#### 3.6 Phase 3 テスト（`tests/test_phase3_parsers.py`）
1. `test_shift_jis_csv_and_txt`: CP932 エンコードの CSV・TXT が文字化けせずパースされる。
2. `test_markdown_table_and_code_preserved`: GFM 表と Python コードブロックが `Block(type="table")` / `Block(type="code")` として保持される。
3. `test_xlsx_merged_cells_and_blank_rows`: 結合セルが展開され、空行が除外される。
4. `test_html_with_urls_and_table`: `https://example.com` を本文に含むローカル HTML と `<table>` が正常パースされ、二重抽出されない。
5. `test_pdf_heading_detection`: `第1章 総則` / `第2条 適用範囲` を含む PDF から heading ブロックと `section_path` が生成される。
6. `test_macro_office_rejected`: `.xlsm` / `.docm` / `.pptm` が `ERR_FILE_MACRO` で拒否される。
7. `test_chunk_overlap_does_not_truncate_body` & `test_infer_doc_kind_mysql_priority`: overlap 結合で本体末尾が消えないこと、`mime="text/plain"` + `extra={"source_type": "mysql"}` が `"mysql"` を返すこと。

---

### Phase 4: MySQL コネクタ完成（業務 DB → 構造化 RAG）

- **ブランチ**: `feat/phase4-mysql-connector`
- **依存**: Phase 1
- **変更対象**:
  - `src/db2rag/constants.py`（`SQLITE_SCHEMA_VERSION = 2`）
  - `src/db2rag/db.py`（v1 → v2 マイグレーション）
  - `src/db2rag/mysql_reader.py`
  - `src/db2rag/pipeline.py`
  - `src/db2rag/api.py`
  - `tests/test_phase4_mysql.py`（新規）
- **触らない範囲**: `src/db2rag/parsers/**`, `src/db2rag/sparse.py`, `src/db2rag/search.py`, `web/**`

#### 4.1 SQLite スキーマ v2 マイグレーション（`db.py`, `constants.py`）
- `SQLITE_SCHEMA_VERSION = 2`。
- `migrate(conn)` で既存 v1 DB に対して冪等に以下を実行する（`PRAGMA table_info(...)` で列存在チェック後 `ALTER TABLE`）:
  - `document_versions` に `record_id TEXT` 列、`is_latest INTEGER NOT NULL DEFAULT 1` 列を追加。
  - `source_objects` に `sync_cursor TEXT` 列、`config_json TEXT NOT NULL DEFAULT '{}'` 列（テンプレート・PK 上書き・カスタム SQL 保存用）を追加。
  - インデックス作成:
    - `CREATE INDEX IF NOT EXISTS idx_doc_object_record ON document_versions(object_id, record_id, is_latest);`
- `SCHEMA_SQL` 本体にも同じ列・インデックスを反映し、新規 DB でも v2 になるようにする。

#### 4.2 複合 PK ソート・ビュー明示 PK・ストリーミング（`mysql_reader.py`, `pipeline.py`）
- `record_id_from(pk_columns, row)`:
  - `for col in sorted(pk_columns):` に変更（設計書 `09` §6 準拠）。
- `row_hash(row, column_order: list[str] | None = None)`:
  - `cols = column_order or sorted(row)` 順に `f"{k}={normalize_text(... )}"` を `|` 連結して SHA-256（bytes は base64、datetime は ISO8601 UTC、NULL は空文字）。
- `iter_table_rows(...)`:
  - 引数追加: `where_col: str | None = None`, `since_value: str | None = None`, `lookups: list[dict[str, str]] | None = None`, `max_rows: int = 500_000`。
  - `lookups` 指定時は `LEFT JOIN `{schema}`.`{lookup_table}` AS `__lk_{i}` ON `base`.`{column}` = `__lk_{i}`.`{lookup_key}`` を安全に構築（全識別子を `_ident` 検証）し、`__lk_{i}`.`{lookup_label}` AS `__label_{column}` を SELECT する。
  - `where_col` と `since_value` 指定時はパラメータ化クエリ `WHERE `base`.`{where_col}` > %s ORDER BY `base`.`{where_col}` ASC` を実行する。
  - `max_rows` 超過時は警告ログを出してループを打ち切る。
  - カラム数が 100 超なら `MysqlError("ERR_MYSQL_TOO_MANY_COLUMNS")` を送出する。
- `iter_custom_sql_rows(conn, sql: str, *, max_rows: int = 500_000)`:
  - 必ず `safe_sql = assert_readonly_sql(sql)` を通す。
  - `REPEATABLE READ` + `READ ONLY` トランザクション + `buffered=False` カーソルで `safe_sql` を実行し、行辞書を yield する。
- **ストリーミングパイプライン（`pipeline.py`）**:
  - `_process_item` で MySQL の場合は全行を `list` に溜めず、`MYSQL_BATCH_SIZE = 200` レコード単位で `chunk → embed → upsert → SQLite commit` を実行する。

#### 4.3 論理文書テンプレート描画（`mysql_reader.py`）
- `render_record_with_template(object_key: str, record_id: str, row: dict[str, Any], template: dict[str, Any] | None) -> tuple[str, str, str | None, str | None]`:
  - 戻り値: `(rendered_text, doc_title, updated_at_value, row_acl_tag)`。
  - `template` が None なら従来の `render_record` にフォールバック。
  - `template` 指定時（設計書 `09` §8 準拠）:
    - `title_column`: 文書タイトルに使用（未指定・空時は `f"{object_key} {record_id}"`）。
    - `updated_at_column`: 増分同期カーソルと payload `updated_at` に使用。
    - `acl_column`: 行ごとの `acl_tag` 上書きに使用（空なら source 既定値）。
    - `sections`: 各 `heading` を `## {heading}` とし、`fields` を走査:
      - `lookup_table` がある場合: `__label_{column}` が取れていれば `コード {val} → {label}` で出力。
      - `type == "long_text"` なら独立段落、それ以外は `- {label}: {val}` で箇条書き。
      - `format == "date_ja"` なら `YYYY-MM-DD` 等を `YYYY年M月D日` に整形（パース失敗時は原文）。

#### 4.4 増分差分同期と旧レコード point 置換（`pipeline.py`）
- MySQL の各レコード処理時:
  1. `rec_id = record_id_from(pk, row)`、`rhash = row_hash(row)` を計算。
  2. `SELECT doc_version_id, row_hash FROM document_versions WHERE object_id=? AND record_id=? AND is_latest=1` を照合。
  3. 存在し `row_hash == rhash` ならスキップ（embed・upsert しない）。
  4. 存在し `row_hash != rhash` なら: 新版 `doc_version_id` を chunk / embed / upsert した後、旧版の `qdrant_point_id` を `store.delete_points(collection, old_point_ids)` で削除し、旧 `document_versions` を `is_latest=0`（および旧 `child_chunks` / `parent_chunks` を削除して verify 件数と一致させる）。
  5. バッチ完了ごとに `max(updated_at_value)` を `source_objects.sync_cursor` に更新する。
- `JobCreateRequest`（`api.py`）に以下を任意フィールド追加:
  - `mysql_templates: dict[str, dict[str, Any]] = Field(default_factory=dict)`（キーは `schema.table`）
  - `mysql_pk_overrides: dict[str, list[str]] = Field(default_factory=dict)`（ビュー用 PK 指定）
  - `custom_sql: list[dict[str, Any]] = Field(default_factory=list)`（`{sql_id, name, sql, pk_columns, template?}`）

#### 4.5 Phase 4 テスト（`tests/test_phase4_mysql.py`）
1. `test_schema_v1_migrates_to_v2`: v1 スキーマ DB に対して `migrate()` を実行し、`record_id` / `is_latest` / `sync_cursor` 列が追加され既存行が壊れない。
2. `test_template_and_lookup_rendering`: テンプレート JSON + `__label_status_code` から `コード 01 → 承認済` や `date_ja` が正しく描画される。
3. `test_custom_sql_readonly_guard`: `SELECT` / `WITH` は通り、`DELETE` / `INSERT` / `DROP` / セミコロン複数文 / `INTO OUTFILE` はすべて `MysqlError` で拒否される。
4. `test_incremental_sync_skips_unchanged_and_replaces_updated`: 1 回目 3 行取込 → 2 回目（1 行未変更、1 行更新、1 行追加）で未変更はスキップ、更新行は旧 Qdrant point が消えて新内容に置換、`sync_cursor` が前進、`sqlite_children == qdrant_count`。
5. `test_composite_pk_sorted_deterministically`: `["b_id", "a_id"]` と `["a_id", "b_id"]` で同じ `record_id`（`a_id=1|b_id=2`）になる。
6. `test_streaming_batches_without_full_materialization`: 650 行のジェネレータを 200 行バッチで完走し、全件 upsert・verify 通過する。

---

### Phase 5: Dify 外部知識 API 連携＋認証・ACL 強制

- **ブランチ**: `feat/phase5-dify-api-auth`
- **依存**: Phase 2, Phase 4
- **変更対象**:
  - `src/db2rag/dify_adapter.py`（新規）
  - `src/db2rag/auth.py`（新規）
  - `src/db2rag/config.py`
  - `src/db2rag/api.py`
  - `tests/test_phase5_dify.py`（新規）
- **触らない範囲**: `src/db2rag/parsers/**`, `src/db2rag/mysql_reader.py`, `src/db2rag/chunking.py`

#### 5.1 Dify External Knowledge API 契約（`dify_adapter.py`, `api.py`）
参照元: `H:\CURSOR\dify\api\services\external_knowledge_service.py`（L89〜120, L352〜425）。

- **エンドポイント**: `POST /api/v1/dify/retrieval`
  - Dify 画面で Endpoint に `http://host.docker.internal:8781/api/v1/dify` を登録すると、Dify は `${endpoint}/retrieval` へ POST する。
- **リクエストモデル（Pydantic）**:
  ```python
  class DifyRetrievalSetting(BaseModel):
      top_k: int = Field(default=5, ge=1, le=50)
      score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)

  class DifyMetadataConditionItem(BaseModel):
      name: list[str]
      comparison_operator: str
      value: Any = None

  class DifyMetadataCondition(BaseModel):
      logical_operator: str = "and"
      conditions: list[DifyMetadataConditionItem] = Field(default_factory=list)

  class DifyRetrievalRequest(BaseModel):
      knowledge_id: str = ""
      query: str
      retrieval_setting: DifyRetrievalSetting = Field(default_factory=DifyRetrievalSetting)
      metadata_condition: DifyMetadataCondition | None = None
  ```
- **認証（`auth.py`）**:
  - `Settings` に追加:
    - `api_key: str | None = None`（環境変数 `DB2RAG_API_KEY`）
    - `dify_content_mode: str = "parent"`（`"parent"` | `"child"`）
    - `dify_default_acl: str = "internal"`
    - `require_acl_in_lan: bool = True`
  - `DB2RAG_API_KEY` が設定されている場合、`/api/v1/dify/retrieval`（および LAN モード時は全 `/api/v1/*` ただし `/api/v1/health` 除く）で `Authorization: Bearer <token>` を `hmac.compare_digest` 検証する。
  - 認証失敗時は Dify 仕様どおり `HTTP 403` JSON: `{"error_code": 1002, "error_msg": "Authorization failed"}`。
  - 起動時フェイルクローズ（`cli.py` / `api.py`）: `host not in {"127.0.0.1", "localhost", "::1"}` のとき `api_key` が空なら起動を拒否する（`RuntimeError("DB2RAG_API_KEY is required when binding non-loopback host")`）。
- **疎通確認（`knowledge_id == ""`）**:
  - Dify の外部知識 API 登録時の疎通チェックは `knowledge_id: ""` を送ってくる。認証通過済みで `knowledge_id == ""` なら `200 {"records": []}` を返す。
- **未知の `knowledge_id`**:
  - `HTTP 404` JSON: `{"error_code": 2001, "error_msg": "The knowledge does not exist"}`。
- **`metadata_condition` 変換（`dify_adapter.py`）**:
  - 許可フィールド: `acl_tag`, `source_type`, `section_path`。
  - 演算子: `is`（単一値）, `in`（リストまたはカンマ区切り）, `contains`（`section_path` 用）。
  - `acl_tag` 条件が指定されない場合は、既定で `[settings.dify_default_acl]` を強制適用する（無制限公開防止）。
  - 許可外フィールドや未対応演算子は黙って無視せず `HTTP 400` でエラーを返す。
- **レスポンス変換**:
  - `run_search(..., expand_parent=True, dedup_parent=True)` を呼び、各 hit を以下へ変換する:
    ```json
    {
      "records": [
        {
          "content": "<parent_text（dify_content_mode=='parent' かつ存在時）または snippet>",
          "score": 0.8421,
          "title": "<title> / <section_path>",
          "metadata": {
            "rag_id": "...",
            "source_type": "mysql",
            "acl_tag": "internal",
            "section_path": "...",
            "record_id": "...",
            "parent_chunk_id": "...",
            "child_chunk_id": "...",
            "dense_score": 0.81
          }
        }
      ]
    }
    ```
- **LAN モードでの通常 `/api/v1/search` の ACL 強制**:
  - `host != 127.0.0.1` かつ `require_acl_in_lan=True` のとき、`filters.acl_tags` が空なら `HTTP 403` で拒否する（設計書 `15` §4 準拠）。

#### 5.2 Phase 5 テスト（`tests/test_phase5_dify.py`）
1. `test_dify_probe_empty_knowledge_id`: 正しい Bearer + `knowledge_id=""` → `200 {"records": []}`。
2. `test_dify_auth_failure_returns_403_code_1002`: 不正・欠落 Bearer → `403` かつ `error_code == 1002`。
3. `test_dify_unknown_knowledge_returns_404_code_2001`: 存在しない `knowledge_id` → `404` かつ `error_code == 2001`。
4. `test_dify_retrieval_returns_parent_content_and_metadata`: 実 RAG 検索で `records[].content` に `parent_text`、`score`（0〜1）、`title`、`metadata` が返る。
5. `test_dify_metadata_condition_filters_acl_and_source_type`: `acl_tag` / `source_type` 絞り込みが効き、未指定時は `dify_default_acl` が適用される。
6. `test_lan_mode_requires_api_key_and_acl`: `host="0.0.0.0"` + `api_key=None` で起動拒否、LAN 時 `acl_tags` 未指定の `/api/v1/search` が `403`。

---

### Phase 6: 定量評価ハーネス・UI 反映・All_Status・リリース資産化

- **ブランチ**: `feat/phase6-eval-ui-release`
- **依存**: Phase 1〜5 すべて
- **変更対象**:
  - `src/db2rag/eval.py`（新規）
  - `src/db2rag/cli.py`
  - `src/db2rag/api.py`
  - `testdata/eval_baseline.json`（新規）
  - `web/index.html`, `web/wizard.html`, `web/search.html`, `web/jobs.html`, `web/live.js`
  - `scripts/user-story-catalog.py`（新規）
  - `scripts/run-user-story-tests.py`（新規）
  - `docs/user-stories/README.md`（新規）
  - `docs/user-stories/user-stories-matrix.csv`（生成物）
  - `TEST.py`
  - `pyproject.toml`（`0.2.0`）
  - `CHANGELOG.md`, `README.md`
  - `H:\CURSOR\.cursor\カタログ.html`（`DB2RAG` エントリ更新）
  - `tests/test_phase6_eval.py`（新規）

#### 6.1 定量評価ハーネス（`eval.py`, `cli.py`, `testdata/eval_baseline.json`）
- `eval.py`:
  - 設計書 `18_test_and_eval.md` のメトリクスを厳密実装:
    - `recall_at_5`, `recall_at_20`, `mrr_at_10`, `ndcg_at_10`, `zero_result_rate`, `hard_negative_fp_at_5`, `latency_p95_ms`。
  - 正解判定は `expected_needles`（部分一致キーワード）または `expected_child_chunk_ids` / `expected_parent_chunk_ids` に対応し、Hard-negative は `forbidden_needles` または `forbidden_chunk_ids` に対応する。
  - 実行結果を `evaluation_runs` テーブルに INSERT し、`gate_passed`（Recall@5 ≥ 0.75, MRR@10 ≥ 0.65, Zero-result ≤ 0.05, HN-FP@5 ≤ 0.10）を実測値だけで判定する（未測定項目は `null`、合格扱いしない）。
- `cli.py`:
  - `uv run db2rag eval --rag-id <id> --dataset testdata/eval_baseline.json [--top-k 20] [--threshold 0.0]`
  - `uv run db2rag --version`（`pyproject.toml` の版を読む）。
- `testdata/eval_baseline.json`:
  - 既存 `testdata`（規程・FAQ・CSV・XLSX・PDF・DOCX・MySQL）に対する 25 問以上（通常質問 + 型番/数値 + Hard-negative 8 問以上）を定義。

#### 6.2 UI 反映と版表示（`api.py`, `web/**`）
- `api.py`:
  - `GET /api/v1/version`: `{"name": "DB2RAG", "version": importlib.metadata.version("db2rag")}` を返す。ハードコード禁止。
  - `GET /api/v1/rags/{rag_id}/dify-config`: Dify 登録用パラメータ（`endpoint_docker`: `http://host.docker.internal:{port}/api/v1/dify`, `endpoint_local`, `knowledge_id`, `auth_required`, `default_acl`）を返す。
  - `POST /api/v1/eval/run`: 評価実行トリガー。
- `web/index.html`, `web/wizard.html`, `web/jobs.html`, `web/search.html`, `web/live.js`:
  - 全 4 画面のサイドバー／ヘッダーにある**ツール名（`DB2RAG`）の直下に `v0.2.0`**（`/api/v1/version` から取得）を常時表示する（`10-versioning.mdc` §4）。
  - RAG 一覧の操作列に「Dify連携」ボタンを追加し、クリックでモーダルを開いて Endpoint URL と `knowledge_id` をワンクリックコピーできる（本文とボタンが重ならないこと）。
  - `wizard.html` の MySQL ステップに折りたたみ領域「詳細設定（任意）」を追加：カスタム SQL（`sql_id`, `sql`, `pk_columns`）、テンプレート JSON、増分同期 `updated_at` 列。
  - `search.html` に「親チャンク重複統合（既定 ON）」チェックボックスと「Reranker（設定有効時のみ操作可）」表示を追加。

#### 6.3 All_Status 手法と `TEST.py` 拡張
- `scripts/user-story-catalog.py`: 全 Epic（`RAG`, `FILE`, `MYSQL`, `SEARCH`, `DIFY`, `EVAL`, `PURGE`, `UI`）のストーリー正本を定義し、`docs/user-stories/user-stories-matrix.csv` を生成する。
- `scripts/run-user-story-tests.py`: `unit` / `api` / `build`（`ruff` + `mypy`）を実行し、`docs/user-stories/test-results.csv` と `docs/user-stories/summary.md` を出力。`fail > 0` なら `exit 1`。
- `TEST.py`: 既存 P0〜P6 に加え、以下を追加検証:
  - `append` 後の既存ファイル残存チェック
  - `POST /api/v1/dify/retrieval` の疎通 (`knowledge_id=""`) と検索返却 (`records[]`)
  - `db2rag eval` ベースライン実行

#### 6.4 版上げ・資産化
- `pyproject.toml`: `version = "0.2.0"`
- `CHANGELOG.md`: `## [0.2.0] - YYYY-MM-DD` を追加（`[Unreleased]` を残す）
- `README.md`: Dify 外部知識 API 接続手順、カスタム SQL・テンプレート・増分同期、評価コマンド、`docs/user-stories/` リンク、終了手順を更新
- `H:\CURSOR\.cursor\カタログ.html`: `DB2RAG` の `version: "0.2.0"` と `summary` / `entry` を更新

---

## 5. Plan 04: テスト・検証マトリクス

| 検証種別 | 実行コマンド | 合格基準 | 失敗時の切り分け |
|---|---|---|---|
| Lint | `uv run ruff check src tests` | exit 0 | 指摘ファイル・行を修正 |
| 型検査 | `uv run mypy src` | exit 0 (`strict=true`) | 欠落型注釈・`Any` 漏れを修正 |
| 単体試験 | `uv run pytest -q` | 全件 pass | 該当 `test_phaseN_*.py` の失敗アサーションからモジュール特定 |
| 煙試験（Ruri なし） | `uv run python TEST.py --skip-docker --hash-embed` | `fail == 0` | `data/test-server.log` と `job_items.error_message` を確認 |
| 実機統合（Docker + Ruri） | `uv run python TEST.py` | P0〜P6 + Dify + eval + export 全 pass | Docker (`qdrant`, `mysql`)・GPU VRAM・ポート競合を確認 |
| 定量評価 | `uv run db2rag eval --rag-id <id> --dataset testdata/eval_baseline.json` | Recall@5 ≥ 0.75, MRR@10 ≥ 0.65, HN-FP@5 ≤ 0.10 | ミス問の rank/dense_score/sparse_rank から原因特定 |
| All_Status | `uv run python scripts/run-user-story-tests.py` | `fail == 0` (exit 0) | `docs/user-stories/summary.md` の Epic 別集計を確認 |

---

## 6. Plan 05: Architect 管理レビューチェックリスト（各 Phase マージ前必須）

Grok ワーカーから Phase 完了報告が返ったら、マージ前に Architect が差分を全行読み、次を確認する:

1. **ベクトル空間不変**: `RURI_MODEL_ID`, `RURI_REVISION`, `EMBED_DIM=768`, `PREFIX_DOC`, `PREFIX_QUERY`, FP32 が改変されていないか。
2. **データ保護**: `append` で既存ポイントが消えないか、`purge` が `uploads/` 外の元ファイルを触らないか、`secrets` 平文がログ・JSON・SQLite `sources` に残っていないか。
3. **サイレント失敗ゼロ**: `except Exception: pass`、検証結果の捨て（`_ = ...`）、エラーコード握り潰しが無いか。
4. **Dify 契約完全一致**: `knowledge_id=""` の 200、`403 error_code=1002`、`404 error_code=2001`、`records[].content/score/title/metadata` が `H:\CURSOR\dify\api\services\external_knowledge_service.py` と一致しているか。
5. **DB 後方互換**: 既存の v1 SQLite ファイルに対して v2 マイグレーションが無破壊で通るか。
6. **スコープ遵守**: 指定外のファイルや機能に勝手な変更が入っていないか。

---

## 7. Plan 06: 完了後の資産化チェックリスト

- [ ] `pyproject.toml` を `0.2.0` へ更新（UI `/api/v1/version`・CLI `--version` が同一正本を参照）
- [ ] `CHANGELOG.md` に `## [0.2.0]` エントリ追記
- [ ] `README.md` に Dify 連携・MySQL 拡張・評価・`docs/user-stories/` リンク反映
- [ ] `docs/user-stories/` 一式（`README.md`, `user-stories-matrix.csv`, `test-results.csv`, `summary.md`）生成・pass 確認
- [ ] `H:\CURSOR\.cursor\カタログ.html` の `DB2RAG` エントリ更新（`57-tool-catalog.mdc`）
- [ ] 開発日記 2 層記録: `H:\CURSOR\ObsidianVault\90_DevLog\YYYY-MM-DD.md` + `H:\CURSOR\DB2RAG\docs\YYYY-MM-DD_開発日記.html`（`04-diary-workflow.mdc`）
- [ ] `.cursor/カタログ.html` 更新分の `cursor-config` commit & push（`00-cursor-config-sync.mdc`）
