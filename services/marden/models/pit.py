"""Point-in-time model inputs: what was public on a run's as-of day (spec 22; pricing-v0.1 "Backtesting").

A run for today reads every published row Marden holds (today's knowledge). A run for a past day is a
backtest and reads only what was public by that day:

- facts: status published or superseded (a superseded value was still what the record said then),
  `valid_from` + the source's publication lag <= as_of, and not ended (`valid_to`) by then. The fact's
  `valid_from` is its record date (filing, statement, report month); `learned_at` is when Marden ingested
  it, which for the archive and the collectors' first runs (2026-10-21) is after every backtest day, so
  `mode="public"` (the default) ignores it and `mode="known"` applies it (exactly `fact_asof`).
- publication lags (`public_date`): an N-PORT mark is public 60 days after its period end; an XBRL
  value 40 days after the period end (pricing-v0.1 "Data"); an EIA-860M monthly inventory on the 24th of
  the following month (the release convention noted in collectors/eia_860m.py). Others: the record date.
- events: published; dated on or before as_of (public that day), or dated later (a scheduled hearing or
  deadline) and learned by Marden by as_of.
- licensing: when MARDEN_SHOW_UNLICENSED_PRICES is false, rows from unlicensed sources are dropped from
  inputs too (docs/methods/licensing.md).

Every read returns rows newest valid_from first, so one-row-per-item fields keep all their items.

Input audit: inside `with pit.audit() as used:` every row these reads return (and every input the models
note themselves: price observations, stored model outputs, static instrument terms) is appended to `used`
as an `Input` with the day it became public, so a backtest can be checked for look-ahead
(`late_inputs(used, as_of)` must be empty; `python -m marden model price|cushion --audit-inputs`).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from marden.core.db import Conn, Row
from marden.core.licensing import show_unlicensed_prices, unlicensed_source_names
from marden.readmodels.context import business_today

NPORT_LAG_DAYS = 60
XBRL_LAG_DAYS = 40
EIA_860M_RELEASE_DAY = 24


@dataclass(frozen=True)
class Input:
    """One input a model read: what it is, the record's own date and the first day it was public."""

    kind: str  # 'fact' | 'event' | 'price_obs' | 'model_output' | 'terms' | 'series'
    subject: str  # '<subject_type>:<subject_id>' (or the instrument id for prices)
    field: str
    record_date: date
    public: date
    source: str
    basis: str = ""  # how `public` was set when it is not the record date


_AUDIT: ContextVar[list[Input] | None] = ContextVar("marden_pit_audit", default=None)


@contextmanager
def audit() -> Iterator[list[Input]]:
    """Collect every input read in this block (see the module docstring)."""
    used: list[Input] = []
    token = _AUDIT.set(used)
    try:
        yield used
    finally:
        _AUDIT.reset(token)


@contextmanager
def unaudited() -> Iterator[None]:
    """Reads in this block are not recorded; the caller notes what it uses with its own public date."""
    token = _AUDIT.set(None)
    try:
        yield
    finally:
        _AUDIT.reset(token)


def auditing() -> bool:
    return _AUDIT.get() is not None


def note(kind: str, subject: str, field: str, record_date: date | datetime, public: date | datetime,
         source: str | None, basis: str = "") -> None:
    used = _AUDIT.get()
    if used is None:
        return
    rd = record_date.date() if isinstance(record_date, datetime) else record_date
    pub = public.date() if isinstance(public, datetime) else public
    used.append(Input(kind, subject, field, rd, pub, source or "", basis))


def late_inputs(used: Sequence[Input], as_of: date | str) -> list[Input]:
    """The inputs that were not yet public on `as_of` (a backtest must have none)."""
    day = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    return [i for i in used if i.public > day]


def audit_report(used: Sequence[Input], as_of: date | str) -> tuple[list[str], int]:
    """Lines summarising an audited run's inputs, and the number not yet public on `as_of`."""
    as_of = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    late = late_inputs(used, as_of)
    lines = [f"inputs read: {len(used)} (as of {as_of.isoformat()})"]
    for kind in sorted({i.kind for i in used}):
        items = [i for i in used if i.kind == kind]
        newest = max(i.public for i in items)
        lines.append(f"  {kind:13} {len(items):7}  latest public {newest.isoformat()}")
    lines.append(f"not yet public on {as_of.isoformat()}: {len(late)}")
    lines += [f"  LATE {i.kind} {i.subject} {i.field} public {i.public} ({i.source})" for i in late[:20]]
    return lines, len(late)


def is_backtest(as_of: date) -> bool:
    return as_of < business_today()


def public_date(row: Row) -> date:
    """The day a fact became public (its record date plus the source's publication lag)."""
    day: date = row["valid_from"]
    source = row.get("source_name") or ""
    if source.startswith("N-PORT"):
        return day + timedelta(days=NPORT_LAG_DAYS)
    if source == "EIA-860M":
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        return date(year, month, EIA_860M_RELEASE_DAY)
    if row.get("subject_type") == "entity" and (row.get("producer") or "").startswith("collector:sec_xbrl"):
        return day + timedelta(days=XBRL_LAG_DAYS)
    return day


def _licence_clause(params: dict[str, Any]) -> str:
    if show_unlicensed_prices():
        return ""
    params["hidden_sources"] = unlicensed_source_names()
    return " AND source_name <> ALL(%(hidden_sources)s)"


def fact_rows(
    conn: Conn,
    as_of: date,
    subject_type: str,
    subject_ids: Sequence[str] | None = None,
    fields: Sequence[str] | None = None,
    *,
    mode: str = "public",
) -> list[Row]:
    """Every fact row usable on `as_of` for these subjects and fields (None = all), newest first."""
    if mode not in ("public", "known"):
        raise ValueError(f"mode must be 'public' or 'known', got {mode!r}")
    params: dict[str, Any] = {"st": subject_type, "day": as_of}
    sql = "SELECT * FROM fact WHERE subject_type = %(st)s"
    if subject_ids is not None:
        sql += " AND subject_id = ANY(%(ids)s)"
        params["ids"] = list(subject_ids)
    if fields is not None:
        sql += " AND field = ANY(%(fields)s)"
        params["fields"] = list(fields)
    backtest = is_backtest(as_of)
    if backtest:
        sql += " AND status IN ('published', 'superseded') AND valid_from <= %(day)s"
        sql += " AND (valid_to IS NULL OR valid_to > %(day)s)"
        if mode == "known":
            sql += " AND learned_at < (%(day)s::date + 1)::timestamptz"
    else:
        sql += " AND status = 'published' AND (valid_to IS NULL OR valid_to > %(day)s)"
    sql += _licence_clause(params)
    sql += " ORDER BY subject_id, field, valid_from DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    if backtest:
        rows = [r for r in rows if public_date(r) <= as_of]
    if auditing():
        for r in rows:
            note("fact", f"{r['subject_type']}:{r['subject_id']}", r["field"], r["valid_from"], public_date(r),
                 r.get("source_name"))
    return rows


_TAG_RANK = {"F": 0, "R": 1, "M": 2}


def current_of(rows: Sequence[Row]) -> Row | None:
    """The value a reader would take from several rows of one subject and field: `fact_asof` precedence
    (non-model first, newest valid_from, filed over reported, newest learned)."""
    if not rows:
        return None
    return min(
        rows,
        key=lambda r: (
            r["tag"] == "M",
            -r["valid_from"].toordinal(),
            _TAG_RANK.get(r["tag"], 3),
            -r["learned_at"].timestamp(),
            -r["id"],
        ),
    )


def event_rows(
    conn: Conn,
    as_of: date,
    subject_type: str,
    subject_ids: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
) -> list[Row]:
    """Published events usable on `as_of`, newest event_date first."""
    params: dict[str, Any] = {"st": subject_type, "day": as_of}
    sql = "SELECT * FROM event WHERE subject_type = %(st)s AND status = 'published'"
    if subject_ids is not None:
        sql += " AND subject_id = ANY(%(ids)s)"
        params["ids"] = list(subject_ids)
    if kinds is not None:
        sql += " AND kind = ANY(%(kinds)s)"
        params["kinds"] = list(kinds)
    if is_backtest(as_of):
        sql += " AND (event_date <= %(day)s OR learned_at < (%(day)s::date + 1)::timestamptz)"
    sql += _licence_clause(params)
    sql += " ORDER BY subject_id, event_date DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    if auditing():
        for r in rows:
            scheduled = r["event_date"] > as_of  # a dated-ahead hearing or deadline: public once learned
            note("event", f"{r['subject_type']}:{r['subject_id']}", r["kind"], r["event_date"],
                 r["learned_at"] if scheduled else r["event_date"], r.get("source_name"),
                 "scheduled: public when learned" if scheduled else "")
    return rows
