"""Scale benchmark for tracedbg: 5000 ports, 10001 ticks, 1M records.

Runs as a standalone process so peak RSS reflects only the library
workload. Prints a JSON report on stdout:

    {
      "records": ..., "import_seconds": ..., "peak_rss_mib": ...,
      "view":  {"n": ..., "p50_ms": ..., "p95_ms": ..., "max_ms": ...},
      "locate": {...},
      "view_queries":  [{"tick":..., "ports":[...], "result":{...}}, ...],
      "locate_queries": [{"start":..., "direction":..., "breakpoints":...,
                          "result":...}, ...]
    }

The query specs and results are echoed so an external test can verify
them against an independent reference model.
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import sys
import time

from tracedbg import Direction, TraceDebugger

NUM_PORTS = 5000
END_TICK = 10000
CHANGES_PER_TICK = 100  # 5000 + 100*10000 = 1,005,000 records
VALUE_RANGE = 100


def generate_records(seed, num_ports=NUM_PORTS, end_tick=END_TICK,
                     changes_per_tick=CHANGES_PER_TICK):
    """Deterministic record stream: tick 0 initialises every port, then
    each tick changes a random subset of ports. Yields (tick, port, value)."""
    rng = random.Random(seed)
    ports = list(range(num_ports))
    current = {}
    for port in ports:
        value = rng.randrange(VALUE_RANGE)
        current[port] = value
        yield 0, f"p{port}", value
    for tick in range(1, end_tick + 1):
        for port in rng.sample(ports, changes_per_tick):
            value = rng.randrange(VALUE_RANGE)
            while value == current[port]:
                value = rng.randrange(VALUE_RANGE)
            current[port] = value
            yield tick, f"p{port}", value


def generate_queries(seed, end_tick=END_TICK, num_ports=NUM_PORTS,
                     n_views=200, n_locates=200):
    """Deterministic query workload. Adjacent locate calls use freshly
    generated breakpoint sets (first-time conditions included); every
    fourth locate reuses an earlier set to exercise cached conditions."""
    rng = random.Random(seed)
    views = []
    for _ in range(n_views):
        tick = rng.randint(0, end_tick)
        ports = [f"p{p}" for p in rng.sample(range(num_ports), 32)]
        views.append((tick, ports))
    ops = ["<", "==", ">="]

    def fresh_bps():
        bps = []
        for _ in range(64):
            conds = []
            for _ in range(rng.randint(1, 4)):
                conds.append((f"p{rng.randrange(num_ports)}",
                              rng.choice(ops), rng.randrange(VALUE_RANGE)))
            bps.append(conds)
        return bps

    locates = []
    history = []
    for i in range(n_locates):
        if history and i % 4 == 3:
            bps = rng.choice(history)
        else:
            bps = fresh_bps()
            history.append(bps)
        start = rng.randint(0, end_tick)
        direction = rng.choice(["forward", "backward"])
        locates.append((start, bps, direction))
    return views, locates


def peak_rss_mib():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1,
                     int(len(sorted_vals) * pct + 0.999999) - 1))
    return sorted_vals[idx]


def stats(times_ms):
    ordered = sorted(times_ms)
    return {
        "n": len(ordered),
        "p50_ms": round(percentile(ordered, 0.50), 3),
        "p95_ms": round(percentile(ordered, 0.95), 3),
        "max_ms": round(ordered[-1], 3) if ordered else 0.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--query-seed", type=int, default=777)
    parser.add_argument("--views", type=int, default=200)
    parser.add_argument("--locates", type=int, default=200)
    args = parser.parse_args()

    dbg = TraceDebugger(args.db)
    t0 = time.perf_counter()
    count = dbg.import_trace(generate_records(args.seed), end_tick=END_TICK)
    import_s = time.perf_counter() - t0

    views, locates = generate_queries(
        args.query_seed, n_views=args.views, n_locates=args.locates)

    view_times = []
    view_reports = []
    for tick, ports in views:
        t0 = time.perf_counter()
        result = dbg.view(tick, ports)
        view_times.append((time.perf_counter() - t0) * 1000)
        view_reports.append({
            "tick": tick,
            "ports": ports,
            "result": {str(k): v for k, v in result.items()},
        })

    locate_times = []
    locate_reports = []
    for start, bps, direction in locates:
        t0 = time.perf_counter()
        hit = dbg.locate(start, bps, direction)
        locate_times.append((time.perf_counter() - t0) * 1000)
        locate_reports.append({
            "start": start,
            "direction": direction,
            "breakpoints": bps,
            "result": None if hit is None else {
                "tick": hit.tick,
                "breakpoint_indices": list(hit.breakpoint_indices),
            },
        })

    rss = peak_rss_mib()
    dbg.close()
    report = {
        "records": count,
        "import_seconds": round(import_s, 3),
        "peak_rss_mib": round(rss, 1),
        "view": stats(view_times),
        "locate": stats(locate_times),
        "view_queries": view_reports,
        "locate_queries": locate_reports,
    }
    json.dump(report, sys.stdout)
    print()


if __name__ == "__main__":
    main()
