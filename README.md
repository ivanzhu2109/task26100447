# trace-debugger

离线仿真轨迹调试库（Python + SQLite），由上层直接调用。轨迹保存在 SQLite 中，
只提供三个能力：**轨迹导入**、**任意时刻查看**、**双向断点定位**。

## 安装与依赖

- 仅依赖 Python 标准库（`sqlite3`），Python 3.10+。
- 测试依赖 `pytest`。

## 快速开始

```python
from trace_debugger import TraceDebugger, Breakpoint, Condition

with TraceDebugger("trace.db") as td:
    # 1. 导入：已结束仿真的端口变更记录流，tick 非递减，逐条提供
    td.import_trace(
        changes=((0, "p1", 0), (0, "p2", 1), (10, "p1", 1), (20, "p2", 0)),
        end_tick=100,   # 显式终点，0..10000
    )

    # 2. 查看：任意 tick、多个端口，返回值属于同一个 tick
    td.view(15, ["p1", "p2"])   # {'p1': 1, 'p2': 1}

    # 3. 定位：最多 64 个断点，每个断点 1..4 个条件（<、==、>=）
    bps = [Breakpoint((Condition("p1", "==", 1), Condition("p2", ">=", 1)))]
    td.locate(0, bps, "forward")     # LocateResult(tick=10, breakpoints=(0,))
    td.locate(100, bps, "backward")  # 反向定位最近的触发
```

## 语义约定

- 某 tick 的可见状态 = 该 tick 全部变更同时生效后的状态；同 tick 不暴露中间状态。
- 断点只在条件**从上一 tick 不成立变为当前 tick 成立**（上升沿）时触发；
  持续成立不重复触发，失效后再成立可再次触发；tick0 只建立初始状态。
- 反向定位仍按时间递增方向定义上升沿，返回 `start_tick` 之前（不含）最近的
  真实触发，不会把条件失效的位置当成反向触发。
- 定位不含起点；返回该方向最近触发 tick 及该 tick 触发的全部断点下标；
  无命中返回 `None`。查看不改变后续定位的起点或结果。
- 相邻两次定位可完全更换断点条件；每次定位独立求值，不依赖历史查询缓存。

## 实现要点

- 存储：单表 `changes(tick, port, value)` + `(port, tick)` 索引；导入分批写入，
  流式消费输入，内存占用与轨迹规模解耦。
- 查看：每端口一次 `tick <= T ORDER BY tick DESC LIMIT 1` 索引查询。
- 定位：只扫描断点引用端口的变更记录；断点条件展开为扁平表，用
  “假条件计数”维护每个断点的成立状态；同 tick 变更先全部应用再按净变化
  判定上升沿，杜绝中间状态误触发。反向定位沿时间递增方向扫描并取最后一个
  上升沿，语义与倒序查找最近触发完全等价。

## 测试与实测

```bash
python3 -m pytest tests/ -q
```

- `tests/test_trace_debugger.py`：业务规则逐项覆盖（同 tick 中间状态不触发、
  边沿语义、多断点同 tick、全程双向遍历对应、查看一致性、参数校验），并与
  独立参照模型 `tests/reference_model.py` 随机交叉核验。
- `tests/test_perf.py`：验收规模实测（独立子进程运行 `benchmarks/perf_benchmark.py`）。

本机实测（5000 端口 / 10001 tick / 1,005,000 条记录，含每 tick 变化的时钟
端口与高频端口，定位混合随机/无命中/高频端口场景且每次更换条件）：

| 指标 | 实测 | 要求 |
| --- | --- | --- |
| 导入耗时 | 1.24 s | — |
| 峰值常驻内存 | 30 MiB | ≤ 128 MiB |
| 查看 32 端口 p95 | 0.23 ms | ≤ 200 ms |
| 单次定位 p95 | 44 ms | ≤ 200 ms |
| 单次定位 max | 61 ms | — |
