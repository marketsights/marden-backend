"""Shared model for the capacity-auction collectors (pjm_rpm, isone_fca; MISO/NYISO collectors can reuse it).

- One auction result per zone is a set of facts on subject_type 'series', subject_id
  'capacity:<iso>:<zone>:<period>': `capacity:pjm:rto:2027-2028` (PJM BRA for delivery year 2027/28),
  `capacity:pjm:dom:2026-2027-3ia` (an incremental auction), `capacity:isone:sene:2025-2026` (FCA 16).
  <zone> is the operator's zone code, lower case, non-alphanumerics as '-' (`zone_slug`).
- Fields: clearing_price (USD/MW-day; operators that price in another unit add clearing_price_native in
  their unit, with the conversion in quote_loc), cleared_mw (MW), auction_name, delivery_start,
  delivery_end, auction_date (when the operator states it), plus operator extras (zonal_capacity_price,
  zonal_ucap_obligation_mw, lda, load_zones ...).
- Tag F, source_name = the operator, source_url = the results document, document_id = its stored copy,
  valid_from = the date the results were published. Facts are published directly (structured operator
  data) and are idempotent through the fact dedupe key; a revised results file adds new rows.
- Events: a published `capacity_auction` event on each V1 site whose capacity zone (its own
  `capacity_zone` fact, or that of an approved campus) has a result, for the latest auction of that zone.
  The title carries the auction and price, so a rerun writes nothing and a new auction (or a revised
  price) adds one event per linked site; history is never flooded with past auctions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from marden.collectors.rates_common import ParseError, RunStats
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.records import EventWriter

SERIES = "series"
PREFIX = "capacity"
USD_MW_DAY = "USD/MW-day"
USD_KW_MONTH = "USD/kW-month"
MW = "MW"
DAYS_PER_MONTH = Decimal("30.4167")  # 365 / 12, rounded as the conversion is commonly published
PRICE_FIELDS = ("zonal_capacity_price", "clearing_price")  # the price an event quotes, in order of preference
EVENT_KIND = "capacity_auction"
CAPACITY_MARKETS = ("pjm", "isone", "miso", "nyiso")


def zone_slug(code: str) -> str:
    """'ATSI-CLEVELAND' -> 'atsi-cleveland', 'Rest-of-Pool' -> 'rest-of-pool', 'AEP ***' -> 'aep'."""
    slug = re.sub(r"[^a-z0-9]+", "-", code.strip().lower()).strip("-")
    if not slug:
        raise ParseError(f"empty zone code {code!r}")
    return slug


def subject_id(iso: str, zone: str, period: str) -> str:
    return f"{PREFIX}:{iso}:{zone_slug(zone)}:{period}"


def zone_prefix(iso: str, zone: str) -> str:
    """The LIKE prefix of every auction subject of one zone."""
    return f"{PREFIX}:{iso}:{zone_slug(zone)}:"


def delivery_year(start_year: int) -> tuple[date, date]:
    """June 1 to May 31: the PJM delivery year and the ISO-NE capacity commitment period."""
    return date(start_year, 6, 1), date(start_year + 1, 5, 31)


def period_key(start_year: int, auction: str | None = None) -> str:
    """'2027-2028' for a base auction, '2026-2027-3ia' for another auction of that period."""
    base = f"{start_year}-{start_year + 1}"
    return f"{base}-{auction}" if auction else base


def decimal(value: Any, what: str) -> Decimal:
    """A published number as a Decimal (float noise such as 53457.600000000006 rounded off at 6 places)."""
    if isinstance(value, bool) or value is None:
        raise ParseError(f"{what}: expected a number, got {value!r}")
    if isinstance(value, int | float | Decimal):
        dec = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    else:
        text = str(value).strip().replace(",", "").lstrip("$")
        try:
            dec = Decimal(text)
        except ArithmeticError:
            raise ParseError(f"{what}: not a number: {value!r}") from None
    if not dec.is_finite():
        raise ParseError(f"{what}: not a finite number: {value!r}")
    return dec.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP).normalize() + Decimal(0)


def kw_month_to_mw_day(price: Decimal) -> Decimal:
    """USD/kW-month -> USD/MW-day: x 1000 / 30.4167, to the cent."""
    return (price * 1000 / DAYS_PER_MONTH).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def conversion_note(price: Decimal) -> str:
    converted = kw_month_to_mw_day(price)
    return f"converted by Marden: {price} USD/kW-month x 1000 / {DAYS_PER_MONTH} = {converted} USD/MW-day"


@dataclass(frozen=True)
class Value:
    value: Any
    unit: str | None = None
    quote_loc: str | None = None
    quote: str | None = None
    source_url: str | None = None  # when the value comes from another document than the auction's results file
    document_id: int | None = None


@dataclass
class ZoneResult:
    zone: str  # the operator's code: 'RTO', 'DOM', 'SENE'
    values: dict[str, Value] = field(default_factory=dict)


@dataclass
class Auction:
    iso: str  # 'pjm', 'isone'
    period: str  # period_key()
    name: str  # '2027/2028 Base Residual Auction', 'FCA 18'
    delivery_start: date
    delivery_end: date
    published: date  # valid_from of every fact
    published_loc: str  # where the publication date was read
    source_url: str  # the results document
    document_id: int | None
    auction_date: date | None = None
    auction_date_loc: str | None = None
    zones: list[ZoneResult] = field(default_factory=list)

    def zone_codes(self) -> list[str]:
        return [z.zone for z in self.zones]


class CapacityWriter:
    """Writes an auction's facts on a connection the caller holds (inside a transaction)."""

    def __init__(self, conn: Conn, stats: RunStats, *, source_name: str, producer: str):
        self.facts = FactWriter(conn)
        self.stats = stats
        self.source_name = source_name
        self.producer = producer

    def write(self, auction: Auction) -> int:
        """All facts of one auction. Returns the number of new facts."""
        if not auction.zones:
            raise ParseError(f"{auction.iso} {auction.period}: no zone results")
        new = 0
        for zone in auction.zones:
            sid = subject_id(auction.iso, zone.zone, auction.period)
            common = {
                "auction_name": Value(auction.name),
                "delivery_start": Value(auction.delivery_start, "date"),
                "delivery_end": Value(auction.delivery_end, "date"),
            }
            if auction.auction_date is not None:
                common["auction_date"] = Value(auction.auction_date, "date", auction.auction_date_loc)
            for name, val in {**common, **zone.values}.items():
                loc = val.quote_loc or f"publication date: {auction.published_loc}"
                res = self.facts.add(
                    SERIES, sid, name, val.value, tag="F", source_name=self.source_name,
                    source_url=val.source_url or auction.source_url, valid_from=auction.published,
                    producer=self.producer, status="published", unit=val.unit,
                    document_id=val.document_id if val.source_url else auction.document_id, quote=val.quote,
                    quote_loc=loc,
                )
                self.stats.facts_seen += 1
                if res.created:
                    new += 1
                    self.stats.facts_new += 1
                    key = f"{auction.iso}:{auction.period}"
                    self.stats.new_by_series[key] = self.stats.new_by_series.get(key, 0) + 1
        self._latest(auction)
        return new

    def _latest(self, auction: Auction) -> None:
        """stats.latest: zone -> [published, period, price] of the newest auction seen for it."""
        for zone in auction.zones:
            price = next((zone.values[f].value for f in PRICE_FIELDS if f in zone.values), None)
            key = f"{auction.iso}:{zone_slug(zone.zone)}"
            seen = self.stats.latest.get(key)
            row = [auction.published.isoformat(), auction.period, float(price) if price is not None else None]
            if seen is None or (seen[0], seen[1]) < (row[0], row[1]):
                self.stats.latest[key] = row


# ---------------------------------------------------------------- events on linked V1 sites
_LINKED_ZONES = """
WITH zoned AS (
  SELECT m.subject_type, m.subject_id, m.value #>> '{}' AS market, z.value #>> '{}' AS zone
  FROM fact_current m
  JOIN fact_current z ON z.subject_type = m.subject_type AND z.subject_id = m.subject_id AND z.field = 'capacity_zone'
  WHERE m.field = 'capacity_market' AND m.subject_type IN ('site', 'campus') AND m.value #>> '{}' = ANY(%(markets)s)
)
SELECT s.slug, zoned.market, zoned.zone, zoned.subject_type AS via_type, zoned.subject_id AS via_id
FROM zoned JOIN site s ON zoned.subject_type = 'site' AND s.slug = zoned.subject_id
WHERE s.in_v1
UNION
SELECT s.slug, zoned.market, zoned.zone, zoned.subject_type, zoned.subject_id
FROM zoned JOIN site_link l
  ON zoned.subject_type = 'campus' AND l.campus_id = zoned.subject_id AND l.status = 'approved'
JOIN site s ON s.slug = l.site_slug
WHERE s.in_v1
ORDER BY 1, 2, 3
"""
_LATEST = """
SELECT p.id, p.subject_id, p.field, p.value, p.unit, p.valid_from, p.source_name, p.source_url, p.document_id,
       n.value #>> '{}' AS auction_name
FROM fact_current p
LEFT JOIN fact_current n ON n.subject_type = p.subject_type AND n.subject_id = p.subject_id AND n.field = 'auction_name'
WHERE p.subject_type = 'series' AND p.subject_id LIKE %s AND p.field = ANY(%s)
ORDER BY p.valid_from DESC, p.subject_id DESC, array_position(%s::text[], p.field)
LIMIT 1
"""


def latest_result(conn: Conn, iso: str, zone: str) -> dict[str, Any] | None:
    """The newest published price fact (zonal price preferred) of a zone's auctions."""
    pattern = zone_prefix(iso, zone).replace("_", r"\_") + "%"
    return conn.execute(_LATEST, (pattern, list(PRICE_FIELDS), list(PRICE_FIELDS))).fetchone()


def event_title(iso_label: str, zone: str, row: dict[str, Any]) -> str:
    price = Decimal(str(row["value"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    what = "zonal capacity price" if row["field"] == "zonal_capacity_price" else "clearing price"
    name = row["auction_name"] or row["subject_id"].rsplit(":", 1)[-1]
    return f"{iso_label} {name}: {zone} {what} ${price:,}/MW-day"


def emit_auction_events(conn: Conn, isos: dict[str, str], stats: RunStats) -> int:
    """`isos`: market id -> label ('pjm' -> 'PJM'). Returns the number of new events."""
    events = EventWriter(conn)
    linked = conn.execute(_LINKED_ZONES, {"markets": list(isos)}).fetchall()
    new = 0
    seen: list[str] = []
    for row in linked:
        latest = latest_result(conn, row["market"], row["zone"])
        if latest is None:
            continue
        res = events.add(
            "site", row["slug"], latest["valid_from"], EVENT_KIND,
            event_title(isos[row["market"]], row["zone"], latest),
            tag="F", source_name=latest["source_name"], source_url=latest["source_url"],
            document_id=latest["document_id"], fact_id=latest["id"], status="published",
        )
        seen.append(f"{row['slug']}:{row['market']}:{row['zone']}")
        new += int(res.created)
    stats.extra["events_new"] = stats.extra.get("events_new", 0) + new
    stats.extra["linked_v1_sites"] = sorted(set(stats.extra.get("linked_v1_sites", [])) | set(seen))
    return new
