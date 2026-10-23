"""Rebuild read models: dirty keys every 30 s (worker), whole views nightly or on demand.

Each built doc is upserted into read_model (view, key, as_of = today in US Eastern), and when it
differs from the previous snapshot the builder's StreamEvent is emitted in the same transaction,
so the NOTIFY reaches the API only after the new doc is visible.

`rebuild_all(view, as_of=<past day>)` backfills a past snapshot: builders then read facts as
known that day (`context.known_facts` -> fact_asof), no live event is sent (screens show the
latest snapshot) and dirty marks, which are about today's snapshot, are left in place. Snapshots
dated after today are refused.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core import db, events
from marden.core.db import Conn
from marden.core.jsonutil import canonical_json, jsonb, sha256_hex, to_jsonable
from marden.readmodels.context import building, business_today, snapshot_date
from marden.readmodels.plain_text import strip_doc
from marden.readmodels.registry import Builder, Doc, get_builder, registered_views

log = logging.getLogger(__name__)
DIRTY_BATCH = 500


@dataclass
class RebuildStats:
    built: int = 0
    changed: int = 0
    events: int = 0
    removed: int = 0
    skipped_views: set[str] = field(default_factory=set)
    errors: list[tuple[str, str, str]] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"built={self.built}", f"changed={self.changed}", f"events={self.events}"]
        if self.removed:
            parts.append(f"no_doc={self.removed}")
        if self.skipped_views:
            parts.append(f"waiting_for_builder={sorted(self.skipped_views)}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


def upsert_doc(conn: Conn, view: str, key: str, doc: Doc, *, as_of: date | None = None) -> bool:
    """Write the snapshot of a doc for `as_of` (default today; never after today). Returns True if it
    differs from the snapshot in force on that day. `inputs_hash` holds the sha256 of the canonical JSON.
    Model versions are taken out of the doc's text (`plain_text.strip_doc`; Track record keeps them)."""
    day = snapshot_date(as_of)
    normalized = strip_doc(to_jsonable(doc), view)
    digest = sha256_hex(canonical_json(normalized))
    prev = conn.execute(
        """SELECT inputs_hash FROM read_model WHERE view = %s AND key = %s AND as_of <= %s
           ORDER BY as_of DESC LIMIT 1""",
        (view, key, day),
    ).fetchone()
    conn.execute(
        """INSERT INTO read_model (view, key, as_of, doc, built_at, inputs_hash)
           VALUES (%s, %s, %s, %s, now(), %s)
           ON CONFLICT (view, key, as_of) DO UPDATE
             SET doc = EXCLUDED.doc, built_at = EXCLUDED.built_at, inputs_hash = EXCLUDED.inputs_hash""",
        (view, key, day, jsonb(normalized), digest),
    )
    return prev is None or prev["inputs_hash"] != digest


def _previous_doc(conn: Conn, view: str, key: str, day: date) -> Doc | None:
    """The snapshot in force on `day` before this write (today's earlier build, or an older day's)."""
    row = conn.execute(
        "SELECT doc FROM read_model WHERE view = %s AND key = %s AND as_of <= %s ORDER BY as_of DESC LIMIT 1",
        (view, key, day),
    ).fetchone()
    return row["doc"] if row else None


class _Cycle:
    """One rebuild pass: writes docs and emits each distinct event once."""

    def __init__(self, as_of: date | None):
        self.as_of = snapshot_date(as_of)
        self.live = self.as_of == business_today()  # a backfilled past snapshot sends no live event
        self.stats = RebuildStats()
        self._emitted: set[str] = set()

    def write(self, conn: Conn, b: Builder, key: str, doc: Doc | None) -> None:
        if doc is None:
            self.stats.removed += 1
            return
        self.stats.built += 1
        previous = _previous_doc(conn, b.view, key, self.as_of) if self.live and b.compares_previous else None
        if not upsert_doc(conn, b.view, key, doc, as_of=self.as_of):
            return
        self.stats.changed += 1
        if not self.live:
            return
        for payload in b.events_for(key, strip_doc(to_jsonable(doc), b.view), previous):
            fingerprint = canonical_json(payload)
            if fingerprint in self._emitted:
                continue
            events.emit(payload, conn=conn)
            self._emitted.add(fingerprint)
            self.stats.events += 1

    def fail(self, view: str, key: str, exc: Exception) -> None:
        log.exception("read model build failed: %s/%s", view, key)
        self.stats.errors.append((view, key, f"{type(exc).__name__}: {exc}"))


def _clear(conn: Conn, view: str, locked: list[dict[str, Any]]) -> None:
    conn.execute(
        """DELETE FROM read_model_dirty d USING unnest(%s::text[], %s::timestamptz[]) AS t(k, m)
           WHERE d.view = %s AND d.key = t.k AND d.marked_at = t.m""",
        ([r["key"] for r in locked], [r["marked_at"] for r in locked], view),
    )


def _rebuild_keys(cycle: _Cycle, b: Builder, keys: list[str]) -> None:
    assert b.build_one is not None
    for key in keys:
        try:
            with building(cycle.as_of), db.transaction() as conn:
                locked = conn.execute(
                    """SELECT key, marked_at FROM read_model_dirty WHERE view = %s AND key = %s
                       FOR UPDATE SKIP LOCKED""",
                    (b.view, key),
                ).fetchall()
                if not locked:
                    continue  # rebuilt by another worker, or being marked right now
                cycle.write(conn, b, key, b.build_one(conn, key))
                _clear(conn, b.view, locked)
        except Exception as exc:
            cycle.fail(b.view, key, exc)


def _rebuild_view(cycle: _Cycle, b: Builder) -> None:
    assert b.build_many is not None
    try:
        with building(cycle.as_of), db.transaction() as conn:
            locked = conn.execute(
                "SELECT key, marked_at FROM read_model_dirty WHERE view = %s FOR UPDATE SKIP LOCKED",
                (b.view,),
            ).fetchall()
            if not locked:
                return
            for key, doc in b.build_many(conn):
                cycle.write(conn, b, key, doc)
            _clear(conn, b.view, locked)
    except Exception as exc:
        cycle.fail(b.view, "*", exc)


def rebuild_dirty(*, limit: int = DIRTY_BATCH, as_of: date | None = None) -> RebuildStats:
    """Build today's snapshot of every dirty (view, key) that has a builder; others wait for theirs.
    Dirty marks are about today's snapshot: backfill a past day with `rebuild_all(view, as_of=day)`."""
    if as_of is not None and as_of != business_today():
        raise ValueError("rebuild_dirty builds today's snapshot; use rebuild_all(view, as_of=day) to backfill")
    with db.transaction() as conn:
        dirty = conn.execute(
            "SELECT view, key FROM read_model_dirty ORDER BY marked_at LIMIT %s", (limit,)
        ).fetchall()
    by_view: dict[str, list[str]] = defaultdict(list)
    for row in dirty:
        by_view[row["view"]].append(row["key"])

    cycle = _Cycle(as_of)
    for view, keys in by_view.items():
        b = get_builder(view)
        if b is None:
            cycle.stats.skipped_views.add(view)
        elif b.build_one is not None:
            _rebuild_keys(cycle, b, keys)
        else:
            _rebuild_view(cycle, b)
    return cycle.stats


def rebuild_all(view: str, *, as_of: date | None = None) -> RebuildStats:
    """Rebuild every key of one view (nightly, after a builder change, or `marden rebuild <view>`).
    With a past `as_of` it backfills that day's snapshot from what was known that day."""
    b = get_builder(view)
    if b is None:
        raise KeyError(f"no builder registered for view {view!r}; known: {registered_views()}")
    cycle = _Cycle(as_of)
    with db.transaction() as conn:
        started = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]  # type: ignore[index]

    def clear(conn: Conn, key: str | None) -> None:
        if not cycle.live:
            return  # dirty marks belong to today's snapshot
        conn.execute(
            """DELETE FROM read_model_dirty WHERE view = %s AND marked_at <= %s
               AND (%s::text IS NULL OR key = %s)""",
            (view, started, key, key),
        )

    if b.build_many is not None:
        try:
            with building(cycle.as_of), db.transaction() as conn:
                for key, doc in b.build_many(conn):
                    cycle.write(conn, b, key, doc)
                clear(conn, None)
        except Exception as exc:
            cycle.fail(view, "*", exc)
        return cycle.stats

    assert b.build_one is not None
    with building(cycle.as_of), db.transaction() as conn:
        keys = b.all_keys(conn)
    for key in keys:
        try:
            with building(cycle.as_of), db.transaction() as conn:
                cycle.write(conn, b, key, b.build_one(conn, key))
                clear(conn, key)
        except Exception as exc:
            cycle.fail(view, key, exc)
    return cycle.stats


def rebuild_every_view(*, as_of: date | None = None) -> dict[str, RebuildStats]:
    return {view: rebuild_all(view, as_of=as_of) for view in registered_views()}
