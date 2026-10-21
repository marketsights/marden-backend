"""Which EIA-860M plant, if any, a Marden power plant is (used by `plant_locate`).

A Marden plant is an EIA plant when exactly one EIA plant in the workbook passes every test:

- state: the EIA Plant State is the plant's state;
- name: every distinctive word of the EIA Plant Name is in our plant's name (`eia_860m_store.names_match`:
  parentheticals in the EIA name dropped, CamelCase split, generic words such as 'energy', 'station', 'gas'
  dropped), and at least one of them is not a state name ('Louisiana 1' names no place of its own);
- technology: when our technology is known (gas, nuclear, fuel cell), at least one of the EIA plant's generators
  has it ('Natural Gas ...' / 'Other Natural Gas', 'Nuclear', prime mover FC). A wind farm that shares a name
  with a gas plant is not that gas plant;
- corroboration, at least one of:
  (a) operator: the company in an EIA Entity Name (its distinctive words that are not the plant's own name, or
      else its first distinctive word: 'NextEra' in 'NextEra Energy Point Beach LLC', 'Talen' in 'TalenEnergy
      Montour LLC'; org words such as 'public', 'power', 'district' and state names dropped) is in our operator,
      or the entity's acronym ('NPPD' for Nebraska Public Power District) is in our operator or plant name;
  (b) location: our reference point (the plant's own point, else its served campus's) within MATCH_KM;
  (c) capacity: our stated MW within CAPACITY_TOLERANCE of the EIA plant's planned or existing nameplate MW;
  (d) phrase: an EIA name of two or more distinctive words appears as a phrase in our name, and our technology
      is known and agrees ('Permian Basin' in 'Permian Basin Power Plant expansion').

Two or more passing EIA plants (a portfolio such as 'Hyperion CCGTs (first 3 plants)', or 'Port Washington' and
'Point Beach' in one name) or a name match with nothing to corroborate it is not a match: both are listed in the
run meta for a person. A plant that already carries an EIA plant id (the column, or an `eia_plant_id` fact from
eia_860m's name+location match) is that EIA plant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from marden.collectors.eia_860m_parse import OPERATING, PLANNED, SHEETS, Generator, Report
from marden.collectors.eia_860m_store import (
    GENERIC_WORDS,
    MATCH_KM,
    EiaPlant,
    eia_name_words,
    eia_plants,
    km_between,
    name_words,
    word_list,
)
from marden.seed.facilities_geo import US_STATES

METHOD = "plant-locate-eia@0.1"
CAPACITY_TOLERANCE = 0.15
ORG_WORDS = frozenset({
    "public", "service", "services", "district", "cooperative", "coop", "assn", "association", "agency", "agny",
    "authority", "municipal", "mun", "pwr", "city", "county", "town", "village", "corp", "corporation", "holdings",
    "holding", "partners", "partnership", "resources", "operating", "operations", "development", "renewable",
    "renewables", "ventures", "group", "international", "systems", "system", "light", "utilities", "utility",
    "northern", "southern", "eastern", "western", "central", "north", "south", "east", "west", "america", "american",
    "national", "united", "states", "us", "usa", "nuclear",
})
STATE_WORDS = frozenset(w for name in US_STATES for w in name.lower().split())
ACRONYM_SKIP = frozenset({"of", "the", "and", "co", "inc", "llc", "lp", "corp", "company"})
TECH_BY_PLANT_TECH = {"Simple-cycle turbine": "gas", "Reciprocating engine": "gas", "Combined cycle": "gas",
                      "Fuel cell": "fuel_cell"}
TECH_BY_TYPE = {"gas plant": "gas", "fuel cell": "fuel_cell", "SMR": "nuclear", "nuclear restart": "nuclear"}


def our_tech(technology: Any, technology_type: Any) -> str | None:
    """Our plant's technology class (gas, nuclear, fuel_cell) from its PlantTech, else its project type."""
    return TECH_BY_PLANT_TECH.get(technology) or TECH_BY_TYPE.get(technology_type)


def generator_techs(g: Generator) -> set[str]:
    tech = g.descriptor.get("technology") or ""
    out = set()
    if g.descriptor.get("prime_mover") == "FC":
        out.add("fuel_cell")
    if "Natural Gas" in tech:
        out.add("gas")
    if tech == "Nuclear":
        out.add("nuclear")
    return out


def entity_words(name: str) -> list[str]:
    seen = set()
    out = []
    for w in word_list(name):
        if len(w) > 1 and w not in GENERIC_WORDS | ORG_WORDS | STATE_WORDS and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def company_words(entity: str, plant_name: str) -> set[str]:
    """Who an EIA Entity Name says runs the plant: its distinctive words that are not the plant's own name, else
    its first distinctive word ('TalenEnergy Montour LLC' for the plant 'TalenEnergy Montour' -> talen)."""
    words = entity_words(entity)
    rest = set(words) - eia_name_words(plant_name)
    return rest or set(words[:1])


def acronym(name: str) -> str | None:
    letters = "".join(w[0] for w in word_list(name) if w not in ACRONYM_SKIP and not w.isdigit())
    return letters if len(letters) >= 3 else None


def contains_phrase(words: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(words[i:i + n] == phrase for i in range(len(words) - n + 1))


@dataclass(frozen=True)
class OurPlant:
    id: str
    name: str
    state: str | None
    operator: str = ""
    tech: str | None = None
    capacity_mw: float | None = None
    point: tuple[float, float] | None = None  # the plant's own point, else its served campus's
    point_basis: str = ""
    eia_plant_id: int | None = None  # power_plant.eia_plant_id, else an eia_plant_id fact


@dataclass
class EiaSite:
    """One EIA plant as the locator reads it: identity, operators, technologies, capacities, the cited row."""

    plant: EiaPlant
    county: str | None
    entities: list[str]
    techs: set[str]
    planned_mw: float
    existing_mw: float
    row: Generator  # the row cited for its coordinates (first by sheet order, then row)

    @property
    def id(self) -> int:
        return self.plant.plant_id


def sites(report: Report) -> dict[int, EiaSite]:
    order = {s: i for i, s in enumerate(SHEETS)}
    out: dict[int, EiaSite] = {}
    for pid, p in eia_plants(report).items():
        gens = sorted(p.generators, key=lambda g: (order[g.sheet], g.row))
        located = [g for g in gens if g.descriptor.get("latitude") is not None] or gens
        entities = sorted({g.descriptor["entity_name"] for g in gens if g.descriptor.get("entity_name")})
        out[pid] = EiaSite(
            p, gens[0].descriptor.get("county"), entities, set().union(*(generator_techs(g) for g in gens)),
            sum(g.descriptor.get("nameplate_mw") or 0 for g in gens if g.sheet == PLANNED),
            sum(g.descriptor.get("nameplate_mw") or 0 for g in gens if g.sheet == OPERATING),
            located[0],
        )
    return out


@dataclass
class Outcome:
    status: str  # matched | by_eia_plant_id | eia_plant_id_not_in_report | ambiguous | name_only | no_name_match
    site: EiaSite | None = None
    evidence: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)


def corroboration(ours: OurPlant, site: EiaSite) -> list[str]:
    """The records that agree our plant is this EIA plant besides its name (empty: none)."""
    out: list[str] = []
    operator = name_words(ours.operator)
    tokens = set(word_list(ours.operator)) | set(word_list(ours.name))
    for ent in site.entities:
        shared = sorted(company_words(ent, site.plant.name) & operator)
        abbr = acronym(ent)
        if shared:
            out.append(f"operator: EIA entity '{ent}' shares {', '.join(shared)} with '{ours.operator}'")
        elif abbr and abbr in tokens:
            out.append(f"operator: '{abbr.upper()}' in ours is the EIA entity '{ent}'")
    if ours.point and site.plant.lat is not None:
        km = km_between(ours.point[0], ours.point[1], site.plant.lat, site.plant.lon)
        if km <= MATCH_KM:
            out.append(f"location: {km:.2f} km from {ours.point_basis}")
    if ours.capacity_mw:
        for label, mw in (("planned", site.planned_mw), ("existing", site.existing_mw)):
            if mw and abs(ours.capacity_mw - mw) <= CAPACITY_TOLERANCE * mw:
                out.append(f"capacity: {ours.capacity_mw:g} MW stated vs EIA {label} nameplate {mw:g} MW")
                break
    phrase = [w for w in word_list(site.plant.name.split("(")[0]) if len(w) > 1 and w not in GENERIC_WORDS]
    if len(phrase) >= 2 and ours.tech and ours.tech in site.techs and contains_phrase(word_list(ours.name), phrase):
        out.append(f"name: '{' '.join(phrase)}' is a phrase in our name and the technology ({ours.tech}) agrees")
    return out


def _candidate(site: EiaSite, why: str) -> dict[str, Any]:
    return {"eia_plant_id": site.id, "eia_name": site.plant.name, "entities": site.entities, "why": why}


def match(ours: OurPlant, by_id: dict[int, EiaSite], by_state: dict[str, list[EiaSite]]) -> Outcome:
    if ours.eia_plant_id is not None:
        site = by_id.get(ours.eia_plant_id)
        if site is None:
            return Outcome("eia_plant_id_not_in_report")
        return Outcome("by_eia_plant_id", site, [f"EIA plant id {site.id} already on the plant"])
    state = (ours.state or "").strip().upper()
    mine = name_words(ours.name)
    named = [s for s in by_state.get(state, []) if (w := eia_name_words(s.plant.name)) - STATE_WORDS and w <= mine]
    if not named:
        return Outcome("no_name_match")
    passing: list[tuple[EiaSite, list[str]]] = []
    candidates: list[dict[str, Any]] = []
    for site in named:
        if ours.tech and ours.tech not in site.techs:
            candidates.append(_candidate(site, f"technology: ours {ours.tech}, EIA {sorted(site.techs) or 'other'}"))
            continue
        evidence = corroboration(ours, site)
        if evidence:
            passing.append((site, evidence))
            candidates.append(_candidate(site, "; ".join(evidence)))
        else:
            candidates.append(_candidate(site, "name only: no operator, location or capacity agrees"))
    if len(passing) == 1:
        site, evidence = passing[0]
        return Outcome("matched", site, [f"name: '{site.plant.name}' in '{ours.name}'", *evidence], candidates)
    return Outcome("ambiguous" if passing else "name_only", None, [], candidates)


def index(report: Report) -> tuple[dict[int, EiaSite], dict[str, list[EiaSite]]]:
    by_id = sites(report)
    by_state: dict[str, list[EiaSite]] = {}
    for site in by_id.values():
        by_state.setdefault(site.plant.state, []).append(site)
    return by_id, by_state
