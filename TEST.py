#!/usr/bin/env python3
"""DB2RAG 運用検証。実装後は必ず実行する。

    uv run python TEST.py
    uv run python TEST.py --skip-docker
    uv run python TEST.py --hash-embed   # Ruri なしの煙テスト（検索品質は見ない）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent
Status = Literal["pass", "fail", "skip", "warn"]


def _print(text: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("utf-8", "replace").decode("utf-8", "replace"))


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    seconds: float


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: Status, detail: str, seconds: float) -> None:
        self.results.append(CheckResult(name, status, detail, seconds))

    @property
    def failed(self) -> bool:
        return any(r.status == "fail" for r in self.results)

    def print_summary(self) -> None:
        _print("\n=== DB2RAG TEST.py summary ===")
        _print(f"{'STATUS':<6} {'SEC':>8}  NAME")
        for item in self.results:
            _print(f"  [{item.status.upper():<4}] {item.seconds:7.2f}s  {item.name}: {item.detail}")
        total = sum(r.seconds for r in self.results)
        _print(f"  TOTAL {total:.2f}s")


def timed(fn):  # type: ignore[no-untyped-def]
    started = time.perf_counter()
    try:
        status, detail = fn()
    except Exception as exc:
        return "fail", f"{type(exc).__name__}: {exc}", time.perf_counter() - started
    return status, detail, time.perf_counter() - started


def http_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 60) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return resp.status, parsed
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {method} {url} -> {exc.code}: {body[:400]}") from exc
    except URLError as exc:
        raise RuntimeError(f"HTTP {method} {url} failed: {exc}") from exc


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd or REPO),
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def docker_up(report: Report) -> None:
    started = time.perf_counter()
    probe = run(["docker", "info"], timeout=30)
    if probe.returncode != 0:
        report.add("P0 docker", "warn", "Docker daemon is not running", time.perf_counter() - started)
        return
    up = run(["docker", "compose", "up", "-d"], timeout=180)
    if up.returncode != 0:
        report.add("P0 compose", "fail", (up.stderr or up.stdout)[-400:], time.perf_counter() - started)
        return
    deadline = time.time() + 90
    last = ""
    while time.time() < deadline:
        try:
            code, body = http_json("GET", "http://127.0.0.1:6333/", timeout=3)
            last = str(body)
            if code == 200:
                break
        except Exception as exc:
            last = str(exc)
            time.sleep(2)
    report.add("P0 compose", "pass", last[:180], time.perf_counter() - started)


def wait_api(base: str, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            code, body = http_json("GET", f"{base}/api/v1/health", timeout=5)
            if code == 200:
                return
            last = str(body)
        except Exception as exc:
            last = str(exc)
            time.sleep(1)
    raise RuntimeError(f"API did not become healthy: {last}")


def wait_job(base: str, job_id: str, timeout: int = 1800) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        _, body = http_json("GET", f"{base}/api/v1/jobs/{job_id}", timeout=30)
        last = body
        status = body.get("status")
        if status in {"completed", "failed", "cancelled"}:
            return body
        time.sleep(2)
    raise RuntimeError(f"job timeout: {last}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-docker", action="store_true")
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument("--hash-embed", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8781")
    parser.add_argument("--keep-server", action="store_true")
    args = parser.parse_args()
    report = Report()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    if args.hash_embed:
        env["DB2RAG_EMBEDDING_BACKEND"] = "hash"

    started = time.perf_counter()
    gen = run([sys.executable, "-m", "db2rag", "testdata"], timeout=60)
    if gen.returncode != 0:
        report.add("testdata", "fail", gen.stderr[-400:], time.perf_counter() - started)
        report.print_summary()
        return 1
    report.add("testdata", "pass", gen.stdout.replace("\n", " | ")[:300], time.perf_counter() - started)

    if not args.skip_docker:
        docker_up(report)
    else:
        report.add("P0 docker", "skip", "skipped", 0)

    if not args.skip_pytest:
        started = time.perf_counter()
        pytest = run([sys.executable, "-m", "pytest", "-q"], timeout=120)
        status: Status = "pass" if pytest.returncode == 0 else "fail"
        report.add("pytest", status, (pytest.stdout + pytest.stderr)[-400:], time.perf_counter() - started)

    # P1 Qdrant
    started = time.perf_counter()
    try:
        code, body = http_json("GET", "http://127.0.0.1:6333/", timeout=5)
        version = str(body.get("version", ""))
        if version.startswith("1.19"):
            report.add("P1 qdrant", "pass", f"version={version}", time.perf_counter() - started)
        elif version:
            report.add("P1 qdrant", "warn", f"version={version} (expected 1.19.x)", time.perf_counter() - started)
        else:
            report.add("P1 qdrant", "warn", f"connected but version unknown: {body}", time.perf_counter() - started)
    except Exception as exc:
        report.add("P1 qdrant", "warn", f"HTTP Qdrant unavailable, TEST will try embedded path: {exc}", time.perf_counter() - started)
        env["DB2RAG_QDRANT_PATH"] = str(REPO / "data" / "qdrant_local")

    log_path = REPO / "data" / "test-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "db2rag", "serve", "--host", "127.0.0.1", "--port", "8781"],
        cwd=str(REPO),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        started = time.perf_counter()
        wait_api(args.api_url, timeout=300)
        code, health = http_json("GET", f"{args.api_url}/api/v1/health")
        warnings = health.get("warnings") or []
        gpu = health.get("gpu") or {}
        status = "pass" if health.get("ok") else "warn"
        if not gpu.get("cuda"):
            status = "warn"
        report.add(
            "P5 health",
            status,
            f"ok={health.get('ok')} gpu={gpu.get('device')} warnings={warnings}",
            time.perf_counter() - started,
        )

        started = time.perf_counter()
        from db2rag.config import get_settings
        from db2rag.db import connect, migrate, schema_version

        settings = get_settings()
        conn = connect(settings.sqlite_path())
        migrate(conn)
        ver = schema_version(conn)
        conn.close()
        report.add("P3 sqlite", "pass" if ver >= 1 else "fail", f"schema_version={ver}", time.perf_counter() - started)

        started = time.perf_counter()
        try:
            _, golden = http_json("POST", f"{args.api_url}/api/v1/embed/golden", {}, timeout=300)
            report.add(
                "P2 ruri-golden",
                "pass" if golden.get("ok") else "fail",
                json.dumps(golden, ensure_ascii=False),
                time.perf_counter() - started,
            )
        except Exception as exc:
            report.add("P2 ruri-golden", "fail", str(exc), time.perf_counter() - started)

        files_dir = REPO / "testdata" / "files"
        paths = [str(p) for p in files_dir.iterdir() if p.is_file()]
        started = time.perf_counter()
        _, created = http_json(
            "POST",
            f"{args.api_url}/api/v1/jobs",
            {
                "mode": "create",
                "display_name": "testdata-files",
                "acl_tag": "internal",
                "source_type": "file",
                "paths": paths,
            },
            timeout=60,
        )
        job = wait_job(args.api_url, created["job_id"])
        ingest_status: Status = "pass" if job.get("status") == "completed" else "fail"
        report.add(
            "P4 ingest-files",
            ingest_status,
            f"job={job.get('status')} items_done={job.get('work_units_done')} rag={created.get('rag_id')}",
            time.perf_counter() - started,
        )
        rag_id = created.get("rag_id")

        queries = [
            ("有給休暇の申請期限", "10日前"),
            ("通勤手当", "15000"),
            ("TOEIC", "8000"),
            ("在宅勤務", "週2日"),
            ("育児休業", "1歳"),
        ]
        search_fail = 0
        started = time.perf_counter()
        for query, needle in queries:
            q0 = time.perf_counter()
            _, result = http_json(
                "POST",
                f"{args.api_url}/api/v1/search",
                {"rag_id": rag_id, "query": query, "top_k": 10, "score_threshold": 0.0, "filters": {"acl_tags": ["internal"]}},
                timeout=120,
            )
            blob = json.dumps(result, ensure_ascii=False)
            hit = needle in blob
            hits = result.get("hits") or []
            if not hit:
                search_fail += 1
            report.add(
                f"search:{query}",
                "pass" if hit else "fail",
                f"hits={len(hits)} needle={needle} top={(hits[0].get('snippet') if hits else '')[:80]}",
                time.perf_counter() - q0,
            )
        q_bad0 = time.perf_counter()
        _, noise = http_json(
            "POST",
            f"{args.api_url}/api/v1/search",
            {
                "rag_id": rag_id,
                "query": "量子ビット誤り訂正符号の閾値定理",
                "top_k": 5,
                "score_threshold": 0.85,
                "filters": {"acl_tags": ["internal"]},
            },
            timeout=120,
        )
        noise_hits = noise.get("hits") or []
        report.add(
            "search:unrelated-high-threshold",
            "pass" if len(noise_hits) == 0 else "warn",
            f"hits={len(noise_hits)} (expect 0 at threshold 0.85)",
            time.perf_counter() - q_bad0,
        )
        report.add(
            "P4 search-files",
            "pass" if search_fail == 0 else "fail",
            f"misses={search_fail}/{len(queries)}",
            time.perf_counter() - started,
        )

        started = time.perf_counter()
        mysql_ok = False
        mysql_last = ""
        for _ in range(30):
            try:
                _, mysql_test = http_json(
                    "POST",
                    f"{args.api_url}/api/v1/mysql/test",
                    {
                        "host": "127.0.0.1",
                        "port": 3307,
                        "user": "db2rag_ro",
                        "password": "db2rag_ro",
                        "database": "hr",
                        "ssl_mode": "DISABLED",
                    },
                    timeout=8,
                )
                mysql_ok = bool(mysql_test.get("ok"))
                mysql_last = json.dumps(mysql_test, ensure_ascii=False)
                if mysql_ok:
                    break
            except Exception as exc:
                mysql_last = str(exc)
                time.sleep(2)
        report.add("mysql-test", "pass" if mysql_ok else "warn", mysql_last, time.perf_counter() - started)

        if mysql_ok:
            started = time.perf_counter()
            _, mysql_job = http_json(
                "POST",
                f"{args.api_url}/api/v1/jobs",
                {
                    "mode": "append",
                    "rag_id": rag_id,
                    "display_name": "testdata-files",
                    "acl_tag": "internal",
                    "source_type": "mysql",
                    "mysql": {
                        "host": "127.0.0.1",
                        "port": 3307,
                        "user": "db2rag_ro",
                        "password": "db2rag_ro",
                        "database": "hr",
                        "ssl_mode": "DISABLED",
                    },
                    "tables": ["hr.regulations", "hr.faq_entries"],
                },
                timeout=60,
            )
            job2 = wait_job(args.api_url, mysql_job["job_id"], timeout=1800)
            report.add(
                "P4 ingest-mysql",
                "pass" if job2.get("status") == "completed" else "fail",
                f"job={job2.get('status')} items={job2.get('work_units_done')}",
                time.perf_counter() - started,
            )
            s0 = time.perf_counter()
            _, result = http_json(
                "POST",
                f"{args.api_url}/api/v1/search",
                {"rag_id": rag_id, "query": "育児休業はいつまで", "top_k": 10, "score_threshold": 0.0, "filters": {"acl_tags": ["internal"]}},
                timeout=120,
            )
            blob = json.dumps(result, ensure_ascii=False)
            report.add(
                "search:mysql-childcare",
                "pass" if "1歳" in blob else "fail",
                (blob[:200]),
                time.perf_counter() - s0,
            )

        started = time.perf_counter()
        routes = ["/", "/wizard", "/jobs", "/progress", "/search"]
        ui_fail = 0
        for route in routes:
            try:
                req = Request(args.api_url + route)
                with urlopen(req, timeout=10) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                if "<html" not in html.lower():
                    ui_fail += 1
            except Exception:
                ui_fail += 1
        wizard_html = urlopen(args.api_url + "/wizard", timeout=10).read().decode("utf-8", errors="replace")
        w1_ok = 'data-choice-group="rag-mode"' in wizard_html and "data-auto-next" not in wizard_html.split("rag-mode")[1].split("source-type")[0]
        w2_ok = 'data-auto-next="true"' in wizard_html
        contract = "pass" if ui_fail == 0 and w1_ok and w2_ok else "fail"
        report.add(
            "P6 ui-routes",
            contract,
            f"routes_miss={ui_fail} w1_explicit={w1_ok} w2_auto={w2_ok}",
            time.perf_counter() - started,
        )

        if rag_id:
            started = time.perf_counter()
            _, exported = http_json("POST", f"{args.api_url}/api/v1/export", {"rag_id": rag_id}, timeout=120)
            report.add(
                "export",
                "pass" if exported.get("manifest_sha256") else "fail",
                json.dumps(exported, ensure_ascii=False),
                time.perf_counter() - started,
            )
    finally:
        if not args.keep_server:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
            log_handle.close()

    report.print_summary()
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
