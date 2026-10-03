"""Correctness tests for tracedbg, checked against an independent
in-memory reference model."""

from __future__ import annotations

import random

import pytest

from tracedbg import (
    Breakpoint,
    Condition,
    Direction,
    Op,
    TraceDebugger,
)


# ---------------------------------------------------------------------
# Independent reference model
# ---------------------------------------------------------------------

class Reference:
    """Brute-force model: replays every tick from the record stream."""

    def __init__(self, records, end_tick):
        self.end_tick = end_tick
        by_tick = {}
        for tick, port, value in records:
            by_tick.setdefault(tick, []).append((port, value))
        self.states = []
        current = {}
        for tick in range(end_tick + 1):
            for port, value in by_tick.get(tick, ()):
                current[port] = value
            self.states.append(dict(current))

    def view(self, tick, ports):
        return {p: self.states[tick].get(p) for p in ports}

    @staticmethod
    def _holds(state, cond):
        value = state.get(cond.port)
        if value is None:
            return False
        if cond.op is Op.LT:
            return value < cond.value
        if cond.op is Op.EQ:
            return value == cond.value
        return value >= cond.value

    def active(self, tick, bp):
        return all(self._holds(self.states[tick], c) for c in bp.conditions)

    def triggers(self, breakpoints):
        """tick -> sorted list of breakpoint indices triggering there."""
        result = {}
        prev = [self.active(0, bp) for bp in breakpoints]
        for tick in range(1, self.end_tick + 1):
            for idx, bp in enumerate(breakpoints):
                now = self.active(tick, bp)
                if now and not prev[idx]:
                    result.setdefault(tick, []).append(idx)
                prev[idx] = now
        return result

    def locate(self, start, breakpoints, direction):
        triggers = self.triggers(breakpoints)
        if direction is Direction.FORWARD:
            ticks = [t for t in triggers if t > start]
            tick = min(ticks) if ticks else None
        else:
            ticks = [t for t in triggers if t < start]
            tick = max(ticks) if ticks else None
        if tick is None:
            return None
        return tick, tuple(triggers[tick])


def make_debugger(tmp_path, records, end_tick):
    dbg = TraceDebugger(str(tmp_path / "trace.db"))
    dbg.import_trace(records, end_tick=end_tick)
    return dbg


def bp(*conditions):
    return Breakpoint(tuple(Condition(p, Op(o), v) for p, o, v in conditions))


# ---------------------------------------------------------------------
# Required scenario: same-tick multi-port change, only the intermediate
# state satisfies the condition -> must NOT trigger.
# ---------------------------------------------------------------------

def test_same_tick_intermediate_state_not_visible(tmp_path):
    records = [
        (0, "a", 0), (0, "b", 1),
        (5, "a", 1), (5, "b", 0),   # intermediate (a=1,b=1) never visible
    ]
    dbg = make_debugger(tmp_path, records, end_tick=10)
    both = bp(("a", "==", 1), ("b", "==", 1))
    assert dbg.locate(0, [both], Direction.FORWARD) is None
    assert dbg.locate(10, [both], Direction.BACKWARD) is None
    # but each half alone does trigger
    only_a = bp(("a", "==", 1))
    hit = dbg.locate(0, [only_a], Direction.FORWARD)
    assert hit is not None and hit.tick == 5


def test_view_returns_same_tick_snapshot(tmp_path):
    records = [(0, "a", 0), (0, "b", 1), (5, "a", 1), (5, "b", 0)]
    dbg = make_debugger(tmp_path, records, end_tick=10)
    assert dbg.view(5, ["a", "b"]) == {"a": 1, "b": 0}
    assert dbg.view(4, ["a", "b"]) == {"a": 0, "b": 1}
    # ticks without any record are still viewable
    assert dbg.view(7, ["a", "b"]) == {"a": 1, "b": 0}


# ---------------------------------------------------------------------
# Trigger edges: sustained true, re-arming, long quiet gaps, tick 0.
# ---------------------------------------------------------------------

def test_sustained_true_triggers_once_then_rearms(tmp_path):
    records = [
        (0, "a", 0),
        (3, "a", 10),    # becomes true at 3
        # long gap with no records at all
        (900, "a", 1),   # false at 900
        (901, "a", 5),   # true again at 901 -> second trigger
    ]
    dbg = make_debugger(tmp_path, records, end_tick=1000)
    cond = bp(("a", ">=", 5))
    hit = dbg.locate(0, [cond], Direction.FORWARD)
    assert hit is not None and hit.tick == 3
    # still true from 3..899: no further trigger
    hit = dbg.locate(3, [cond], Direction.FORWARD)
    assert hit is not None and hit.tick == 901
    hit = dbg.locate(500, [cond], Direction.FORWARD)
    assert hit is not None and hit.tick == 901
    assert dbg.locate(901, [cond], Direction.FORWARD) is None
    # reverse finds the same real edges
    hit = dbg.locate(1000, [cond], Direction.BACKWARD)
    assert hit is not None and hit.tick == 901
    hit = dbg.locate(901, [cond], Direction.BACKWARD)
    assert hit is not None and hit.tick == 3


def test_tick0_never_triggers(tmp_path):
    records = [(0, "a", 5), (2, "a", 6)]
    dbg = make_debugger(tmp_path, records, end_tick=10)
    cond = bp(("a", ">=", 5))
    # true since tick 0: no trigger anywhere
    assert dbg.locate(0, [cond], Direction.FORWARD) is None
    assert dbg.locate(10, [cond], Direction.BACKWARD) is None


def test_multiple_breakpoints_same_tick(tmp_path):
    records = [
        (0, "a", 0), (0, "b", 0), (0, "c", 0),
        (4, "a", 1), (4, "b", 2), (4, "c", 3),
    ]
    dbg = make_debugger(tmp_path, records, end_tick=10)
    bps = [
        bp(("a", "==", 1)),
        bp(("b", ">=", 2), ("c", "<", 5)),
        bp(("a", "==", 7)),          # never triggers
        bp(("c", ">=", 3)),
    ]
    hit = dbg.locate(0, bps, Direction.FORWARD)
    assert hit is not None
    assert hit.tick == 4
    assert hit.breakpoint_indices == (0, 1, 3)
    hit = dbg.locate(10, bps, Direction.BACKWARD)
    assert hit is not None
    assert hit.tick == 4
    assert hit.breakpoint_indices == (0, 1, 3)


def test_view_does_not_affect_locate(tmp_path):
    records = [(0, "a", 0), (6, "a", 1)]
    dbg = make_debugger(tmp_path, records, end_tick=10)
    cond = bp(("a", "==", 1))
    before = dbg.locate(0, [cond], Direction.FORWARD)
    dbg.view(6, ["a"])
    dbg.view(0, ["a"])
    after = dbg.locate(0, [cond], Direction.FORWARD)
    assert before == after


# ---------------------------------------------------------------------
# Full bidirectional traversal correspondence.
# ---------------------------------------------------------------------

def test_bidirectional_traversal_correspondence(tmp_path):
    rng = random.Random(20261004)
    records = [(0, p, 0) for p in ("a", "b", "c", "d")]
    tick = 0
    while tick < 200:
        tick += rng.randint(1, 7)
        if tick > 200:
            break
        for port in rng.sample(("a", "b", "c", "d"), rng.randint(1, 3)):
            records.append((tick, port, rng.randint(0, 4)))
    end_tick = 200
    dbg = make_debugger(tmp_path, records, end_tick=end_tick)
    bps = [
        bp(("a", ">=", 2), ("b", "<", 3)),
        bp(("c", "==", 1)),
        bp(("d", ">=", 1), ("a", "<", 4), ("b", "==", 2)),
        bp(("a", "==", 0), ("d", "==", 0)),
    ]
    ref = Reference(records, end_tick)
    expected = ref.triggers(bps)

    # forward sweep collects every trigger in increasing order
    forward = {}
    pos = 0
    while True:
        hit = dbg.locate(pos, bps, Direction.FORWARD)
        if hit is None:
            break
        forward[hit.tick] = sorted(hit.breakpoint_indices)
        pos = hit.tick
    # backward sweep collects the same events in decreasing order
    backward = {}
    pos = end_tick
    while True:
        hit = dbg.locate(pos, bps, Direction.BACKWARD)
        if hit is None:
            break
        backward[hit.tick] = sorted(hit.breakpoint_indices)
        pos = hit.tick

    assert forward == expected
    assert backward == expected
    assert list(forward) == sorted(expected)
    assert list(backward) == sorted(expected, reverse=True)


# ---------------------------------------------------------------------
# Randomised cross-check against the reference model.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(8))
def test_randomised_cross_check(tmp_path, seed):
    rng = random.Random(1000 + seed)
    ports = [f"p{i}" for i in range(6)]
    end_tick = 120
    records = [(0, p, rng.randint(0, 3)) for p in ports]
    for tick in range(1, end_tick + 1):
        for port in ports:
            if rng.random() < 0.08:
                records.append((tick, port, rng.randint(0, 3)))
    dbg = make_debugger(tmp_path, records, end_tick=end_tick)
    ref = Reference(records, end_tick)

    # view cross-check on random ticks/ports
    for _ in range(50):
        tick = rng.randint(0, end_tick)
        chosen = rng.sample(ports, rng.randint(1, len(ports)))
        assert dbg.view(tick, chosen) == ref.view(tick, chosen)

    # locate cross-check with freshly generated breakpoint sets
    ops = [Op.LT, Op.EQ, Op.GE]
    for _ in range(60):
        bps = []
        for _ in range(rng.randint(1, 8)):
            conds = []
            for _ in range(rng.randint(1, 4)):
                conds.append(Condition(
                    rng.choice(ports), rng.choice(ops), rng.randint(0, 3)))
            bps.append(Breakpoint(tuple(conds)))
        start = rng.randint(0, end_tick)
        for direction in (Direction.FORWARD, Direction.BACKWARD):
            got = dbg.locate(start, bps, direction)
            want = ref.locate(start, bps, direction)
            if want is None:
                assert got is None
            else:
                want_tick, want_indices = want
                assert got is not None
                assert got.tick == want_tick
                assert sorted(got.breakpoint_indices) == sorted(want_indices)


# ---------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------

def test_validation(tmp_path):
    dbg = make_debugger(tmp_path, [(0, "a", 1)], end_tick=10)
    with pytest.raises(ValueError):
        dbg.view(11, ["a"])
    with pytest.raises(ValueError):
        dbg.locate(-1, [bp(("a", "==", 1))])
    with pytest.raises(ValueError):
        dbg.locate(0, [], Direction.FORWARD)
    with pytest.raises(ValueError):
        dbg.locate(0, [bp(("a", "==", 1))] * 65, Direction.FORWARD)
    with pytest.raises(ValueError):
        dbg.locate(0, [Breakpoint(())], Direction.FORWARD)
    with pytest.raises(ValueError):
        dbg.locate(0, [Breakpoint(tuple(
            Condition("a", Op.EQ, 1) for _ in range(5)))])
    dbg.close()


def test_import_requires_non_decreasing_ticks(tmp_path):
    dbg = TraceDebugger(str(tmp_path / "trace.db"))
    with pytest.raises(ValueError):
        dbg.import_trace([(5, "a", 1), (4, "b", 2)], end_tick=10)
    dbg.close()


def test_end_tick_bounds(tmp_path):
    dbg = TraceDebugger(str(tmp_path / "trace.db"))
    with pytest.raises(ValueError):
        dbg.import_trace([], end_tick=10001)
    dbg.close()
