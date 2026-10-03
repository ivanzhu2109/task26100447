"""Public data types for the trace debugging library."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Union

# A port identifier as supplied by the caller. Identifiers are normalised to
# ``str`` for storage; results echo back the original objects as keys.
PortId = Union[str, int]


class Op(str, Enum):
    """Comparison operators supported by breakpoint conditions."""

    LT = "<"
    EQ = "=="
    GE = ">="


class Direction(str, Enum):
    """Search direction for breakpoint location."""

    FORWARD = "forward"
    BACKWARD = "backward"


@dataclass(frozen=True)
class Condition:
    """A single comparison: ``port <op> value``."""

    port: PortId
    op: Op
    value: int


@dataclass(frozen=True)
class Breakpoint:
    """A breakpoint: 1 to 4 conditions, all must hold (logical AND)."""

    conditions: tuple[Condition, ...]


@dataclass(frozen=True)
class LocateResult:
    """Outcome of a locate call: the trigger tick and every breakpoint
    (by index into the caller-supplied list) that triggered at that tick."""

    tick: int
    breakpoint_indices: tuple[int, ...]
