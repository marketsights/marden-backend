"""Headline events for capacity auctions that price several periods or localities at once (NYISO ICAP, MISO PRA).

capacity_common.emit_auction_events announces the latest price fact of a zone, which suits one-period auctions
(PJM BRA, ISO-NE FCA). A NYISO monthly auction prices up to six months and a MISO PRA four seasons, all
published the same day, and NYISO prices localities (NYCA, G-J, NYC, LI) while a site's `capacity_zone` is its
NYISO load zone (A-K). These collectors therefore announce their own headline per auction:

- `Headline`: one zone's line for one auction (NYISO: the prompt period; MISO: the four seasons in one title).
- `announce`: for each V1 site whose `capacity_market` is the market and whose `capacity_zone` (its own, or an
  approved linked campus's) maps to a zone with a headline, one published event of kind 'capacity_auction'
  (tag F, the results document as source). Only auctions new to Marden (facts created by this load) and
  published within the window (EVENT_WINDOW_DAYS for the annual MISO PRA; about one monthly check for NYISO)
  are announced, so backfilled history raises no events and a rerun writes nothing (the event dedupe key
  covers site, date, kind, title and source).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from marden.collectors.capacity_common import EVENT_KIND
from marden.collectors.rates_common import RunStats
from marden.core.db import Conn
from marden.core.records import EventWriter

EVENT_WINDOW_DAYS = 120

_LINKED = """
SELECT s.slug, z.value #>> '{}' AS zone
FROM site s
JOIN fact_current m ON m.subject_type = 'site' AND m.subject_id = s.slug AND m.field = 'capacity_market'
JOIN fact_current z ON z.subject_type = 'site' AND z.subject_id = s.slug AND z.field = 'capacity_zone'
WHERE s.in_v1 AND m.value #>> '{}' = %(market)s
UNION
SELECT s.slug, z.value #>> '{}'
FROM site s
JOIN site_link l ON l.site_slug = s.slug AND l.status = 'approved'
JOIN fact_current m ON m.subject_type = 'campus' AND m.subject_id = l.campus_id AND m.field = 'capacity_market'
JOIN fact_current z ON z.subject_type = 'campus' AND z.subject_id = l.campus_id AND z.field = 'capacity_zone'
WHERE s.in_v1 AND m.value #>> '{}' = %(market)s
ORDER BY 1, 2
"""


@dataclass(frozen=True)
class Headline:
    zone: str  # the series zone slug the line is about: 'nyca', 'lrz9'
    title: str  # 'NYISO Winter 2026-2027 Strip Auction: NYCA $3.50/kW-month ($115.07/MW-day)'
    published: date  # event date: the results' publication date
    source_url: str
    document_id: int | None


def linked_v1_sites(conn: Conn, market: str) -> list[tuple[str, str]]:
    """(site slug, capacity_zone value) of the V1 sites in `market` ('nyiso', 'miso') that have a zone."""
    return [(r["slug"], r["zone"]) for r in conn.execute(_LINKED, {"market": market}).fetchall()]


def announce(
    conn: Conn, stats: RunStats, *, market: str, source_name: str, headlines: list[Headline],
    series_zone: Callable[[str], str | None], today: date, window_days: int = EVENT_WINDOW_DAYS,
) -> int:
    """Events for `headlines` on linked V1 sites; `series_zone` maps a site's capacity_zone value to the series
    zone slug it is priced in (NYISO 'A' -> 'nyca'). Headlines published more than `window_days` before `today`
    are history and raise no event. Returns the number of new events."""
    cutoff = today - timedelta(days=window_days)
    by_zone: dict[str, list[Headline]] = {}
    for h in headlines:
        if h.published >= cutoff:
            by_zone.setdefault(h.zone, []).append(h)
    if not by_zone:
        return 0
    events = EventWriter(conn)
    new = 0
    linked: list[str] = []
    for slug, zone in linked_v1_sites(conn, market):
        for h in by_zone.get(series_zone(zone) or "", []):
            res = events.add("site", slug, h.published, EVENT_KIND, h.title, tag="F", source_name=source_name,
                             source_url=h.source_url, document_id=h.document_id, status="published")
            new += int(res.created)
            linked.append(f"{slug}:{zone}")
    stats.extra["events_new"] = stats.extra.get("events_new", 0) + new
    stats.extra["announced_to"] = sorted(set(stats.extra.get("announced_to", [])) | set(linked))
    return new
