"""Pipeline grouping (read model `pipeline`, views/pipeline.py): which signals belong to the same project.

A signal source is a `Candidate`: a tracked campus, an interconnection-queue load request, or an announced project
Marden holds as `pipeline_project` facts. Candidates are grouped into projects by developer + county + coordinates
or parcel, and a group links to at most one tracked campus:

- an explicit link (the queue collector's `linked_campus`, a `pipeline_project`'s stated `campusId`) always joins;
- the same parcel id in the same state joins;
- otherwise both must sit in the same state and county, name the same developer or share a distinctive developer
  or tenant word (not a county, state or generic word such as "data", "center" or "LLC"; a tracked campus's own
  name counts on its side), and, when both have a surveyed point, lie within `NEAR_KM` of each other. Town names
  in project names alone never join two projects ("1 Gig Data Center East Fishkill" and "iPark East Fishkill"
  have different developers).

Two tracked campuses never merge (each is its own row). Pure functions only: the database reads are in
pipeline_signals.py.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

STAGES = ("announced", "land_acquired", "permits_filed", "power_requested", "permits_approved", "construction_started")
STAGE_LABELS = {
    "announced": "Announced",
    "land_acquired": "Land acquired",
    "permits_filed": "Rezoning / permits filed",
    "power_requested": "Power requested",
    "permits_approved": "Permits approved",
    "construction_started": "Construction started",
}
# What usually comes after the furthest stage reached (the "next expected step" when no source states one).
NEXT_STEP = {
    "announced": "Land purchase or option",
    "land_acquired": "Rezoning or permit filing",
    "permits_filed": "Utility power request or permit approval",
    "power_requested": "Rezoning or permit approval",
    "permits_approved": "Construction start",
    "construction_started": "First building in service",
}
NEAR_KM = 25.0

GENERIC = frozenset(
    [
        "llc",
        "inc",
        "corp",
        "corporation",
        "co",
        "company",
        "companies",
        "ltd",
        "lp",
        "l",
        "p",
        "the",
        "and",
        "of",
        "for",
        "a",
        "an",
        "at",
        "in",
        "on",
        "by",
        "data",
        "datacenter",
        "datacenters",
        "center",
        "centers",
        "centre",
        "campus",
        "campuses",
        "site",
        "sites",
        "project",
        "projects",
        "park",
        "digital",
        "tech",
        "technology",
        "technologies",
        "infrastructure",
        "infra",
        "energy",
        "power",
        "holdings",
        "holding",
        "group",
        "partners",
        "partner",
        "development",
        "developments",
        "developer",
        "ventures",
        "capital",
        "properties",
        "property",
        "realty",
        "services",
        "operating",
        "systems",
        "solutions",
        "international",
        "global",
        "america",
        "american",
        "north",
        "south",
        "east",
        "west",
        "new",
        "ai",
        "compute",
        "computing",
        "cloud",
        "hyperscale",
        "hpc",
        "load",
        "loads",
        "phase",
        "increase",
        "incremental",
        "request",
        "interconnection",
        "mw",
        "gw",
        "megawatt",
        "gigawatt",
        "building",
        "buildings",
        "facility",
        "facility",
        "facilities",
        "township",
        "county",
        "city",
        "parish",
        "state",
        "road",
        "rd",
        "highway",
        "hwy",
        "drive",
        "dr",
        "street",
        "st",
        "industrial",
        "innovation",
        "redevelopment",
        "management",
        "expansion",
        "former",
        "proposed",
        "planned",
        "district",
        "public",
        "utility",
        "utilities",
        "electric",
        "cooperative",
        "coop",
        "rural",
        "association",
        "municipal",
        "generation",
        "authority",
        "economic",
        "devel",
    ]
)


def norm(text: str | None) -> str:
    """Lower case, punctuation to spaces, single spaces ('St. Lawrence' and 'St; Lawrence' -> 'st lawrence')."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def county_key(county: str | None) -> str:
    """A county name without its suffix ('Richland Parish' -> 'richland', 'Doña Ana County' -> 'do a ana')."""
    words = [w for w in norm(county).split() if w not in ("county", "parish", "borough", "co")]
    return " ".join(words)


def words(*texts: str | None) -> set[str]:
    """Distinctive words of names and developers: three letters or more, not generic."""
    out: set[str] = set()
    for t in texts:
        out.update(w for w in norm(t).split() if len(w) >= 3 and w not in GENERIC and not w.isdigit())
    return out


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, h)))


@dataclass
class Signal:
    stage: str
    day: date
    text: str
    tag: str
    source_name: str
    source_url: str | None
    origin: str  # 'campus' | 'announcement' | 'queue'
    quote: str | None = None

    def doc(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "stage": self.stage,
            "date": self.day.isoformat(),
            "text": self.text,
            "tag": self.tag,
            "sourceName": self.source_name,
            "sourceUrl": self.source_url,
            "from": self.origin,
        }
        if self.quote:
            out["quote"] = self.quote
        return out


@dataclass
class Candidate:
    key: str  # 'campus:<id>' | 'queue:<id>' | 'project:<id>'
    origin: str  # 'tracked' | 'queue' | 'announced'
    name: str
    state: str | None
    county: str | None
    developer: dict[str, Any] | None = None  # Sourced<string> with a value, or None
    tenant: dict[str, Any] | None = None
    point: tuple[float, float] | None = None  # surveyed site point (lat, lon); None for county-level records
    parcels: frozenset[str] = frozenset()
    mw: dict[str, Any] | None = None  # Sourced<number>
    campus_link: str | None = None  # an explicit tracked-campus id
    signals: list[Signal] = field(default_factory=list)
    financing: dict[str, Any] | None = None
    next_step: dict[str, Any] | None = None
    aliases: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def campus_id(self) -> str | None:
        return self.key.split(":", 1)[1] if self.origin == "tracked" else None

    def party(self) -> str:
        return norm((self.developer or {}).get("value"))

    def party_words(self) -> set[str]:
        """Distinctive words of the developer and the tenant (not of the place)."""
        place = words(self.county, self.state)
        return words((self.developer or {}).get("value"), (self.tenant or {}).get("value")) - place

    def name_words(self) -> set[str]:
        place = words(self.county, self.state)
        return words(self.name, *self.aliases) - place


def match_reason(a: Candidate, b: Candidate) -> str | None:
    """Why `a` and `b` are the same project (a short phrase for the row's 'grouped by'), or None."""
    if a.origin == "tracked" and b.origin == "tracked":
        return None
    for x, y in ((a, b), (b, a)):
        if x.campus_link and y.origin == "tracked" and x.campus_link == y.campus_id:
            return "linked to the tracked campus"
    same_state = bool(a.state) and norm(a.state) == norm(b.state)
    if same_state and a.parcels & b.parcels:
        return "same parcel"
    if not same_state or not a.county or county_key(a.county) != county_key(b.county):
        return None
    if a.party() and a.party() == b.party():
        shared = {a.party()}
    elif a.origin == "tracked" or b.origin == "tracked":
        tracked, other = (a, b) if a.origin == "tracked" else (b, a)
        shared = other.party_words() & (tracked.party_words() | tracked.name_words())
    else:
        shared = a.party_words() & b.party_words()
    if not shared:
        return None
    if a.point and b.point:
        distance = km(a.point, b.point)
        if distance > NEAR_KM:
            return None
        return f"developer + county + coordinates ({distance:.0f} km apart)"
    return f"developer + county ({', '.join(sorted(shared))})"


def group(cands: Iterable[Candidate]) -> list[tuple[list[Candidate], list[str]]]:
    """Groups of candidates (each with its reasons). A group holds at most one tracked campus: a candidate that
    matches two is joined to the one it matches first in key order with the strongest reason."""
    items = sorted(cands, key=lambda c: (c.origin != "tracked", c.key))
    parent = list(range(len(items)))
    tracked_of: dict[int, int | None] = {i: (i if c.origin == "tracked" else None) for i, c in enumerate(items)}
    reasons: dict[int, list[str]] = {i: [] for i in range(len(items))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pairs: list[tuple[int, int, int, str]] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            why = match_reason(items[i], items[j])
            if why:
                pairs.append((_strength(why), i, j, why))
    for _, i, j, why in sorted(pairs, key=lambda p: (p[0], p[1], p[2])):
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        ti, tj = tracked_of[ri], tracked_of[rj]
        if ti is not None and tj is not None:
            continue  # would join two tracked campuses
        parent[rj] = ri
        tracked_of[ri] = ti if ti is not None else tj
        reasons[ri] = reasons[ri] + reasons[rj] + [why]
    out: dict[int, list[Candidate]] = {}
    for i, c in enumerate(items):
        out.setdefault(find(i), []).append(c)
    return [(members, sorted(set(reasons[root]))) for root, members in out.items()]


def _strength(why: str) -> int:
    if why.startswith("linked"):
        return 0
    if why.startswith("same parcel"):
        return 1
    if "coordinates" in why:
        return 2
    return 3


def furthest(signals: Iterable[Signal]) -> str | None:
    stages = [s.stage for s in signals]
    return max(stages, key=STAGES.index) if stages else None
