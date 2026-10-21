"""The snapshot date a builder is building, and fact reads that honor it (ARCHITECTURE "Read models").

`rebuild_dirty` / `rebuild_all` write the snapshot of one day (`read_model.as_of`). Today's snapshot
is built from `fact_current`; a backfilled snapshot of a past day must show only what Marden knew
that day, so it is built from `fact_asof(day)`. Builders therefore read facts through
`known_facts`, and use `build_date()` for any other dated read (events, prices, model outputs):

    @builder("dc_row", keys=campus_ids)
    def build(conn, key):
        facts = known_facts(conn, "campus", key, ["facility_mw", "status"])
        ...
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from zoneinfo import ZoneInfo

from marden.core.db import Conn, Row
from marden.core.facts import current_facts, facts_asof

BUSINESS_TZ = ZoneInfo("America/New_York")

_BUILD_DATE: ContextVar[date | None] = ContextVar("marden_readmodel_build_date", default=None)


def business_today() -> date:
    """Snapshots are dated in US Eastern (spec 22: screens rebuilt 5-6 am Eastern)."""
    return datetime.now(BUSINESS_TZ).date()


def snapshot_date(as_of: date | None) -> date:
    """The snapshot day for a write: `as_of`, or today. A snapshot dated after today is refused:
    the API serves the latest snapshot, so a future-dated one would hide every real rebuild."""
    today = business_today()
    day = as_of or today
    if day > today:
        raise ValueError(f"read-model snapshot {day.isoformat()} is after today ({today.isoformat()})")
    return day


def build_date() -> date:
    """The day of the snapshot being built (today outside a rebuild)."""
    return _BUILD_DATE.get() or business_today()


def is_backfill() -> bool:
    """True while building a snapshot of a past day."""
    return build_date() < business_today()


@contextmanager
def building(day: date) -> Iterator[date]:
    token = _BUILD_DATE.set(day)
    try:
        yield day
    finally:
        _BUILD_DATE.reset(token)


def known_facts(
    conn: Conn, subject_type: str, subject_id: str, fields: Sequence[str] | None = None
) -> dict[str, Row]:
    """field -> fact row as Marden knew it on the build date: `fact_current` for today's snapshot,
    `fact_asof(day)` for a past day."""
    if is_backfill():
        return facts_asof(conn, build_date(), subject_type, subject_id, fields)
    return current_facts(conn, subject_type, subject_id, fields)
