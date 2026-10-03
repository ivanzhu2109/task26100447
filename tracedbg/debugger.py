"""Public facade combining trace storage and the trigger engine."""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Sequence, Tuple, Union

from .engine import TriggerEngine
from .store import ChangeRow, TraceStore
from .types import Breakpoint, Condition, Direction, LocateResult, Op, PortId

Record = Tuple[int, PortId, int]  # (tick, port, value)

MAX_BREAKPOINTS = 64
MAX_CONDITIONS_PER_BREAKPOINT = 4


class TraceDebugger:
    """Offline debugger over one finished simulation trace."""

    def __init__(self, db_path: str) -> None:
        self._store = TraceStore(db_path)
        self._engine = TriggerEngine(self._store)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "TraceDebugger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def end_tick(self) -> int:
        return self._store.end_tick

    def import_trace(
        self,
        records: Iterable[Record],
        end_tick: int,
        batch_size: int = 10000,
    ) -> int:
        """Import the whole record stream; returns the record count."""
        self._store.begin_import(end_tick)
        count = 0
        batch: list = []
        for tick, port, value in records:
            batch.append((tick, port, value))
            if len(batch) >= batch_size:
                self._store.add_records(batch)
                count += len(batch)
                batch.clear()
        if batch:
            self._store.add_records(batch)
            count += len(batch)
        self._store.finish_import()
        return count

    def view(self, tick: int, ports: Iterable[PortId]) -> Dict[PortId, Optional[int]]:
        """Values of ``ports`` as visible at ``tick`` (same tick for all)."""
        self._check_tick(tick)
        return self._store.values_at(ports, tick)

    def locate(
        self,
        start_tick: int,
        breakpoints: Sequence[Union[Breakpoint, Sequence]],
        direction: Union[Direction, str] = Direction.FORWARD,
    ) -> Optional[LocateResult]:
        """Nearest trigger tick from ``start_tick`` (exclusive) or None."""
        self._check_tick(start_tick)
        bps = [self._coerce_breakpoint(bp) for bp in breakpoints]
        if not 1 <= len(bps) <= MAX_BREAKPOINTS:
            raise ValueError(
                f"breakpoints count must be in [1, {MAX_BREAKPOINTS}]"
            )
        hit = self._engine.locate(start_tick, bps, Direction(direction))
        if not hit:
            return None
        tick, indices = next(iter(hit.items()))
        return LocateResult(tick=tick, breakpoint_indices=tuple(indices))

    def _check_tick(self, tick: int) -> None:
        if not isinstance(tick, int) or tick < 0 or tick > self._store.end_tick:
            raise ValueError(f"tick must be in [0, {self._store.end_tick}]")

    @staticmethod
    def _coerce_breakpoint(bp) -> Breakpoint:
        if isinstance(bp, Breakpoint):
            conditions = bp.conditions
        else:
            conditions = tuple(
                c if isinstance(c, Condition) else Condition(c[0], Op(c[1]), int(c[2]))
                for c in bp
            )
        if not 1 <= len(conditions) <= MAX_CONDITIONS_PER_BREAKPOINT:
            raise ValueError(
                "a breakpoint needs 1 to "
                f"{MAX_CONDITIONS_PER_BREAKPOINT} conditions"
            )
        normalised = tuple(
            Condition(c.port, Op(c.op), int(c.value)) for c in conditions
        )
        return Breakpoint(normalised)
