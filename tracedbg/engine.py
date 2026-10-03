"""Breakpoint trigger engine.

A breakpoint triggers at tick t when its combined condition (AND of its
comparisons) is false at t-1 and true at t. Tick 0 never triggers.
Reverse location uses the same forward-time trigger definition: it
returns the nearest real trigger before the start tick, it does not
invent mirrored "untrigger" events.

The truth value of a single comparison only changes where its port
changes, so each condition is reduced to a sorted list of transition
ticks; breakpoint triggers are found by merging those lists.
"""

from __future__ import annotations

import heapq
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from typing import Dict, List, Sequence, Tuple

from .store import TraceStore
from .types import Breakpoint, Condition, Direction, Op

_COND_CACHE_CAP = 8192
_TRIG_CACHE_CAP = 4096


def compare(value: int, op: Op, const: int) -> bool:
    """Evaluate ``value <op> const``."""
    if op is Op.LT:
        return value < const
    if op is Op.EQ:
        return value == const
    if op is Op.GE:
        return value >= const
    raise ValueError(f"unsupported operator: {op!r}")


def _cond_key(cond: Condition) -> Tuple[str, Op, int]:
    return (str(cond.port), Op(cond.op), int(cond.value))


def _lru_get(cache: "OrderedDict", key):
    try:
        value = cache.pop(key)
    except KeyError:
        return None
    cache[key] = value
    return value


def _lru_put(cache: "OrderedDict", key, value, cap: int) -> None:
    cache[key] = value
    while len(cache) > cap:
        cache.popitem(last=False)


class TriggerEngine:
    """Computes trigger ticks for breakpoints over one loaded trace."""

    def __init__(self, store: TraceStore) -> None:
        self._store = store
        self._cond_cache: "OrderedDict" = OrderedDict()
        self._trig_cache: "OrderedDict" = OrderedDict()

    def condition_transitions(self, cond: Condition) -> Tuple[bool, Tuple[int, ...]]:
        """Return (truth at tick 0, sorted ticks where the truth flips).

        Results are cached per condition so repeated locate calls with
        the same conditions stay cheap.
        """
        key = _cond_key(cond)
        hit = _lru_get(self._cond_cache, key)
        if hit is not None:
            return hit
        port, op, const = key
        series = self._store.port_series(port)
        flips: List[int] = []
        if not series:
            result = (False, ())
        else:
            truth = compare(series[0][1], op, const)
            initial = truth
            start = 1 if series[0][0] == 0 else 0
            if start == 0:
                # No tick-0 record for this port: unknown before the first
                # record, treated as not holding.
                initial = False
                truth = False
            for tick, value in series[start:]:
                now = compare(value, op, const)
                if now != truth:
                    flips.append(tick)
                    truth = now
            result = (initial, tuple(flips))
        _lru_put(self._cond_cache, key, result, _COND_CACHE_CAP)
        return result

    def breakpoint_triggers(self, bp: Breakpoint) -> Tuple[int, ...]:
        """All trigger ticks of one breakpoint, in increasing order."""
        key = frozenset(_cond_key(c) for c in bp.conditions)
        hit = _lru_get(self._trig_cache, key)
        if hit is not None:
            return hit
        parts = [self.condition_transitions(c) for c in bp.conditions]
        n = len(parts)
        states = [init for init, _ in parts]
        active = all(states)
        triggers: List[int] = []
        # k-way merge of the per-condition flip lists; all flips at the
        # same tick are applied before evaluating, so intermediate states
        # of a multi-port tick are never observed.
        heap: List[Tuple[int, int, int]] = []
        for idx, (_, flips) in enumerate(parts):
            if flips:
                heapq.heappush(heap, (flips[0], idx, 0))
        while heap:
            tick = heap[0][0]
            while heap and heap[0][0] == tick:
                _, idx, pos = heapq.heappop(heap)
                states[idx] = not states[idx]
                flips = parts[idx][1]
                if pos + 1 < len(flips):
                    heapq.heappush(heap, (flips[pos + 1], idx, pos + 1))
            now_active = all(states)
            if now_active and not active:
                triggers.append(tick)
            active = now_active
        result = tuple(triggers)
        _lru_put(self._trig_cache, key, result, _TRIG_CACHE_CAP)
        return result

    def locate(
        self,
        start_tick: int,
        breakpoints: Sequence[Breakpoint],
        direction: Direction,
    ) -> Dict[int, List[int]]:
        """Find the nearest trigger tick from ``start_tick`` (exclusive).

        Returns an empty dict when nothing triggers in that direction,
        otherwise ``{tick: [breakpoint indices triggering there]}``.
        """
        best_tick = None
        best_indices: List[int] = []
        forward = Direction(direction) is Direction.FORWARD
        for idx, bp in enumerate(breakpoints):
            triggers = self.breakpoint_triggers(bp)
            if forward:
                pos = bisect_right(triggers, start_tick)
                if pos == len(triggers):
                    continue
                tick = triggers[pos]
                if best_tick is None or tick < best_tick:
                    best_tick, best_indices = tick, [idx]
                elif tick == best_tick:
                    best_indices.append(idx)
            else:
                pos = bisect_left(triggers, start_tick) - 1
                if pos < 0:
                    continue
                tick = triggers[pos]
                if best_tick is None or tick > best_tick:
                    best_tick, best_indices = tick, [idx]
                elif tick == best_tick:
                    best_indices.append(idx)
        if best_tick is None:
            return {}
        return {best_tick: best_indices}
