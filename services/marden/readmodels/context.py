"""The snapshot date a builder is building, and fact reads that honor it (ARCHITECTURE "Read models").

`rebuild_dirty` / `rebuild_all` write the snapshot of one day (`read_model.as_of`). Today's snapshot
is built from `fact_current`; a backfilled snapshot of a past day must show only what Marden knew
that day, so it is built from `fact_asof(day)`. Builders therefore read facts through
`known_facts`, and use `build_date()` for any other dated read (events, prices, model outputs).
With MARDEN_SHOW_UNLICENSED_PRICES=false both fact reads leave out rows from unlicensed sources
(`hidden_source_names()`, docs/methods/licensing.md); so must any builder's own price_obs or fact SQL:

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
from marden.core.licensing import hidden_source_names

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
    `fact_asof(day)` for a past day. Rows from sources the licence switch hides are left out first, so
    the field falls back to its next (licensed) value or to n/d."""
    hidden = hidden_source_names()
    if is_backfill():
        return facts_asof(conn, build_date(), subject_type, subject_id, fields, hidden=hidden)
    return current_facts(conn, subject_type, subject_id, fields, hidden=hidden)


def known_fact_rows(conn: Conn, subject_type: str, subject_id: str, fields: Sequence[str]) -> dict[str, list[Row]]:
    """field -> every row of a one-fact-per-item field (structure_term, issue, imagery, risk_evidence ...)
    as Marden knew them on the build date, newest valid_from first. `known_facts` keeps only the
    current row per field; these fields hold one published row per item. Same visibility rules as
    `fact_current` (today) and `fact_asof(day)` (a backfilled day), licence switch included."""
    day = build_date()
    hidden = hidden_source_names()
    if is_backfill():
        sql = """SELECT * FROM fact WHERE subject_type = %s AND subject_id = %s AND field = ANY(%s)
                   AND status IN ('published', 'superseded') AND valid_from <= %s
                   AND learned_at < (%s::date + 1)::timestamptz AND (valid_to IS NULL OR valid_to > %s)"""
        params: tuple = (subject_type, subject_id, list(fields), day, day, day)
    else:
        sql = """SELECT * FROM fact WHERE subject_type = %s AND subject_id = %s AND field = ANY(%s)
                   AND status = 'published' AND (valid_to IS NULL OR valid_to > CURRENT_DATE)"""
        params = (subject_type, subject_id, list(fields))
    if hidden:
        sql += " AND source_name <> ALL(%s)"
        params = (*params, hidden)
    sql += " ORDER BY field, valid_from DESC, id DESC"
    out: dict[str, list[Row]] = {f: [] for f in fields}
    for row in conn.execute(sql, params).fetchall():
        out[row["field"]].append(row)
    return out
