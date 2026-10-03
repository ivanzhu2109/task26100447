# task26100447 — tracedbg

离线仿真轨迹调试库（Python + SQLite），由上层直接调用。轨迹保存在
SQLite 中，只提供三个能力：**轨迹导入**、**任意时刻状态查看**、
**双向断点定位**。无任何外部服务依赖，持久化只用 SQLite 文件。

## 用法

```python
from tracedbg import TraceDebugger, Breakpoint, Condition, Direction, Op

with TraceDebugger("trace.db") as dbg:
    # 1. 导入：records 为 (tick, port, value) 流，tick 非递减，
    #    tick 0 提供全部端口初值；end_tick <= 10000。
    dbg.import_trace(records, end_tick=10000)

    # 2. 查看：多个端口返回同一 tick 的快照。
    dbg.view(1234, ["p0", "p7"])          # -> {"p0": 3, "p7": 18}

    # 3. 定位：最多 64 个断点，每个 1~4 个比较条件（<、==、>=），
    #    从 start 出发（不含起点），返回最近触发 tick 及该 tick
    #    触发的全部断点下标；无命中返回 None。
    hit = dbg.locate(
        5000,
        [Breakpoint((Condition("p0", Op.GE, 10),
                     Condition("p7", Op.LT, 50)))],
        Direction.FORWARD,                 # 或 Direction.BACKWARD
    )
    # -> LocateResult(tick=5013, breakpoint_indices=(0,)) 或 None
```

断点也可直接用 `(port, op, const)` 元组列表传入，见
`tests/test_tracedbg.py`。端口标识支持 `str` 或 `int`（内部统一按
`str` 存储）。

## 触发语义

- 某 tick 的可见状态是该 tick 全部变更同时生效后的状态；同 tick 内
  不产生中间状态（定位与查看都遵守）。
- 断点只在"上一 tick 不成立、当前 tick 成立"的上升沿触发；持续成立
  不重复触发，失效后再次成立可再次触发；tick 0 只建立初值，不触发。
- 反向定位返回的仍是按时间递增方向定义的真实触发边沿，只是取起点
  之前最近的一个，不会生成镜像的"反向触发"。
- 查看不改变定位的起点或结果；相邻两次定位可完全更换断点条件。

## 实现要点

- 存储：`changes(tick, port, value)` 表 + `(port, tick)` 索引；导入
  分批写入后统一建索引。查看单端口即一次索引回查。
- 定位：单个比较条件的真假只在其端口变化处改变，因此每个条件归约
  为"tick 0 真假 + 翻转 tick 列表"（按条件 LRU 缓存）；断点触发点
  由其各条件翻转列表做 k 路归并求得（同 tick 的翻转全部生效后再判
  定，天然排除中间状态），正反向定位再对触发列表二分。

## 测试与实测

```
python3 -m pytest tests/          # 22 个测试（正确性 + 规模验收）
python3 benchmark.py --db /tmp/t.db   # 单独跑规模基准，输出 JSON
```

规模：5000 端口、10001 tick、1,005,000 条变更记录（本机实测，
Python 3.14 / SQLite 3.50，macOS）：

| 指标 | 要求 | 实测 |
| --- | --- | --- |
| 导入+调试全过程峰值 RSS | ≤ 128 MiB | 52.5 MiB |
| 查看 32 端口 p95 | ≤ 200 ms | 0.26 ms |
| 64 断点单次定位 p95 | ≤ 200 ms | 51.8 ms |
| 导入 1,005,000 条 | — | 1.6 s |

定位工作负载每次调用随机生成全新断点条件（首次使用计入统计），
每 4 次复用一次旧条件以覆盖缓存路径，正反向随机。

测试覆盖：同 tick 多端口变更的中间状态不触发；持续成立/失效再
成立/长时间无变化只报真实边沿；多断点同 tick 触发；全程双向遍历
正反结果完全对应；随机轨迹与独立暴力参考模型逐点比对；规模测试
中全部查看与定位结果与独立参考模型核验一致。
