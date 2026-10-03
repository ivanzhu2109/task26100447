"""Scale acceptance test: 5000 ports, 10001 ticks, 1,005,000 records.

Runs benchmark.py in a clean subprocess (so peak RSS measures only the
library workload), asserts the non-functional requirements, and verifies
every returned view/locate result against an independent reference model
built from the same deterministic record stream.
"""

from __future__ import annotations

import json
import subprocess
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmark import END_TICK, generate_records  # noqa: E402

SEED = 20261004
QUERY_SEED = 777
EXPECTED_RECORDS = 5000 + 100 * 10000

RSS_LIMIT_MIB = 128
P95_LIMIT_MS = 200


class RefModel:
    """Independent reference: per-port sorted arrays + bisect."""

    def __init__(self, records):
        series = {}
        for tick, port, value in records:
            series.setdefault(str(port), []).append((tick, value))
        self.ports = {}
        for port, rows in series.items():
            ticks = [t for t, _ in rows]
            values = [v for _, v in rows]
            self.ports[port] = (ticks, values)

    def value_at(self, port, tick):
        entry = self.ports.get(port)
        if entry is None:
            return None
        ticks, values = entry
        idx = bisect_right(ticks, tick) - 1
        return None if idx < 0 else values[idx]

    def cond_holds(self, cond, tick):
        port, op, const = cond
        value = self.value_at(port, tick)
        if value is None:
            return False
        if op == "<":
            return value < const
        if op == "==":
            return value == const
        assert op == ">="
        return value >= const

    def active(self, bp, tick):
        return all(self.cond_holds(c, tick) for c in bp)

    def triggers_of(self, bp):
        events = set()
        for cond in bp:
            entry = self.ports.get(cond[0])
            if entry:
                events.update(t for t in entry[0] if t >= 1)
        triggers = []
        prev = self.active(bp, 0)
        for tick in sorted(events):
            now = self.active(bp, tick)
            if now and not prev:
                triggers.append(tick)
            prev = now
        return triggers

    def locate(self, start, bps, direction):
        best = None
        indices = []
        for idx, bp in enumerate(bps):
            triggers = self.triggers_of(bp)
            if direction == "forward":
                pos = bisect_right(triggers, start)
                if pos == len(triggers):
                    continue
                tick = triggers[pos]
                if best is None or tick < best:
                    best, indices = tick, [idx]
                elif tick == best:
                    indices.append(idx)
            else:
                pos = bisect_left(triggers, start) - 1
                if pos < 0:
                    continue
                tick = triggers[pos]
                if best is None or tick > best:
                    best, indices = tick, [idx]
                elif tick == best:
                    indices.append(idx)
        return None if best is None else (best, sorted(indices))


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    db = tmp_path_factory.mktemp("scale") / "trace.db"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "benchmark.py"), "--db", str(db),
         "--seed", str(SEED), "--query-seed", str(QUERY_SEED)],
        capture_output=True, text=True, timeout=600, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def ref():
    return RefModel(generate_records(SEED))


def test_scale_and_memory(report):
    assert report["records"] == EXPECTED_RECORDS
    assert report["peak_rss_mib"] <= RSS_LIMIT_MIB, (
        f"peak RSS {report['peak_rss_mib']} MiB exceeds {RSS_LIMIT_MIB} MiB")


def test_latency_percentiles(report):
    for kind in ("view", "locate"):
        assert report[kind]["p95_ms"] <= P95_LIMIT_MS, (
            f"{kind} p95 {report[kind]['p95_ms']} ms exceeds "
            f"{P95_LIMIT_MS} ms")


def test_view_results_match_reference(report, ref):
    assert len(report["view_queries"]) == 200
    for query in report["view_queries"]:
        tick = query["tick"]
        expected = {p: ref.value_at(p, tick) for p in query["ports"]}
        assert query["result"] == expected


def test_locate_results_match_reference(report, ref):
    queries = report["locate_queries"]
    assert len(queries) == 200
    # Full independent re-computation for a representative subset.
    for query in queries[:25]:
        want = ref.locate(query["start"], query["breakpoints"],
                          query["direction"])
        got = query["result"]
        if want is None:
            assert got is None
        else:
            assert got is not None
            assert got["tick"] == want[0]
            assert sorted(got["breakpoint_indices"]) == want[1]
    # Edge-consistency check for every non-empty result: the reported
    # tick is a genuine false->true edge for exactly the reported set.
    for query in queries:
        got = query["result"]
        if got is None:
            continue
        tick, start = got["tick"], query["start"]
        if query["direction"] == "forward":
            assert tick > start
        else:
            assert tick < start
        bps = query["breakpoints"]
        expected = sorted(
            idx for idx, bp in enumerate(bps)
            if ref.active(bp, tick) and not ref.active(bp, tick - 1)
        )
        assert sorted(got["breakpoint_indices"]) == expected
