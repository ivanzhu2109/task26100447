"""正确性测试：业务规则逐项覆盖 + 与独立参照模型交叉核验。"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trace_debugger import Breakpoint, Condition, LocateResult, TraceDebugger
from reference_model import ReferenceModel


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "trace.db")


def make_debugger(db_path, changes, end_tick):
    td = TraceDebugger(db_path)
    td.import_trace(changes, end_tick)
    return td


# --- 同 tick 多端口同时变更：仅中间状态满足条件，不得触发 ---

def test_same_tick_intermediate_state_not_triggered(db_path):
    # p1: 0->1, p2: 1->0 同 tick 发生；断点 p1==1 AND p2==1 只在
    # “先改一个再改另一个”的中间状态成立，可见状态从不成立。
    changes = [(0, "p1", 0), (0, "p2", 1), (10, "p1", 1), (10, "p2", 0)]
    td = make_debugger(db_path, changes, 100)
    bp = [Breakpoint((Condition("p1", "==", 1), Condition("p2", "==", 1)))]
    assert td.locate(0, bp, "forward") is None
    assert td.locate(100, bp, "backward") is None
    # 同 tick 可见状态确认
    assert td.view(9, ["p1", "p2"]) == {"p1": 0, "p2": 1}
    assert td.view(10, ["p1", "p2"]) == {"p1": 1, "p2": 0}
    td.close()


def test_same_tick_simultaneous_satisfy_does_trigger(db_path):
    # 同 tick 两个端口一起变为满足条件：可见状态成立，必须触发。
    changes = [(0, "p1", 0), (0, "p2", 0), (7, "p1", 1), (7, "p2", 1)]
    td = make_debugger(db_path, changes, 100)
    bp = [Breakpoint((Condition("p1", "==", 1), Condition("p2", "==", 1)))]
    assert td.locate(0, bp, "forward") == LocateResult(7, (0,))
    assert td.locate(100, bp, "backward") == LocateResult(7, (0,))
    td.close()


# --- 边沿语义：持续成立不重复、失效再成立可再触发、tick0 不触发 ---

def test_edge_semantics(db_path):
    # p: 0(t0) -> 10(t5) -> 3(t8) -> 7(t20)，之后 80 个 tick 无变化
    changes = [(0, "p", 0), (5, "p", 10), (8, "p", 3), (20, "p", 7)]
    td = make_debugger(db_path, changes, 100)
    bp = [Breakpoint((Condition("p", ">=", 5),))]
    # 持续成立不重复触发
    assert td.locate(5, bp, "forward") == LocateResult(20, (0,))
    # 失效后再成立可以再次触发
    assert td.locate(0, bp, "forward") == LocateResult(5, (0,))
    # 长时间无变化区域不产生触发
    assert td.locate(20, bp, "forward") is None
    assert td.locate(50, bp, "forward") is None
    # 反向同样按时间递增方向的上升沿定义
    assert td.locate(100, bp, "backward") == LocateResult(20, (0,))
    assert td.locate(20, bp, "backward") == LocateResult(5, (0,))
    assert td.locate(5, bp, "backward") is None
    td.close()


def test_tick0_establishes_state_without_trigger(db_path):
    # tick0 初值即满足条件：不触发
    changes = [(0, "p", 5), (3, "p", 0), (9, "p", 5)]
    td = make_debugger(db_path, changes, 100)
    bp = [Breakpoint((Condition("p", "==", 5),))]
    assert td.locate(0, bp, "forward") == LocateResult(9, (0,))
    assert td.locate(100, bp, "backward") == LocateResult(9, (0,))
    assert td.locate(9, bp, "backward") is None  # tick0 不算触发
    td.close()


def test_reverse_finds_real_edge_not_inverted(db_path):
    # 条件在 t4 失效(false 边沿)、t6 重新成立(true 边沿)。
    # 反向定位必须返回 t6（真正的上升沿），不能把 t4 当作反向触发。
    changes = [(0, "p", 1), (4, "p", 0), (6, "p", 1)]
    td = make_debugger(db_path, changes, 100)
    bp = [Breakpoint((Condition("p", "==", 1),))]
    assert td.locate(100, bp, "backward") == LocateResult(6, (0,))
    assert td.locate(6, bp, "backward") is None
    td.close()


# --- 多断点同 tick 触发 ---

def test_multiple_breakpoints_same_tick(db_path):
    changes = [
        (0, "a", 0), (0, "b", 0), (0, "c", 0),
        (12, "a", 5), (12, "b", 9),
    ]
    td = make_debugger(db_path, changes, 100)
    bps = [
        Breakpoint((Condition("a", ">=", 5),)),                    # t12 触发
        Breakpoint((Condition("b", "<", 5),)),                     # t0 已成立，不触发
        Breakpoint((Condition("a", "==", 5), Condition("b", ">=", 9),)),  # t12 触发
        Breakpoint((Condition("c", "==", 1),)),                    # 从不触发
    ]
    assert td.locate(0, bps, "forward") == LocateResult(12, (0, 2))
    assert td.locate(100, bps, "backward") == LocateResult(12, (0, 2))
    td.close()


# --- 查看：多端口同一 tick、无记录 tick、不影响后续定位 ---

def test_view_consistency_and_no_side_effect(db_path):
    changes = [(0, "a", 1), (0, "b", 2), (30, "a", 3), (60, "b", 4)]
    td = make_debugger(db_path, changes, 100)
    # 无变更记录的 tick 也可查看
    assert td.view(55, ["a", "b"]) == {"a": 3, "b": 2}
    # 返回值属于同一个 tick
    assert td.view(60, ["a", "b"]) == {"a": 3, "b": 4}
    bp = [Breakpoint((Condition("a", "==", 3),))]
    before = td.locate(0, bp, "forward")
    td.view(60, ["a", "b"])
    td.view(0, ["a"])
    after = td.locate(0, bp, "forward")
    assert before == after == LocateResult(30, (0,))
    td.close()


# --- 全程双向遍历：正反触发位置与同 tick 断点集合完全对应 ---

def test_full_bidirectional_traversal(db_path):
    rng = random.Random(20261003)
    ports = [f"p{i}" for i in range(8)]
    changes = [(0, p, rng.randint(0, 3)) for p in ports]
    for t in range(1, 201):
        for _ in range(rng.randint(0, 3)):
            changes.append((t, rng.choice(ports), rng.randint(0, 3)))
    end_tick = 200
    td = make_debugger(db_path, changes, end_tick)
    bps = [
        Breakpoint((Condition(rng.choice(ports), rng.choice(["<", "==", ">="]), rng.randint(0, 3)),))
        for _ in range(6)
    ]
    # 正向全程收集
    fwd = []
    pos = 0
    while True:
        r = td.locate(pos, bps, "forward")
        if r is None:
            break
        fwd.append((r.tick, r.breakpoints))
        pos = r.tick
    # 反向全程收集
    bwd = []
    pos = end_tick
    while True:
        r = td.locate(pos, bps, "backward")
        if r is None:
            break
        bwd.append((r.tick, r.breakpoints))
        pos = r.tick
    assert fwd == list(reversed(bwd))
    # 与参照模型完全一致
    ref = ReferenceModel(changes, end_tick)
    expected = sorted((t, tuple(sorted(v))) for t, v in ref.triggers(bps).items())
    assert fwd == expected
    td.close()


# --- 随机交叉核验：查看与双向定位均与独立参照模型一致，相邻调用更换条件 ---

def test_randomized_cross_check(db_path):
    rng = random.Random(42)
    n_ports, end_tick = 12, 120
    ports = list(range(n_ports))
    changes = [(0, p, rng.randint(0, 5)) for p in ports]
    for t in range(1, end_tick + 1):
        for _ in range(rng.randint(0, 4)):
            changes.append((t, rng.choice(ports), rng.randint(0, 5)))
    td = make_debugger(db_path, changes, end_tick)
    ref = ReferenceModel(changes, end_tick)

    # 查看核验
    for _ in range(200):
        t = rng.randint(0, end_tick)
        ps = rng.sample(ports, rng.randint(1, n_ports))
        assert td.view(t, ps) == ref.view(t, ps)

    # 定位核验：每次随机生成全新断点条件（覆盖相邻调用更换条件场景）
    for _ in range(300):
        n_bp = rng.randint(1, 8)
        bps = []
        for _ in range(n_bp):
            conds = tuple(
                Condition(rng.choice(ports), rng.choice(["<", "==", ">="]), rng.randint(0, 5))
                for _ in range(rng.randint(1, 4))
            )
            bps.append(Breakpoint(conds))
        start = rng.randint(0, end_tick)
        direction = rng.choice(["forward", "backward"])
        got = td.locate(start, bps, direction)
        exp = ref.locate(start, bps, direction)
        if exp is None:
            assert got is None
        else:
            assert got is not None
            assert (got.tick, got.breakpoints) == exp
    td.close()


# --- 参数校验 ---

def test_validation(db_path):
    td = make_debugger(db_path, [(0, "a", 1)], 10)
    with pytest.raises(ValueError):
        td.locate(0, [], "forward")  # 0 个断点
    with pytest.raises(ValueError):
        td.locate(0, [Breakpoint(tuple())], "forward")  # 空条件
    with pytest.raises(ValueError):
        td.locate(0, [Breakpoint(tuple(Condition("a", "==", i) for i in range(5)))], "forward")
    with pytest.raises(ValueError):
        td.locate(0, [Breakpoint((Condition("a", ">", 1),))], "forward")  # 不支持的运算
    with pytest.raises(ValueError):
        td.locate(11, [Breakpoint((Condition("a", "==", 1),))], "forward")
    with pytest.raises(ValueError):
        td.view(11, ["a"])
    td.close()
