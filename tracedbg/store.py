"""SQLite-backed storage for a finished simulation trace.

The trace is append-only: records are imported once, then only read.
Schema:

    meta(key TEXT PRIMARY KEY, value TEXT)      -- e.g. end_tick
    changes(tick INTEGER, port TEXT, value INTEGER)
    index on changes(port, tick)

Port identifiers are normalised to ``str`` before hitting the database.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple

from .types import PortId

ChangeRow = Tuple[int, str, int]  # (tick, port, value)

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE changes (
    tick  INTEGER NOT NULL,
    port  TEXT    NOT NULL,
    value INTEGER NOT NULL
);
"""

_INDEX = "CREATE INDEX idx_changes_port_tick ON changes(port, tick);"


def normalize_port(port: PortId) -> str:
    """Normalise a caller-supplied port identifier for storage."""
    if isinstance(port, str):
        return port
    if isinstance(port, int):
        return str(port)
    raise TypeError(f"unsupported port identifier type: {type(port)!r}")


class TraceStore:
    """Owns the SQLite connection and all SQL for one trace."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA synchronous = OFF;")
        self._conn.execute("PRAGMA journal_mode = MEMORY;")
        self._end_tick: Optional[int] = None
        self._last_import_tick = -1

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TraceStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- import ---------------------------------------------------------

    def begin_import(self, end_tick: int) -> None:
        """Create a fresh schema (dropping any previous trace)."""
        if not isinstance(end_tick, int) or end_tick < 0 or end_tick > 10000:
            raise ValueError("end_tick must be an integer in [0, 10000]")
        cur = self._conn.cursor()
        cur.execute("DROP TABLE IF EXISTS changes;")
        cur.execute("DROP TABLE IF EXISTS meta;")
        cur.executescript(_SCHEMA)
        cur.execute(
            "INSERT INTO meta(key, value) VALUES ('end_tick', ?);",
            (str(end_tick),),
        )
        self._conn.commit()
        self._end_tick = end_tick
        self._last_import_tick = -1

    def add_records(self, records: Iterable[ChangeRow]) -> None:
        """Append a batch of (tick, port, value) records."""
        if self._end_tick is None:
            raise RuntimeError("begin_import() must be called first")
        end_tick = self._end_tick
        last = self._last_import_tick
        batch: List[ChangeRow] = []
        for tick, port, value in records:
            if tick < last:
                raise ValueError(
                    f"records must arrive with non-decreasing tick "
                    f"(got {tick} after {last})"
                )
            if tick < 0 or tick > end_tick:
                raise ValueError(f"tick {tick} outside [0, {end_tick}]")
            last = tick
            batch.append((tick, normalize_port(port), int(value)))
        self._last_import_tick = last
        if batch:
            self._conn.executemany(
                "INSERT INTO changes(tick, port, value) VALUES (?, ?, ?);",
                batch,
            )

    def finish_import(self) -> None:
        """Build indexes and finalise the imported trace."""
        self._conn.commit()
        self._conn.execute(_INDEX)
        self._conn.execute("ANALYZE;")
        self._conn.commit()

    # -- queries --------------------------------------------------------

    @property
    def end_tick(self) -> int:
        if self._end_tick is None:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'end_tick';"
            ).fetchone()
            if row is None:
                raise RuntimeError("no trace has been imported yet")
            self._end_tick = int(row[0])
        return self._end_tick

    def value_at(self, port: PortId, tick: int) -> Optional[int]:
        """Value of one port visible at ``tick`` (None if never recorded)."""
        row = self._conn.execute(
            "SELECT value FROM changes WHERE port = ? AND tick <= ? "
            "ORDER BY tick DESC LIMIT 1;",
            (normalize_port(port), tick),
        ).fetchone()
        return None if row is None else int(row[0])

    def values_at(self, ports: Iterable[PortId], tick: int) -> Dict[PortId, Optional[int]]:
        """Values of several ports, all belonging to the same tick."""
        return {port: self.value_at(port, tick) for port in ports}

    def port_series(self, port: PortId) -> List[Tuple[int, int]]:
        """Full (tick, value) history of one port, ordered by tick."""
        cur = self._conn.execute(
            "SELECT tick, value FROM changes WHERE port = ? ORDER BY tick;",
            (normalize_port(port),),
        )
        return [(int(t), int(v)) for t, v in cur]
