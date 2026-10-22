"""Shared pieces of the rate collectors (`ust_par_curve`, `nyfed_sofr`, `fred`).

- Time series that are not instruments are facts on subject_type 'series': subject_id is the
  series id ('ust_par_10y', 'sofr'), field 'value', one fact per observation date (valid_from).
- Cells a publisher left blank are skipped and counted; nothing is filled in or carried forward.
- Cadences are Eastern time. Procrastinate crons are UTC, so each task is scheduled at both UTC
  hours the ET time can fall on and `due_et` lets only the run at the ET time through.
- A run is one `heartbeat.run`; a failed fetch or parse is recorded on the source and returned.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from dateutil import tz

from marden.core import db, documents, heartbeat
from marden.core.db import Conn
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter
from marden.core.http import FetchError
from marden.core.sources import SourceBlocked, SourceSpec, upsert_spec

log = logging.getLogger(__name__)

ET = tz.gettz("America/New_York")
SERIES = "series"
VALUE = "value"
# A rate or spread in percent outside this range means the file changed shape (e.g. basis points),
# not a market move (CCC OAS peaked near 45% in Dec 2008).
PCT_RANGE = (Decimal(-5), Decimal(60))
BLANKS = frozenset({"", ".", "N/A", "NA", "n/a"})


class ParseError(ValueError):
    """The fetched file is not in the shape the parser knows; nothing from it is written."""


def today_et() -> date:
    return datetime.now(ET).date()


def due_et(timestamp: int | None, hour: int, minute: int) -> bool:
    """True when a periodic run scheduled at `timestamp` (UTC epoch) is at hour:minute Eastern.
    A manual defer (no timestamp) is always due."""
    if timestamp is None:
        return True
    local = datetime.fromtimestamp(timestamp, UTC).astimezone(ET)
    return (local.hour, local.minute) == (hour, minute)


def number(text: str | None) -> Decimal | None:
    """The published decimal, or None for a blank cell. Anything else that is not a number is a ParseError."""
    if text is None:
        return None
    text = text.strip()
    if text in BLANKS:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ParseError(f"not a number: {text!r}") from None
    if not value.is_finite():
        raise ParseError(f"not a finite number: {text!r}")
    return value


def check_pct(series_id: str, day: date, value: Decimal) -> Decimal:
    low, high = PCT_RANGE
    if not low <= value <= high:
        raise ParseError(f"{series_id} {day.isoformat()}: {value} is outside {low}..{high} percent")
    return value


def document_loaded(conn: Conn, document_id: int) -> bool:
    """Whether facts were already written from this document (a finished backfill file)."""
    return conn.execute("SELECT 1 FROM fact WHERE document_id = %s LIMIT 1", (document_id,)).fetchone() is not None


def url_loaded(source_id: str, url: str) -> bool:
    """The latest stored version of `url` exists and facts were written from it."""
    doc = documents.latest_document(source_id, url)
    if doc is None:
        return False
    with db.transaction() as conn:
        return document_loaded(conn, doc.id)


@dataclass
class RunStats:
    """Counts for one run; `as_meta()` goes into source_run.meta and the CLI output."""

    documents_seen: int = 0
    documents_new: int = 0
    facts_seen: int = 0
    facts_new: int = 0
    blank_cells: int = 0
    new_by_series: dict[str, int] = field(default_factory=dict)
    latest: dict[str, list[Any]] = field(default_factory=dict)  # series -> [date, value] newest seen
    extra: dict[str, Any] = field(default_factory=dict)

    def document(self, doc: DocumentRow) -> None:
        self.documents_seen += 1
        self.documents_new += int(doc.was_new)

    def as_meta(self) -> dict[str, Any]:
        return {
            "documents_seen": self.documents_seen, "documents_new": self.documents_new,
            "facts_seen": self.facts_seen, "facts_new": self.facts_new, "blank_cells": self.blank_cells,
            "new_by_series": dict(sorted(self.new_by_series.items())),
            "latest": dict(sorted(self.latest.items())), **self.extra,
        }


class SeriesWriter:
    """Published series facts from one document (structured publisher data publishes directly)."""

    def __init__(self, conn: Conn, stats: RunStats, *, tag: str, source_name: str, producer: str):
        self.facts = FactWriter(conn)
        self.stats = stats
        self.tag = tag
        self.source_name = source_name
        self.producer = producer

    def observation(
        self, series_id: str, day: date, value: Decimal, *, source_url: str, document_id: int | None,
        unit: str = "pct",
    ) -> bool:
        res = self.facts.add(
            SERIES, series_id, VALUE, value, tag=self.tag, source_name=self.source_name, source_url=source_url,
            valid_from=day, producer=self.producer, status="published", unit=unit, document_id=document_id,
        )
        self._count(series_id, res.created)
        newest = self.stats.latest.get(series_id)
        if newest is None or newest[0] < day.isoformat():
            self.stats.latest[series_id] = [day.isoformat(), float(value)]
        return res.created

    def attribute(
        self, series_id: str, field_name: str, value: Any, *, valid_from: date, source_url: str,
        quote: str | None = None, quote_loc: str | None = None, document_id: int | None = None,
        unit: str | None = None,
    ) -> bool:
        """A non-observation fact about a series (licence notice, display permission, a fund statistic)."""
        res = self.facts.add(
            SERIES, series_id, field_name, value, tag=self.tag, source_name=self.source_name,
            source_url=source_url, valid_from=valid_from, producer=self.producer, status="published",
            document_id=document_id, quote=quote, quote_loc=quote_loc, unit=unit,
        )
        self._count(f"{series_id}.{field_name}", res.created)
        return res.created

    def _count(self, key: str, created: bool) -> None:
        self.stats.facts_seen += 1
        if created:
            self.stats.facts_new += 1
            self.stats.new_by_series[key] = self.stats.new_by_series.get(key, 0) + 1


def collect(spec: SourceSpec, body: Callable[[RunStats], None]) -> dict[str, Any]:
    """Upsert the source row, then run `body` inside one heartbeat. A blocked source, a failed fetch
    or an unparseable file is recorded on the source (source_run.ok = false, source.last_error) and
    returned as {'ok': False, 'error': ...}; nothing is invented to cover the gap."""
    upsert_spec(spec)
    stats = RunStats()
    try:
        with heartbeat.run(spec.id) as hb:
            hb.meta = stats.extra  # filled in as the run goes, so a failed run keeps what it did
            try:
                body(stats)
            finally:
                hb.meta = stats.as_meta()
            hb.seen(stats.facts_seen)
            hb.new(stats.facts_new)
    except (SourceBlocked, FetchError, ParseError) as exc:
        log.warning("%s: %s: %s", spec.id, type(exc).__name__, exc)
        out = stats.as_meta()
        out.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        return out
    out = stats.as_meta()
    out["ok"] = True
    return out


def print_stats(stats: dict[str, Any]) -> int:
    print(json.dumps(stats, indent=2, default=str))
    return 0 if stats.get("ok") else 1
