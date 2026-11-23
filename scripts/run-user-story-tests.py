"""ストーリーを unit / build で実行し、結果 CSV と Epic 集計を書く。"""

from __future__ import annotations

import csv
import importlib.util
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "user_story_catalog",
    Path(__file__).resolve().parent / "user-story-catalog.py",
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("user-story-catalog.py を読めません")
_CATALOG = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CATALOG)
COLUMNS = _CATALOG.COLUMNS
STORIES = _CATALOG.STORIES
write_catalog = _CATALOG.write_catalog

DOCS = ROOT / "docs" / "user-stories"
RESULTS = DOCS / "test-results.csv"
SUMMARY = DOCS / "summary.md"


def _run(cmd: list[str]) -> tuple[str, str]:
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    detail = (completed.stdout + "\n" + completed.stderr).strip()
    status = "pass" if completed.returncode == 0 else "fail"
    return status, detail[-500:]


def main() -> int:
    write_catalog()
    DOCS.mkdir(parents=True, exist_ok=True)
    unit_status, unit_detail = _run([sys.executable, "-m", "pytest", "-q"])
    ruff_status, ruff_detail = _run([sys.executable, "-m", "ruff", "check", "src", "tests", "TEST.py"])
    mypy_status, mypy_detail = _run([sys.executable, "-m", "mypy", "src"])
    build = {"ruff": (ruff_status, ruff_detail), "mypy": (mypy_status, mypy_detail)}

    rows: list[dict[str, str]] = []
    for story in STORIES:
        row = {key: story[key] for key in COLUMNS}
        if story["test_method"] == "unit":
            row["result"] = unit_status
            row["detail"] = unit_detail.replace("\n", " ")
        elif story["test_method"] == "build":
            status, detail = build[story["test_ref"]]
            row["result"] = status
            row["detail"] = detail.replace("\n", " ")
        else:
            row["result"] = "skip"
            row["detail"] = "unsupported test_method"
        rows.append(row)

    fieldnames = [*COLUMNS, "result", "detail"]
    with RESULTS.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    counts: Counter[str] = Counter(row["result"] for row in rows)
    by_epic: dict[str, Counter[str]] = {}
    for row in rows:
        by_epic.setdefault(row["epic"], Counter())[row["result"]] += 1
    lines = [
        "# User story results",
        "",
        f"- pass: {counts['pass']}",
        f"- fail: {counts['fail']}",
        f"- skip: {counts['skip']}",
        "",
        "| Epic | pass | fail | skip |",
        "|---|---:|---:|---:|",
    ]
    for epic, counter in by_epic.items():
        lines.append(f"| {epic} | {counter['pass']} | {counter['fail']} | {counter['skip']} |")
    SUMMARY.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(SUMMARY)
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
