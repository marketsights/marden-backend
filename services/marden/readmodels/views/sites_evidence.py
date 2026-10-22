"""Completion's 'Last evidence' (spec 03: the latest material event): a dated record about the site.

Candidates are the site's published past-dated events, the events of its linked campuses and of the
plants serving them, and the case research's dated evidence items (risk_evidence facts that cite a
document). A record counts only when it is about the site or its chain parties (`sites_names`): its
text names the site, its project, campus, plant, SPV or a chain party, or its document does (URL slug,
a chain party's SEC filing, the source of the site's debt terms). A permit, interconnection or queue
record of the site's own plant or campus, and the site's own flagged schedule step (catalyst), are
about the site by construction. Left out whatever they say: market-wide prints (capacity auction
results, GPU spot prices '$1.99/hr'); a regulator order, county agenda or press item that names none
of the above is noise for this site.

Among the records that count: material events first (approved, judged material by the archive, or
flagged red / amber), then filed records (F), then reported ones (R), then model notes (M); the most
recent first within each. None when no record about the site exists yet (shown as n/d).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows
from marden.readmodels.views.sites_common import (
    Doc,
    day,
    event_evidence,
    fact_evidence,
    material_events,
    next_day,
    severity,
    site_row,
)
from marden.readmodels.views.sites_links import linked_campuses, site_plants
from marden.readmodels.views.sites_names import SiteNames, site_names

OWN_KINDS = frozenset({"permit", "interconnection", "queue", "catalyst"})  # keyed to this site / plant / campus
MARKET_KINDS = frozenset({"capacity_auction"})  # zone-wide auction prints
PRICE_PRINT = re.compile(r"\$\s?\d[\d.,]*\s*/\s*(?:hr|hour|gpu-?hr)\b|\bspot prints?\b", re.I)
TAG_TIER = {"F": 1, "R": 2, "M": 3}
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Candidate:
    tier: int  # 0 material, 1 filed, 2 reported, 3 model
    date: str
    strength: int  # 0 the text names the site or a party, 1 only its document does (sites_names)
    order: int  # 0 the site's own records, 1 campus / plant records, 2 research items off the build drivers
    evidence: Doc

    def key(self) -> tuple:
        return (self.tier, -date.fromisoformat(self.date).toordinal(), self.strength, self.order)


def linked_events(conn: Conn, subject_type: str, ids: Sequence[str]) -> list[Row]:
    """Published events of the site's campuses or plants Marden knew by the build date, past-dated."""
    if not ids:
        return []
    return conn.execute(
        """SELECT * FROM event WHERE subject_type = %s AND subject_id = ANY(%s) AND status = 'published'
             AND learned_at < %s::date AND event_date <= %s ORDER BY event_date DESC, id DESC""",
        (subject_type, list(ids), next_day(), day()),
    ).fetchall()


def market_print(kind: str, *texts: str | None) -> bool:
    return kind in MARKET_KINDS or any(t and PRICE_PRINT.search(t) for t in texts)


def event_candidate(ev: Row, names: SiteNames, material: bool, order: int) -> Candidate | None:
    kind, title = ev["kind"], ev["title"]
    flagged = material or severity(ev) in ("red", "amber") or bool(ev.get("material"))
    if (kind == "catalyst" and not flagged) or market_print(kind, title):
        return None  # an unflagged schedule step is a plan, not evidence; a market print is not about the site
    strength = 0 if kind in OWN_KINDS else names.strength(ev.get("source_url"), title)
    if strength is None:
        return None
    tier = 0 if flagged else TAG_TIER.get(ev["tag"], 3)
    return Candidate(tier, ev["event_date"].isoformat(), strength, order, event_evidence(ev))


def research_candidates(conn: Conn, slug: str, names: SiteNames) -> list[Candidate]:
    """Dated case-research items about the site, each with the document it cites (an archive note
    with no document is not a record); the item's quote and summary count as its text. On the same
    day, an item on a build driver (power, construction, permits, tenant ...) comes before 'other'."""
    out = []
    for row in known_fact_rows(conn, "site", slug, ["risk_evidence"])["risk_evidence"]:
        v: dict[str, Any] = row["value"] if isinstance(row["value"], dict) else {}
        when, text, url = v.get("date"), v.get("headline"), v.get("url") or row.get("source_url") or ""
        if not (isinstance(when, str) and DAY.fullmatch(when) and isinstance(text, str) and text and url):
            continue  # undated ('2026'), headless or document-less items are not a dated record
        if when > day().isoformat() or market_print("", text, row.get("quote"), v.get("summary")):
            continue
        strength = names.strength(url, text, row.get("quote"), v.get("summary"))
        if strength is None:
            continue
        rec = fact_evidence(row, text, when)
        rec["sourceUrl"] = url
        out.append(Candidate(TAG_TIER.get(row["tag"], 3), when, strength, 2 if v.get("driver") == "other" else 0, rec))
    return out


def evidence_candidates(conn: Conn, slug: str, events: Sequence[Row]) -> list[Candidate]:
    """Every record about the site, best first (tier, then newest, then the site's own records)."""
    site = site_row(conn, slug)
    if site is None:
        return []
    campuses, _approved = linked_campuses(conn, slug)
    plants = site_plants(conn, slug, campuses)
    names = site_names(conn, site, campuses, plants, next_day())
    material = {ev["id"] for ev, _note in material_events(conn, slug, events)}
    found = [c for ev in events if (c := event_candidate(ev, names, ev["id"] in material, 0))]
    linked = linked_events(conn, "campus", [c["id"] for c in campuses])
    linked += linked_events(conn, "plant", [p["id"] for p in plants])
    found += [c for ev in linked if (c := event_candidate(ev, names, False, 1))]
    found += research_candidates(conn, slug, names)
    found.sort(key=Candidate.key)
    return found


def last_evidence(conn: Conn, slug: str, events: Sequence[Row]) -> Doc | None:
    """Completion's 'Last evidence' (module doc); None (n/d) when no record about the site exists."""
    found = evidence_candidates(conn, slug, events)
    return found[0].evidence if found else None
