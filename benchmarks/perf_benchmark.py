"""验收规模性能基准: 5000 端口 / 10001 tick / 约 100 万条变更记录。

独立进程运行, 输出 JSON:
  import_seconds       导入耗时
  peak_rss_bytes       全过程峰值常驻内存
  view_p50/p95/max_ms  查看 32 端口耗时分布
  locate_p50/p95/max_ms 单次定位(最多 64 断点)耗时分布
"""
import json
import os
import random
import resource
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trace_debugger import Breakpoint, Condition, TraceDebugger

N_PORTS = 5000
END_TICK = 10000
CLOCK_PORTS = [0, 1]          # 每 tick 都变的时钟端口
HOT_PORTS = list(range(2, 12))  # 每 10 tick 变化的高频端口
RANDOM_PER_TICK = 97          # 每 tick 随机变更的端口数
MIB = 1024 * 1024


def gen_changes(seed=20261003):
    """逐条产生变更记录, tick 非递减, 同 tick 同端口不重复。"""
    rng = random.Random(seed)
    for p in range(N_PORTS):
        yield (0, p, rng.randint(0, 99))
    for t in range(1, END_TICK + 1):
        for p in CLOCK_PORTS:
            yield (t, p, rng.randint(0, 99))
        if t % 10 == 0:
            for p in HOT_PORTS:
                yield (t, p, rng.randint(0, 99))
        for p in rng.sample(range(12, N_PORTS), RANDOM_PER_TICK):
            yield (t, p, rng.randint(0, 99))


def peak_rss_bytes():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 返回字节, Linux 返回 KiB
    return rss if sys.platform == "darwin" else rss * 1024


def percentile(sorted_vals, pct):
    idx = max(0, min(len(sorted_vals) - 1, int(round(pct / 100 * len(sorted_vals))) - 1))
    return sorted_vals[idx]


def main():
    db_path = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv else "/tmp/perf_trace.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    rng = random.Random(777)

    td = TraceDebugger(db_path)
    t0 = time.perf_counter()
    n_records = 0
    def counted():
        nonlocal n_records
        for rec in gen_changes():
            n_records += 1
            yield rec
    td.import_trace(counted(), END_TICK)
    import_seconds = time.perf_counter() - t0

    # --- 查看: 300 次, 每次 32 个端口 ---
    view_times = []
    for _ in range(300):
        tick = rng.randint(0, END_TICK)
        ports = rng.sample(range(N_PORTS), 32)
        t0 = time.perf_counter()
        td.view(tick, ports)
        view_times.append((time.perf_counter() - t0) * 1000)

    # --- 定位: 混合场景, 每次调用都更换断点条件 ---
    def random_bps(n_bp, port_pool, const_lo=0, const_hi=99, impossible=False):
        bps = []
        for _ in range(n_bp):
            conds = []
            for _ in range(rng.randint(1, 4)):
                const = 10_000 if impossible else rng.randint(const_lo, const_hi)
                conds.append(Condition(
                    rng.choice(port_pool), rng.choice(["<", "==", ">="]), const))
            bps.append(Breakpoint(tuple(conds)))
        return bps

    locate_times = []
    scenarios = (
        ["random"] * 150        # 随机端口/常量/方向/起点
        + ["no_hit"] * 30       # 不可能条件 -> 全程扫描无命中
        + ["hot"] * 30          # 条件集中在高频变化端口
        + ["clock"] * 30        # 涉及时钟端口(每 tick 变化) + 随机端口
    )
    rng.shuffle(scenarios)
    for sc in scenarios:
        if sc == "random":
            bps = random_bps(64, list(range(N_PORTS)))
        elif sc == "no_hit":
            bps = random_bps(64, list(range(N_PORTS)), impossible=True)
        elif sc == "hot":
            bps = random_bps(64, HOT_PORTS)
        else:
            pool = CLOCK_PORTS + rng.sample(range(12, N_PORTS), 20)
            bps = random_bps(64, pool)
        start = rng.randint(0, END_TICK)
        direction = rng.choice(["forward", "backward"])
        t0 = time.perf_counter()
        td.locate(start, bps, direction)
        locate_times.append((time.perf_counter() - t0) * 1000)

    view_times.sort()
    locate_times.sort()
    result = {
        "records": n_records,
        "ports": N_PORTS,
        "ticks": END_TICK + 1,
        "import_seconds": round(import_seconds, 3),
        "peak_rss_bytes": peak_rss_bytes(),
        "view_calls": len(view_times),
        "view_p50_ms": round(percentile(view_times, 50), 2),
        "view_p95_ms": round(percentile(view_times, 95), 2),
        "view_max_ms": round(view_times[-1], 2),
        "locate_calls": len(locate_times),
        "locate_p50_ms": round(percentile(locate_times, 50), 2),
        "locate_p95_ms": round(percentile(locate_times, 95), 2),
        "locate_max_ms": round(locate_times[-1], 2),
    }
    td.close()
    if os.path.exists(db_path):
        os.remove(db_path)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
