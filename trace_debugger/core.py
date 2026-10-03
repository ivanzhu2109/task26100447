"""离线仿真轨迹调试库核心类型与接口。"""
from __future__ import annotations

import operator
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Literal, Optional, Sequence, Tuple, Any

Op = Literal["<", "==", ">="]

_OP_FUNCS = {"<": operator.lt, "==": operator.eq, ">=": operator.ge}

# 一条端口变更记录: (tick, 端口标识, 整数值)
Change = Tuple[int, Any, int]

_IMPORT_BATCH = 10000


@dataclass(frozen=True)
class Condition:
    """单个比较条件: 端口值 op 常量。"""
    port: Any
    op: Op
    value: int


@dataclass(frozen=True)
class Breakpoint:
    """断点: 1~4 个条件，全部成立才算成立。"""
    conditions: Tuple[Condition, ...]


@dataclass(frozen=True)
class LocateResult:
    """定位结果: 触发 tick 与该 tick 触发的全部断点下标。"""
    tick: int
    breakpoints: Tuple[int, ...]


class TraceDebugger:
    """基于 SQLite 的离线轨迹调试器。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=OFF")
        self._conn.execute("PRAGMA synchronous=OFF")
        self._end_tick: Optional[int] = None
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if row:
            r = self._conn.execute("SELECT value FROM meta WHERE key='end_tick'").fetchone()
            if r:
                self._end_tick = int(r[0])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TraceDebugger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def end_tick(self) -> int:
        if self._end_tick is None:
            raise RuntimeError("轨迹尚未导入")
        return self._end_tick

    def import_trace(self, changes: Iterable[Change], end_tick: int) -> None:
        """导入已结束仿真的端口变更记录流（tick 非递减，逐条提供）。"""
        if end_tick < 0 or end_tick > 10000:
            raise ValueError("end_tick 必须在 0..10000 之间")
        cur = self._conn.cursor()
        cur.execute("DROP TABLE IF EXISTS changes")
        cur.execute("DROP TABLE IF EXISTS meta")
        cur.execute(
            "CREATE TABLE changes (tick INTEGER NOT NULL, port NOT NULL, value INTEGER NOT NULL)"
        )
        cur.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        batch = []
        for rec in changes:
            batch.append(rec)
            if len(batch) >= _IMPORT_BATCH:
                cur.executemany("INSERT INTO changes VALUES (?,?,?)", batch)
                batch.clear()
        if batch:
            cur.executemany("INSERT INTO changes VALUES (?,?,?)", batch)
        cur.execute(
            "CREATE INDEX idx_changes_port_tick ON changes (port, tick)"
        )
        cur.execute("INSERT INTO meta VALUES ('end_tick', ?)", (end_tick,))
        self._conn.commit()
        self._end_tick = end_tick

    def view(self, tick: int, ports: Sequence[Any]) -> dict:
        """查看某 tick 多个端口的状态（同 tick 全部变更同时生效）。"""
        if tick < 0 or tick > self.end_tick:
            raise ValueError(f"tick 超出范围 [0, {self.end_tick}]: {tick}")
        cur = self._conn.cursor()
        result = {}
        for port in ports:
            result[port] = self._value_at(cur, port, tick)
        return result

    @staticmethod
    def _value_at(cur: sqlite3.Cursor, port: Any, tick: int) -> Optional[int]:
        row = cur.execute(
            "SELECT value FROM changes WHERE port = ? AND tick <= ? "
            "ORDER BY tick DESC LIMIT 1",
            (port, tick),
        ).fetchone()
        return row[0] if row is not None else None

    def locate(
        self,
        start_tick: int,
        breakpoints: Sequence[Breakpoint],
        direction: Literal["forward", "backward"] = "forward",
    ) -> Optional[LocateResult]:
        """从 start_tick（不含）出发定位最近的断点触发。

        触发定义为: 断点条件从上一 tick 不成立变为当前 tick 成立（时间递增
        方向的上升沿），正反向定位均按此定义。返回该方向最近的触发 tick
        及该 tick 触发的全部断点下标；无命中返回 None。
        """
        if direction not in ("forward", "backward"):
            raise ValueError(f"非法方向: {direction!r}")
        end_tick = self.end_tick
        if start_tick < 0 or start_tick > end_tick:
            raise ValueError(f"start_tick 超出范围 [0, {end_tick}]: {start_tick}")
        bps = [self._normalize_breakpoint(bp) for bp in breakpoints]
        if not (1 <= len(bps) <= 64):
            raise ValueError("断点数量必须在 1..64 之间")

        forward = direction == "forward"
        if forward:
            init_tick, lo, hi = start_tick, start_tick + 1, end_tick
        else:
            # 反向: 在 [1, start-1] 内找最后一个上升沿。沿时间递增方向扫描
            # 并记录最后一次触发，语义等价于从 start 倒序找最近触发。
            init_tick, lo, hi = 0, 1, start_tick - 1
            if hi < lo:
                return None

        # 展开为扁平条件表，按端口索引；断点用“假条件计数”维护成立状态。
        cond_port: list = []
        cond_func: list = []
        cond_const: list = []
        cond_bp: list = []
        conds_by_port: dict = {}
        for bi, bp in enumerate(bps):
            for port, op, const in bp:
                ci = len(cond_port)
                cond_port.append(port)
                cond_func.append(_OP_FUNCS[op])
                cond_const.append(const)
                cond_bp.append(bi)
                conds_by_port.setdefault(port, []).append(ci)
        watched = list(conds_by_port)

        cur = self._conn.cursor()
        state = {p: self._value_at(cur, p, init_tick) for p in watched}
        n_bp = len(bps)
        cond_truth = [False] * len(cond_port)
        false_count = [0] * n_bp
        for ci, port in enumerate(cond_port):
            v = state[port]
            ok = v is not None and cond_func[ci](v, cond_const[ci])
            cond_truth[ci] = ok
            if not ok:
                false_count[cond_bp[ci]] += 1

        placeholders = ",".join("?" for _ in watched)
        rows = cur.execute(
            f"SELECT tick, port, value FROM changes "
            f"WHERE tick >= ? AND tick <= ? AND port IN ({placeholders}) ORDER BY tick",
            (lo, hi, *watched),
        )

        pending_tick: Optional[int] = None
        dirty_ports: list = []
        last_hit: Optional[LocateResult] = None

        def fire_check(tick: int) -> Optional[LocateResult]:
            # 两阶段: 先应用本 tick 全部变更引起的条件真值更新，
            # 再按净变化判定上升沿，避免同 tick 中间状态被误判为触发。
            touched: dict = {}
            for port in dirty_ports:
                v = state[port]
                for ci in conds_by_port[port]:
                    new = cond_func[ci](v, cond_const[ci])
                    if new != cond_truth[ci]:
                        cond_truth[ci] = new
                        bi = cond_bp[ci]
                        if bi not in touched:
                            touched[bi] = false_count[bi]
                        false_count[bi] += -1 if new else 1
            fired = [
                bi for bi, old_fc in touched.items()
                if old_fc > 0 and false_count[bi] == 0
            ]
            if fired:
                return LocateResult(tick=tick, breakpoints=tuple(sorted(fired)))
            return None

        for tick, port, value in rows:
            if pending_tick is None:
                pending_tick = tick
            elif tick != pending_tick:
                hit = fire_check(pending_tick)
                if hit is not None:
                    if forward:
                        return hit
                    last_hit = hit
                dirty_ports.clear()
                pending_tick = tick
            state[port] = value
            if port in conds_by_port:
                dirty_ports.append(port)
        if pending_tick is not None:
            hit = fire_check(pending_tick)
            if hit is not None:
                if forward:
                    return hit
                last_hit = hit
        return last_hit

    @staticmethod
    def _normalize_breakpoint(bp: object) -> Tuple[Tuple[Any, str, int], ...]:
        if isinstance(bp, Breakpoint):
            conds = bp.conditions
        else:
            conds = tuple(bp)  # type: ignore[arg-type]
        out = []
        for c in conds:
            if isinstance(c, Condition):
                port, op, value = c.port, c.op, c.value
            else:
                port, op, value = c  # type: ignore[misc]
            if op not in ("<", "==", ">="):
                raise ValueError(f"不支持的比较运算: {op!r}")
            out.append((port, op, int(value)))
        if not (1 <= len(out) <= 4):
            raise ValueError("每个断点需包含 1..4 个比较条件")
        return tuple(out)

