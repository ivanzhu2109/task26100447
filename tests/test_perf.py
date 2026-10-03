"""验收规模性能与内存实测: 5000 端口 / 10001 tick / 约 100 万条记录。

在独立子进程中运行基准, 使峰值常驻内存测量针对干净的库进程。
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "benchmarks", "perf_benchmark.py")

MAX_RSS_MIB = 128
P95_BUDGET_MS = 200.0


@pytest.fixture(scope="module")
def bench_result(tmp_path_factory):
    db = tmp_path_factory.mktemp("perf") / "trace.db"
    out = subprocess.run(
        [sys.executable, BENCH, "--db", str(db)],
        capture_output=True, text=True, cwd=ROOT, timeout=600,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_scale(bench_result):
    assert bench_result["ports"] == 5000
    assert bench_result["ticks"] == 10001
    assert bench_result["records"] >= 1_000_000


def test_peak_rss_under_128mib(bench_result):
    rss_mib = bench_result["peak_rss_bytes"] / (1024 * 1024)
    assert rss_mib < MAX_RSS_MIB, f"峰值常驻内存 {rss_mib:.1f} MiB 超限"


def test_view_p95(bench_result):
    assert bench_result["view_p95_ms"] < P95_BUDGET_MS


def test_locate_p95(bench_result):
    assert bench_result["locate_p95_ms"] < P95_BUDGET_MS
