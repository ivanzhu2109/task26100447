"""Offline simulation-trace debugging library (Python + SQLite).

Capabilities: trace import, state viewing at any tick, and bidirectional
breakpoint location.
"""

from .debugger import TraceDebugger
from .types import (
    Breakpoint,
    Condition,
    Direction,
    LocateResult,
    Op,
    PortId,
)

__all__ = [
    "Breakpoint",
    "Condition",
    "Direction",
    "LocateResult",
    "Op",
    "PortId",
    "TraceDebugger",
]

__version__ = "0.1.0"
