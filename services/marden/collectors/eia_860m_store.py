"""Change-only writes of one EIA-860M report: eia_generator history, slip counts, and matched Marden plants.

- Subjects 'eia_generator' (subject_id '<plant id>_<generator id>', e.g. '63798_ATL01'): every generator on the
  Planned sheet in a data-center state (DC_STATES), and every generator already tracked (it was planned in an
  earlier report Marden stored) wherever it is now listed. Fields, tag F, source 'EIA-860M', valid_from = the
  report month's last day: `generator` (descriptor: plant, owner, county, BA, technology, MW, coordinates),
  `status` (EIA code; quote = the Status cell), `planned_online` ('YYYY-MM'), `operating_month` (actual, once
  operating), `retired_month`. A value is written only when it differs from Marden's value as of that report, so
  the stored rows are the planned-vs-actual history (fact_asof answers what a report said) and a rerun writes
  nothing. Nothing is derived; slips are only counted in run meta.
- Plants (power_plant): matched by eia_plant_id, else by name (the EIA plant name's distinctive words all in ours;
  parentheticals in the EIA name dropped, CamelCase split: 'TalenEnergy' = 'Talen Energy') in the same state
  within MATCH_KM of the plant's point, or, for a plant without one, of its served campus's point (exactly one
  served campus with a point, in the plant's state: the stand-in grid_join uses). Facts `planned_online`
  ({generator id: 'YYYY-MM'}) and `eia_status` ({generator id: EIA code}); a plant's own `status` keeps Marden's
  PlantStatus vocabulary (spec 15), so EIA codes go to `eia_status`. Tag F on an eia_plant_id match; tag M
  (MATCH_METHOD) plus an `eia_plant_id` fact on a name+location match. power_plant.eia_plant_id and the plant's
  point are set from those facts by `plant_locate`, which owns those columns.
"""

from __future__ import annotations

import math
import re
import unicodedata
from calendar import monthrange
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.collectors.eia_860m_parse import (
    CANCELED,
    OPERATING,
    PLANNED,
    PLANNED_CODES,
    RETIRED,
    Generator,
    Report,
)
from marden.collectors.rates_common import RunStats
from marden.core.db import Conn
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter
from marden.core.jsonutil import canonical_json

SUBJECT = "eia_generator"
PRODUCER = "collector:eia_860m"
SOURCE_NAME = "EIA-860M"
MATCH_METHOD = "eia860m-plant-match@0.1"
MATCH_KM = 2.0
DC_STATES = frozenset({"TX", "NM", "LA", "OH", "WI", "MI", "ND", "TN", "MS", "GA", "VA", "AZ", "PA", "IN", "NY"})
CANDIDATES_SHOWN = 50
GENERIC_WORDS = frozenset({
    "the", "of", "and", "at", "energy", "center", "centre", "station", "generating", "generation", "power", "plant",
    "project", "facility", "llc", "inc", "co", "company", "unit", "units", "site", "campus", "complex", "gas",
    "natural", "electric", "solar", "wind", "storage", "battery", "bess", "hybrid", "farm", "park", "plc", "lp",
})


def report_date(year: int, month: int) -> date:
    """valid_from of a report: the inventory is 'as of <month>', i.e. at the month's end."""
    return date(year, month, monthrange(year, month)[1])


_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_PARENS = re.compile(r"\([^)]*\)")


def word_list(text: str) -> list[str]:
    """Lower-case ASCII words in order, CamelCase split ('TalenEnergy Montour' -> talen, energy, montour)."""
    ascii_text = unicodedata.normalize("NFKD", _CAMEL.sub(" ", text)).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", ascii_text)


def name_words(text: str) -> set[str]:
    return {w for w in word_list(text) if len(w) > 1 and w not in GENERIC_WORDS}


def eia_name_words(eia: str) -> set[str]:
    """The distinctive words of an EIA plant name, without its parentheticals ('Paris (WI)', '... (PMG)')."""
    return name_words(_PARENS.sub(" ", eia))


def names_match(ours: str, eia: str) -> bool:
    words = eia_name_words(eia)
    return bool(words) and words <= name_words(ours)


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def load_prior(conn: Conn, as_of: date) -> dict[tuple[str, str, str], Any]:
    """(subject_type, subject_id, field) -> this collector's value as of `as_of`."""
    rows = conn.execute(
        """SELECT DISTINCT ON (subject_type, subject_id, field) subject_type, subject_id, field, value
             FROM fact WHERE producer = %s AND status = 'published' AND valid_from <= %s
            ORDER BY subject_type, subject_id, field, valid_from DESC, learned_at DESC, id DESC""",
        (PRODUCER, as_of),
    ).fetchall()
    return {(r["subject_type"], r["subject_id"], r["field"]): r["value"] for r in rows}


@dataclass
class EiaPlant:
    plant_id: int
    name: str
    state: str
    lat: float | None
    lon: float | None
    generators: list[Generator] = field(default_factory=list)


def eia_plants(report: Report) -> dict[int, EiaPlant]:
    """The report's generators grouped by EIA plant; the plant's coordinates from its first row that has them."""
    out: dict[int, EiaPlant] = {}
    for g in report.generators:
        d = g.descriptor
        p = out.get(g.plant_id)
        if p is None:
            p = out[g.plant_id] = EiaPlant(g.plant_id, d.get("plant_name", ""), g.state, None, None)
        if p.lat is None and d.get("latitude") is not None and d.get("longitude") is not None:
            p.lat, p.lon = d["latitude"], d["longitude"]
        p.generators.append(g)
    return out


SERVED_POINTS = """
SELECT ps.plant_id, c.id AS campus_id, ST_Y(c.geom) AS lat, ST_X(c.geom) AS lon, c.state AS campus_state,
       p.state AS plant_state, ps.evidence_fact_id
  FROM plant_serves ps JOIN campus c ON c.id = ps.campus_id JOIN power_plant p ON p.id = ps.plant_id
 WHERE c.geom IS NOT NULL ORDER BY ps.plant_id, c.id
"""


def served_campus_points(conn: Conn) -> dict[str, dict[str, Any]]:
    """plant id -> its served campus's point, for plants serving exactly one campus with a point in the plant's
    state (the stand-in grid_join uses): {campus_id, lat, lon, evidence_fact_id}."""
    by_plant: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(SERVED_POINTS).fetchall():
        by_plant.setdefault(r["plant_id"], []).append(dict(r))
    out: dict[str, dict[str, Any]] = {}
    for pid, rows in by_plant.items():
        if len(rows) != 1:
            continue
        r = rows[0]
        plant_state, campus_state = (r["plant_state"] or "").strip().upper(), (r["campus_state"] or "").strip().upper()
        if plant_state and plant_state != campus_state:
            continue
        out[pid] = {k: r[k] for k in ("campus_id", "lat", "lon", "evidence_fact_id")}
    return out


class ReportWriter:
    """Writes one parsed report on the caller's connection (one transaction per report)."""

    def __init__(self, conn: Conn, stats: RunStats, report: Report, doc: DocumentRow, *, tag: str = "F"):
        self.conn = conn
        self.stats = stats
        self.report = report
        self.doc = doc
        self.tag = tag
        self.day = report_date(report.year, report.month)
        self.facts = FactWriter(conn)
        self.prior = load_prior(conn, self.day)
        self.counts: Counter[str] = Counter()
        self.slips: Counter[str] = Counter()
        self.slips_by_state: Counter[str] = Counter()
        self.plants: dict[str, Any] = {}
        self.file_name = doc.url.rsplit("/", 1)[-1]

    # ------------------------------------------------------------ facts
    def _put(self, subject_type: str, subject_id: str, fld: str, value: Any, *, tag: str | None = None,
             quote: str | None = None, quote_loc: str | None = None) -> bool:
        self.stats.facts_seen += 1
        key = (subject_type, subject_id, fld)
        old = self.prior.get(key)
        if old is not None and canonical_json(old) == canonical_json(value):
            return False
        tag = tag or self.tag
        res = self.facts.add(
            subject_type, subject_id, fld, value, tag=tag, source_name=SOURCE_NAME, source_url=self.doc.url,
            valid_from=self.day, producer=PRODUCER, status="published", document_id=self.doc.id, quote=quote,
            quote_loc=quote_loc, method_version=MATCH_METHOD if tag == "M" else None,
        )
        self.prior[key] = value
        if res.created:
            self.stats.facts_new += 1
            self.counts[f"{subject_type}.{fld}"] += 1
        return res.created

    # ------------------------------------------------------------ generators
    def write_generators(self) -> None:
        tracked = {sid for (st, sid, _) in self.prior if st == SUBJECT}
        seen: set[str] = set()
        for g in self.report.generators:
            if g.state not in DC_STATES or not (g.sheet == PLANNED or g.key in tracked):
                continue
            seen.add(g.key)
            self._generator(g)
        for sid in tracked - seen:
            if self.prior.get((SUBJECT, sid, "status")) in PLANNED_CODES:
                self.slips["planned_then_unlisted"] += 1  # dropped without a Canceled row: nothing is written

    def _generator(self, g: Generator) -> None:
        sid = g.key
        old_status = self.prior.get((SUBJECT, sid, "status"))
        old_planned = self.prior.get((SUBJECT, sid, "planned_online"))
        self._count_slips(g, old_status, old_planned)
        loc = f"{self.file_name}!{g.sheet} row {g.row}"
        self._put(SUBJECT, sid, "generator", g.descriptor, quote_loc=loc)
        self._put(SUBJECT, sid, "status", g.status, quote=g.status_text, quote_loc=loc)
        if g.planned:
            self._put(SUBJECT, sid, "planned_online", g.planned, quote_loc=loc)
        if g.operating and g.sheet != PLANNED:
            self._put(SUBJECT, sid, "operating_month", g.operating, quote_loc=loc)
        if g.retired:
            self._put(SUBJECT, sid, "retired_month", g.retired, quote_loc=loc)

    def _count_slips(self, g: Generator, old_status: Any, old_planned: Any) -> None:
        if g.sheet == PLANNED:
            self.slips["planned"] += 1
            if old_status is None:
                self.slips["first_seen_planned"] += 1
            if g.planned and g.planned < self.report.label:
                self.slips["planned_month_passed"] += 1
            if isinstance(old_planned, str) and g.planned:
                if g.planned > old_planned:
                    self.slips["slipped"] += 1
                    self.slips_by_state[g.state] += 1
                elif g.planned < old_planned:
                    self.slips["pulled_in"] += 1
            return
        if old_status in PLANNED_CODES:
            outcome = {OPERATING: "came_online", CANCELED: "canceled_or_postponed", RETIRED: "retired"}[g.sheet]
            self.slips[outcome] += 1
            if outcome == "came_online" and isinstance(old_planned, str) and g.operating:
                late = g.operating > old_planned
                self.slips["came_online_after_last_plan" if late else "came_online_by_last_plan"] += 1

    # ------------------------------------------------------------ plants
    def eia_plants(self) -> dict[int, EiaPlant]:
        return eia_plants(self.report)

    def write_plants(self) -> None:
        plants = self.conn.execute(
            """SELECT id, name, state, eia_plant_id, ST_Y(geom) AS lat, ST_X(geom) AS lon FROM power_plant
                ORDER BY id"""
        ).fetchall()
        stand_in = served_campus_points(self.conn)
        eia = self.eia_plants()
        by_state: dict[str, list[EiaPlant]] = {}
        for p in eia.values():
            by_state.setdefault(p.state, []).append(p)
        counts: Counter[str] = Counter()
        unlocated: list[dict[str, Any]] = []
        for row in plants:
            if row["eia_plant_id"] is not None:
                match = eia.get(row["eia_plant_id"])
                counts["matched_by_eia_plant_id" if match else "eia_plant_id_not_in_report"] += 1
                if match:
                    self._plant_facts(row["id"], match, tag=self.tag)
                continue
            named = [p for p in by_state.get((row["state"] or "").upper(), []) if names_match(row["name"], p.name)]
            if not named:
                counts["no_name_match"] += 1
                continue
            lat, lon, where = row["lat"], row["lon"], "the plant point"
            if lat is None and row["id"] in stand_in:
                campus = stand_in[row["id"]]
                lat, lon = campus["lat"], campus["lon"]
                where = f"the served campus {campus['campus_id']} point (plant has no location)"
            if lat is None:
                counts["name_match_without_location"] += 1
                unlocated += [{"plant": row["id"], "eia_plant_id": p.plant_id, "eia_name": p.name} for p in named]
                continue
            near = [(km_between(lat, lon, p.lat, p.lon), p) for p in named if p.lat is not None]
            near = [(km, p) for km, p in near if km <= MATCH_KM]
            if len(near) != 1:
                counts["name_match_not_within_2km" if not near else "ambiguous"] += 1
                continue
            km, match = near[0]
            counts["matched_by_name_location" if row["lat"] is not None else "matched_by_name_served_campus"] += 1
            loc = f"{self.file_name}: '{match.name}' ({match.state}) {km:.2f} km from {where}"
            self._put("plant", row["id"], "eia_plant_id", match.plant_id, tag="M", quote_loc=loc)
            self._plant_facts(row["id"], match, tag="M")
        self.plants = {"plants": len(plants), **dict(sorted(counts.items())),
                       "name_candidates_without_location": unlocated[:CANDIDATES_SHOWN]}

    def _plant_facts(self, plant_id: str, match: EiaPlant, *, tag: str) -> None:
        gens = sorted(match.generators, key=lambda g: g.generator_id)
        loc = f"{self.file_name}: EIA plant {match.plant_id}"
        self._put("plant", plant_id, "eia_status", {g.generator_id: g.status for g in gens}, tag=tag, quote_loc=loc)
        planned = {g.generator_id: g.planned for g in gens if g.planned}
        if planned:
            self._put("plant", plant_id, "planned_online", planned, tag=tag, quote_loc=loc)

    # ------------------------------------------------------------ run meta
    def summary(self) -> dict[str, Any]:
        dc = [g for g in self.report.generators if g.state in DC_STATES]
        return {
            "report": self.report.label, "url": self.doc.url, "document_id": self.doc.id,
            "document_new": self.doc.was_new, "rows": self.report.rows, "skipped_rows": self.report.skipped,
            "dc_state_generators": len(dc), "dc_state_planned": sum(g.sheet == PLANNED for g in dc),
            "facts_new": dict(sorted(self.counts.items())), "slips": dict(sorted(self.slips.items())),
            "slipped_by_state": dict(sorted(self.slips_by_state.items())), "plants": self.plants,
        }


def write_report(conn: Conn, stats: RunStats, report: Report, doc: DocumentRow, *, tag: str = "F") -> dict[str, Any]:
    writer = ReportWriter(conn, stats, report, doc, tag=tag)
    writer.write_generators()
    writer.write_plants()
    return writer.summary()
