"""ユーザストーリー正本。CSV は生成物。"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "user-stories" / "user-stories-matrix.csv"

COLUMNS = [
    "story_id",
    "epic",
    "title",
    "steps",
    "expected_behavior",
    "impl_status",
    "test_method",
    "test_ref",
]

STORIES: list[dict[str, str]] = [
    {
        "story_id": "RAG-001",
        "epic": "RAG",
        "title": "追記しても既存文書が残る",
        "steps": "1. ファイルを create する。2. 別ファイルを append する。3. 両方の文言を検索する。",
        "expected_behavior": "append 後も最初の文書のポイントが残り、alias は空の staging に切り替わらない。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase1_integrity.py",
    },
    {
        "story_id": "FILE-001",
        "epic": "FILE",
        "title": "文書を親と子に分けて取り込む",
        "steps": "1. PDF / Office / CSV / HTML / MD をパーサへ渡す。2. マクロと remote HTML を拒否する。",
        "expected_behavior": "読める文書はチャンクになり、マクロとリモート HTML は ERR_FILE_MACRO / ERR_FILE_REMOTE_HTML になる。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase3_parsers.py",
    },
    {
        "story_id": "MYSQL-001",
        "epic": "MYSQL",
        "title": "テンプレートとカスタム SQL をジョブに渡す",
        "steps": "1. mysql_templates と custom_sql を付ける。2. ジョブを作る。3. sources.config_json を見る。",
        "expected_behavior": "template と custom_sql が残り、パスワード平文は残らない。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase4_mysql.py",
    },
    {
        "story_id": "SEARCH-001",
        "epic": "SEARCH",
        "title": "BM25 だけのヒットを既定閾値で落とさない",
        "steps": "1. dense が閾値未満で sparse_rank が付いた候補を渡す。2. 既定 0.60 で検索する。",
        "expected_behavior": "sparse_rank が近い BM25 ヒットは残り、閾値 0.85 では落ちる。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase2_search.py",
    },
    {
        "story_id": "DIFY-001",
        "epic": "DIFY",
        "title": "Dify の外部知識 API で検索する",
        "steps": "1. Bearer 付きで knowledge_id 空を送る。2. 未知 ID を送る。3. 実在 RAG を検索する。4. 鍵なしで LAN を起動する。",
        "expected_behavior": "空 ID は 200 records []。未知 ID は 404 error_code 2001。認証失敗は 403 error_code 1002。非 loopback は API キーが無いと起動しない。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase5_dify.py",
    },
    {
        "story_id": "EVAL-001",
        "epic": "EVAL",
        "title": "Recall と Hard-negative を保存する",
        "steps": "1. 質問セットを評価する。2. evaluation_runs を読む。3. ベースライン件数を数える。",
        "expected_behavior": "Recall@5 / MRR@10 / Zero-result / Hard-negative 1位率が実測のみで gate され、未測定は不合格。ベースラインは 25 問以上、Hard-negative は 8 問以上。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase6_eval.py",
    },
    {
        "story_id": "PURGE-001",
        "epic": "PURGE",
        "title": "RAG を復元できない形で消す",
        "steps": "1. RAG を作る。2. purge する。3. SQLite と secret を確認する。",
        "expected_behavior": "関連行と secret が消え、uploads 外の元ファイルは残る。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_purge.py",
    },
    {
        "story_id": "UI-001",
        "epic": "UI",
        "title": "ツール名の直下に版が出る",
        "steps": "1. GET /api/v1/version を呼ぶ。2. 画面は data-app-version にその版を出す。",
        "expected_behavior": "version は pyproject.toml の 0.2.1 と一致し、ハードコードしない。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase6_eval.py::test_package_version_matches_pyproject",
    },
    {
        "story_id": "DIFY-010",
        "epic": "DIFY",
        "title": "Dify の閾値は返却スコアに掛かる",
        "steps": "1. score 0.2 と 0.9 のヒットを返す。2. score_threshold 0.5 で retrieval する。",
        "expected_behavior": "検索側の dense 閾値は 0。返却は 0.9 のレコードだけ。dify-config に SSRF 許可のヒントがある。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase5_dify.py",
    },
    {
        "story_id": "MYSQL-020",
        "epic": "MYSQL",
        "title": "取り込み失敗では増分カーソルを進めない",
        "steps": "1. 1行取ったあとストリームを失敗させる。2. sync_cursor を読む。",
        "expected_behavior": "取れた行は残り、sync_cursor は NULL のまま。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase4_mysql.py::test_failed_stream_does_not_advance_cursor",
    },
    {
        "story_id": "EVAL-020",
        "epic": "EVAL",
        "title": "評価データセットのパスを閉じる",
        "steps": "1. ../ を resolve する。2. POST /api/v1/eval/run に同じパスを送る。",
        "expected_behavior": "どちらも拒否。API は 400。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase6_eval.py::test_eval_run_endpoint_rejects_escape",
    },
    {
        "story_id": "EXPORT-010",
        "epic": "RAG",
        "title": "退行した版はエクスポートしない",
        "steps": "1. is_latest 0 と 1 の文書を入れる。2. export する。",
        "expected_behavior": "documents.jsonl.zst には is_latest=1 だけが入る。",
        "impl_status": "implemented",
        "test_method": "unit",
        "test_ref": "tests/test_phase6_eval.py::test_export_omits_retired_versions",
    },
    {
        "story_id": "BUILD-001",
        "epic": "RAG",
        "title": "ruff が通る",
        "steps": "1. uv run ruff check src tests TEST.py を実行する。",
        "expected_behavior": "exit 0。",
        "impl_status": "implemented",
        "test_method": "build",
        "test_ref": "ruff",
    },
    {
        "story_id": "BUILD-002",
        "epic": "RAG",
        "title": "mypy が通る",
        "steps": "1. uv run mypy src を実行する。",
        "expected_behavior": "exit 0。",
        "impl_status": "implemented",
        "test_method": "build",
        "test_ref": "mypy",
    },
]


def write_catalog(path: Path = OUT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(STORIES)
    return path


def main() -> None:
    written = write_catalog()
    print(written)


if __name__ == "__main__":
    main()
