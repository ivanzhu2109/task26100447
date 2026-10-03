"""独立核验用的朴素参照模型：直接基于内存变更表暴力计算。"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class ReferenceModel:
    """与 TraceDebugger 无关的独立实现，用于交叉核验。"""

    def __init__(self, changes: Iterable[Tuple[int, Any, int]], end_tick: int):
        self.end_tick = end_tick
        # tick -> {port: value}
        self.by_tick: Dict[int, Dict[Any, int]] = {}
        for tick, port, value in changes:
            self.by_tick.setdefault(tick, {})[port] = value
        self.ports = sorted({p for m in self.by_tick.values() for p in m})
        # 预计算每个 tick 之后的完整状态
        self.states: List[Dict[Any, int]] = []
        state: Dict[Any, int] = {}
        for t in range(end_tick + 1):
            if t in self.by_tick:
                state = {**state, **self.by_tick[t]}
            self.states.append(state)

    def view(self, tick: int, ports: Sequence[Any]) -> Dict[Any, Optional[int]]:
        return {p: self.states[tick].get(p) for p in ports}

    @staticmethod
    def _eval(conditions, state) -> bool:
        for port, op, const in conditions:
            v = state.get(port)
            if v is None:
                return False
            if op == "<":
                if not v < const:
                    return False
            elif op == "==":
                if not v == const:
                    return False
            elif op == ">=":
                if not v >= const:
                    return False
            else:
                raise ValueError(op)
        return True

    def triggers(self, breakpoints) -> Dict[int, List[int]]:
        """返回 {tick: [断点下标]}，仅含真正的上升沿（tick>=1）。"""
        bps = []
        for bp in breakpoints:
            conds = getattr(bp, "conditions", bp)
            bps.append([
                (c.port, c.op, c.value) if hasattr(c, "port") else tuple(c)
                for c in conds
            ])
        prev = [self._eval(bp, self.states[0]) for bp in bps]
        out: Dict[int, List[int]] = {}
        for t in range(1, self.end_tick + 1):
            state = self.states[t]
            cur = [self._eval(bp, state) for bp in bps]
            fired = [i for i in range(len(bps)) if cur[i] and not prev[i]]
            if fired:
                out[t] = fired
            prev = cur
        return out

    def locate(self, start_tick, breakpoints, direction):
        tr = self.triggers(breakpoints)
        if direction == "forward":
            cands = [t for t in tr if t > start_tick]
            if not cands:
                return None
            t = min(cands)
        else:
            cands = [t for t in tr if t < start_tick]
            if not cands:
                return None
            t = max(cands)
        return (t, tuple(sorted(tr[t])))
