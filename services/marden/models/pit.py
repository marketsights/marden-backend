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
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from marden.core.db import Conn, Row
from marden.core.licensing import show_unlicensed_prices, unlicensed_source_names
from marden.readmodels.context import business_today

NPORT_LAG_DAYS = 60
XBRL_LAG_DAYS = 40
EIA_860M_RELEASE_DAY = 24


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
    return conn.execute(sql, params).fetchall()
