"""Section runner for the `facilities` loader: short transactions, retried on lock conflicts.

Every published fact and event marks shared ``read_model_dirty`` rows (``home|all``, ``sites|all``
...), so one long load transaction would hold those row locks for minutes and block, or deadlock
with, every other writer. The loader therefore commits section by section (each section is
idempotent on its own) and retries a section that hits a deadlock or serialization failure. The
sinks' counters are restored before a retry so the run statistics stay exact.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import psycopg

from marden.core.db import Conn
from marden.seed.facilities_sink import EventSink, FactSink

T = TypeVar("T")
RETRIES = 4
RETRYABLE = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure,
             psycopg.errors.LockNotAvailable)
_STATE = {FactSink: ("counts", "created", "seen"),
          EventSink: ("counts", "skipped", "created", "reviews", "notes")}


@dataclass
class Sections:
    conn: Conn
    sinks: tuple[FactSink | EventSink, ...]
    timings: dict[str, float] = field(default_factory=dict)
    retries: dict[str, int] = field(default_factory=dict)

    def _snapshot(self) -> list[dict[str, Any]]:
        return [{a: copy.copy(getattr(s, a)) for a in _STATE[type(s)]} for s in self.sinks]

    def _restore(self, snap: list[dict[str, Any]]) -> None:
        for sink, state in zip(self.sinks, snap, strict=True):
            for attr, value in state.items():
                setattr(sink, attr, value)

    def run(self, name: str, fn: Callable[[], T]) -> T:
        """Run `fn` in its own transaction (a savepoint when the caller holds one)."""
        start = time.monotonic()
        for attempt in range(1, RETRIES + 1):
            snap = self._snapshot()
            try:
                with self.conn.transaction():
                    result = fn()
                self.timings[name] = round(self.timings.get(name, 0.0) + time.monotonic() - start, 2)
                return result
            except RETRYABLE:
                self._restore(snap)
                if attempt == RETRIES:
                    raise
                self.retries[name] = attempt
                time.sleep(1.5 * attempt)
        raise AssertionError("unreachable")
