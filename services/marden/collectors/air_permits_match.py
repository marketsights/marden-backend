"""Air permits (spec 15/20): what a state permit record is, what Marden knows about each plant, and the
conservative rule that ties one to the other (method air-permit-match@0.1).

A `Facility` is one permitted site in a state system (a TCEQ regulated entity, an NMED application, an
Iowa DNR plant number) with its permits. A `PlantProfile` is one `power_plant` row with the names,
counties and permit numbers its own records give. A facility is tied to a plant only when all hold:

1. same state;
2. a distinctive name phrase of the plant's operator, applicant, owner or chain parties (the phrase rules
   of queue-match@0.1: generic words such as LLC, energy, data, center are dropped) appears as whole words
   in the facility's applicant / customer names or its facility name;
3. the record shows power generation (turbines, engines, fuel cells, an electric generating NAICS / SIC,
   'electricity generation' ...), quoted verbatim;
4. location agrees: the facility's county is one of the plant's counties. Where Marden holds no county for
   the plant, a permit number the plant's own records cite, or the facility's own project name found in the
   plant's name (two or more words, at least one distinctive: 'Project Llano', 'GW Ranch'), stands in;
5. no conflict: a facility in a county the plant's records do not list is never tied to it.

Each facility goes to the one plant with the most evidence; a tie between plants is not resolved by
guessing and goes to the run report, as does every candidate that fails a rule (with the reasons).
Plant counties come from the plant row, the campuses it serves, and '<Name> County' / '<Name> Parish'
written in the plant's name, its served-campus text or a county body named as a docket agency, each
checked against the Census county list for the plant's state.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.collectors.iso_queue_match import LEGAL, contains, county_words, phrase, split_names, tokens
from marden.core.db import Conn

METHOD_VERSION = "air-permit-match@0.1"
GENERATION = re.compile(
    r"\b(electric(ity|al)?\s+(power\s+)?generat\w*|power\s+generat\w*|generat\w+\s+(unit|station|facility|plant)s?"
    r"|EGUs?|combined[- ]cycle|simple[- ]cycle|peak(ing|er)|turbines?|reciprocating|engine generators?"
    r"|fuel cells?|microgrid|power plant|energy server|generator sets?|genset)\b",
    re.I,
)
PERMIT_NO = re.compile(r"\b(GHGPSDTX\d+|PSDTX\d+|NSR\s?\d{4,6}|\d{2}-A-\d{3}|\d{5,6})\b")
PERMIT_WORDS = re.compile(r"\b(air|permit|registration|NSR|PSD|TCEQ|NMED|DNR)\b", re.I)
PARTY_ROLES = frozenset({"developer_builder", "power_supplier"})
# Single words that name no company on their own (queue-match@0.1's list covers places and resources).
WEAK_SINGLE = frozenset({"associates", "private", "build", "builder", "owner", "owners", "affiliate", "last", "rep",
                         "filings", "state", "public", "district", "cooperative", "electric", "utilities", "transfer"})


@dataclass
class Action:
    """One dated record in a permit's history (application, notice, draft, decision, amendment, withdrawal)."""

    day: date
    kind: str
    text: str
    url: str
    project: str | None = None
    quote: str | None = None
    document_id: int | None = None


@dataclass
class Permit:
    agency: str
    number: str
    ids: list[str]
    type: str | None = None  # 'Standard', 'PBR', 'PSD', 'Title V', 'NSR', 'Minor', 'Major'
    status: str | None = None  # pending | issued | withdrawn | denied | cancelled | inactive
    agency_status: str | None = None  # the agency's own word, as written
    applicant: str | None = None
    filed: date | None = None
    issued: date | None = None
    expires: date | None = None
    units: list[dict[str, Any]] = field(default_factory=list)  # stated in the record, each with its quote
    emission_limits: list[dict[str, Any]] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    url: str = ""
    quote: str | None = None  # verbatim text stating the number and status
    quote_loc: str | None = None
    document_id: int | None = None


@dataclass
class Facility:
    system: str  # source id
    agency: str  # 'TCEQ', 'NMED', 'Iowa DNR'
    state: str
    key: str  # the system's own id: RN112029061, NSR 10883, 01-09-001
    name: str
    county: str | None
    names: list[str]  # applicant / customer / owner names on the record
    generation: str | None  # verbatim text showing power generation, or None
    permits: list[Permit]
    url: str
    document_id: int | None = None
    quote: str | None = None
    aliases: list[str] = field(default_factory=list)  # project names the record gives ('Project Jupiter')


@dataclass
class PlantProfile:
    id: str
    name: str
    state: str
    technology: str | None
    counties: dict[tuple[str, ...], str]  # county words -> where Marden's records give it
    phrases: list[tuple[tuple[str, ...], str]]  # operator / applicant name phrases and their origin
    name_tokens: list[str]
    numbers: dict[str, str]  # normalized permit number -> where the plant's records cite it


@dataclass
class Match:
    facility: Facility
    plant: PlantProfile | None
    evidence: dict[str, Any]
    reasons: list[str]  # why it is not matched (empty when matched)


def norm_number(text: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "", text.upper().replace("NSR", ""))


# ---------------------------------------------------------------- plant profiles
def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return []


def _county_names(conn: Conn, states: Iterable[str]) -> dict[str, list[tuple[str, ...]]]:
    rows = conn.execute("SELECT stusps, name FROM us_county WHERE stusps = ANY(%s)", (sorted(set(states)),)).fetchall()
    out: dict[str, list[tuple[str, ...]]] = {}
    for r in rows:
        if words := county_words(r["name"]):
            out.setdefault(r["stusps"], []).append(words)
    return out


def counties_in(text: str | None, known: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Census counties of the state written in `text` as '<Name> County' or '<Name> Parish'."""
    toks = tokens(text)
    return [c for c in known if contains(toks, (*c, "county")) or contains(toks, (*c, "parish"))]


PROFILE_SQL = """
SELECT p.id, p.name, p.state, p.county,
       (SELECT jsonb_object_agg(f.field, f.value) FROM fact_current f
         WHERE f.subject_type = 'plant' AND f.subject_id = p.id
           AND f.field IN ('operator', 'owner', 'parties', 'serves_text', 'dockets', 'technology',
                           'operator_entity_id')) AS facts,
       -- an earlier record's permit (seed archive), never this collector's own: a match must not feed itself
       (SELECT f.value FROM fact f WHERE f.subject_type = 'plant' AND f.subject_id = p.id AND f.field = 'air_permit'
           AND f.status = 'published' AND f.producer <> 'collector:air_permits'
         ORDER BY f.valid_from DESC, f.learned_at DESC, f.id DESC LIMIT 1) AS prior_permit,
       (SELECT array_agg(DISTINCT c.county) FROM plant_serves ps JOIN campus c ON c.id = ps.campus_id
         WHERE ps.plant_id = p.id AND c.county IS NOT NULL AND c.state = p.state) AS campus_counties
  FROM power_plant p
 WHERE p.state IS NOT NULL
 ORDER BY p.id
"""


def load_profiles(conn: Conn, states: Iterable[str] | None = None) -> list[PlantProfile]:
    rows = conn.execute(PROFILE_SQL).fetchall()
    if states is not None:
        wanted = set(states)
        rows = [r for r in rows if r["state"] in wanted]
    county_list = _county_names(conn, {r["state"] for r in rows})
    entity_ids = {(r["facts"] or {}).get("operator_entity_id") for r in rows}
    entity_names = {
        e["id"]: e["name"]
        for e in conn.execute("SELECT id, name FROM entity WHERE id = ANY(%s)",
                              (sorted(i for i in entity_ids if isinstance(i, str)),)).fetchall()
    }
    return [profile(r, county_list.get(r["state"], []), entity_names) for r in rows]


def profile(row: dict[str, Any], county_list: list[tuple[str, ...]], entity_names: dict[str, str]) -> PlantProfile:
    facts = row.get("facts") or {}
    counties: dict[tuple[str, ...], str] = {}
    if words := county_words(row.get("county")):
        counties[words] = "plant county"
    for c in row.get("campus_counties") or []:
        if words := county_words(c):
            counties.setdefault(words, "served campus county")
    for text, origin in ((row["name"], "plant name"), (facts.get("serves_text"), "plant serves_text")):
        for c in counties_in(text if isinstance(text, str) else None, county_list):
            counties.setdefault(c, origin)
    for d in facts.get("dockets") or []:
        agency = d.get("agency") if isinstance(d, dict) else None
        for c in counties_in(agency, county_list):
            counties.setdefault(c, "plant docket agency")
    names: list[tuple[str | None, str]] = [(facts.get("operator"), "plant operator"),
                                           (facts.get("owner"), "plant owner")]
    for p in facts.get("parties") or []:
        # Builders and power suppliers only: a tenant or the grid utility is not who holds the plant's permit.
        # 'name' can be a ticker (FRMI); the note's first part is then the company name.
        if isinstance(p, dict) and p.get("role") in PARTY_ROLES:
            names.append((p.get("name"), "plant party"))
            note = p.get("note")
            if isinstance(note, str) and "|" in note:  # 'Fermi Inc. (Fermi America)|$431m convertible notes'
                names.append((note.split("|")[0], "plant party note"))
    if isinstance(eid := facts.get("operator_entity_id"), str) and eid in entity_names:
        names.append((entity_names[eid], "operator entity"))
    permit = row.get("prior_permit")
    if isinstance(permit, dict):
        names.append((permit.get("applicant"), "air permit applicant (earlier record)"))
    place = {w for c in counties for w in c} | set(tokens(row["state"]))
    phrases: list[tuple[tuple[str, ...], str]] = []
    for text, origin in names:
        if not isinstance(text, str):
            continue
        for words in name_phrases(text):
            if not set(words) <= place and all(words != w for w, _ in phrases):
                phrases.append((words, origin))
    numbers: dict[str, str] = {}
    if isinstance(permit, dict):
        for n in [permit.get("number"), *(permit.get("ids") or [])]:
            if isinstance(n, str) and (k := norm_number(n)):
                numbers.setdefault(k, "plant air_permit fact")
    for d in facts.get("dockets") or []:
        if not isinstance(d, dict):
            continue
        text = " ".join(_texts(d))
        if PERMIT_WORDS.search(text):
            for n in PERMIT_NO.findall(text):
                numbers.setdefault(norm_number(n), "plant docket text")
    tech = facts.get("technology")
    return PlantProfile(row["id"], row["name"], row["state"], tech if isinstance(tech, str) else None, counties,
                        phrases, tokens(row["name"]), numbers)


def name_phrases(text: str) -> list[tuple[str, ...]]:
    """Name phrases of an operator text. Only parts written as names count: 'Crusoe / Lancium (Blue Owl & Primary
    Digital JV owners)' gives crusoe, lancium; a lower-case note ('affiliate GN DC1 LLC per state filings', 'build')
    or a figure ('1.12bn Texas Energy Fund loan') gives nothing. A three-letter acronym written in capitals (CPV,
    LES, PSO) counts as a name."""
    out = []
    for part in split_names(text):
        if not re.match(r"[A-Z]", part):
            continue
        words = phrase(part)
        if words is None and re.fullmatch(r"[A-Z]{3}", part.strip()) and part.strip().lower() not in LEGAL:
            words = (part.strip().lower(),)
        if words and not (len(words) == 1 and words[0] in WEAK_SINGLE):
            out.append(words)
    return out


# ---------------------------------------------------------------- the rule
def project_phrase(name: str) -> tuple[str, ...] | None:
    """A project name as distinctive words: two or more ('GW RANCH ENERGY CENTER' -> gw ranch), or one after
    'Project' ('PROJECT LLANO' -> project llano). A single word such as a town ('ABILENE POWER STATION') is not
    a project name; `same_name` covers a record named exactly like the plant."""
    words = phrase(name)
    raw = tuple(t for t in tokens(name) if t not in LEGAL)
    if not words:
        return None
    if len(words) >= 2:
        return words
    if len(raw) >= 2 and raw[0] == "project" and raw[1] == words[0]:
        return raw[:2]
    return None


def same_name(a: str, b: str) -> bool:
    """'Orient Energy Center' is 'ORIENT ENERGY CENTER' (legal-form words ignored)."""
    ta, tb = [t for t in tokens(a) if t not in LEGAL], [t for t in tokens(b) if t not in LEGAL]
    return bool(ta) and ta == tb and phrase(a) is not None


def evaluate(f: Facility, p: PlantProfile) -> tuple[dict[str, Any], list[str]]:
    texts = [tokens(n) for n in [*f.names, f.name] if n]
    name = next(((w, o) for w, o in p.phrases if any(contains(t, w) for t in texts)), None)
    fcounty = county_words(f.county)
    county = fcounty in p.counties if fcounty else False
    numbers = sorted({i for permit in f.permits for i in permit.ids if norm_number(i) in p.numbers})
    project = next((w for w in map(project_phrase, [f.name, *f.aliases]) if w and contains(p.name_tokens, w)), None)
    if project is None and same_name(f.name, p.name):
        project = tuple(t for t in tokens(f.name) if t not in LEGAL)
    ev: dict[str, Any] = {"method": METHOD_VERSION, "facility": f.key, "facility_name": f.name, "agency": f.agency}
    reasons = []
    if name:
        ev["name"] = {"phrase": " ".join(name[0]), "from": name[1]}
    else:
        reasons.append("no operator/applicant name of the plant on the record")
    if f.generation:
        ev["generation"] = f.generation
    else:
        reasons.append("record shows no power generation")
    if county:
        ev["county"] = {"county": f.county, "from": p.counties[fcounty]}
    if numbers:
        ev["permit_numbers"] = {"numbers": numbers, "from": sorted({p.numbers[norm_number(n)] for n in numbers})}
    if project:
        ev["project_name"] = " ".join(project)
    if fcounty and p.counties and not county:
        reasons.append(f"county {f.county} is not the plant's ({', '.join(' '.join(c) for c in p.counties)})")
    elif not (county or numbers or project):
        reasons.append("no location agreement (county, cited permit number or project name)")
    return ev, reasons


def score(ev: dict[str, Any]) -> int:
    return sum(k in ev for k in ("name", "county", "permit_numbers", "project_name"))


def match_all(facilities: Iterable[Facility], plants: list[PlantProfile]) -> list[Match]:
    """One Match per facility: the plant it is tied to, or None with the reasons (best candidate's). A plant
    takes several facilities only when each brings a cited permit number or the project name: an operator with
    several permitted sites in the plant's county (a utility's existing stations) is not guessed at."""
    out = _match_each(facilities, plants)
    by_plant: dict[str, list[Match]] = {}
    for m in out:
        if m.plant is not None:
            by_plant.setdefault(m.plant.id, []).append(m)
    for plant_id, ms in by_plant.items():
        weak = [m for m in ms if not ({"permit_numbers", "project_name"} & set(m.evidence))]
        if len(weak) > 1:
            keys = ", ".join(m.facility.key for m in weak)
            for m in weak:
                m.reasons = [f"{len(weak)} facilities of the operator in the plant's county ({keys}) and no permit "
                             "number or project name to tell which is the plant's"]
                m.evidence = dict(m.evidence, closest_plant=plant_id)
                m.plant = None
    return out


def candidate(m: Match) -> bool:
    """A record worth listing in the run report: tied, or bearing the plant's name with power generation, or a
    permit number / project name of the plant."""
    ev = m.evidence
    strong = bool({"permit_numbers", "project_name"} & set(ev))
    return m.plant is not None or ("name" in ev and "generation" in ev) or strong


def _match_each(facilities: Iterable[Facility], plants: list[PlantProfile]) -> list[Match]:
    out = []
    for f in facilities:
        same = [p for p in plants if p.state == f.state]
        results = [(p, *evaluate(f, p)) for p in same]
        passing = [(p, ev) for p, ev, reasons in results if not reasons]
        if passing:
            top = max(score(ev) for _, ev in passing)
            best = [(p, ev) for p, ev in passing if score(ev) == top]
            if len(best) == 1:
                others = sorted(p.id for p, _ in passing if p is not best[0][0])
                out.append(Match(f, best[0][0], dict(best[0][1], **({"also_passed": others} if others else {})), []))
                continue
            ev = dict(best[0][1], tied=[p.id for p, _ in best])
            tied = ", ".join(p.id for p, _ in best)
            out.append(Match(f, None, ev, [f"equal evidence for {len(best)} plants: {tied}"]))
            continue
        relevant = [(p, ev, reasons) for p, ev, reasons in results if score(ev) > 0]
        if relevant:
            p, ev, reasons = max(relevant, key=lambda x: (score(x[1]), x[0].id))
            out.append(Match(f, None, dict(ev, closest_plant=p.id), reasons))
        else:
            out.append(Match(f, None, {"method": METHOD_VERSION, "facility": f.key, "facility_name": f.name},
                             ["no plant of ours shares a name, county, permit number or project name"]))
    return out


def report_row(m: Match) -> dict[str, Any]:
    f = m.facility
    return {
        "system": f.system, "facility": f.key, "name": f.name, "county": f.county, "names": f.names[:6],
        "permits": [{"number": p.number, "ids": p.ids, "type": p.type, "status": p.status} for p in f.permits],
        "url": f.url, "plant": m.plant.id if m.plant else None, "reasons": m.reasons, "evidence": m.evidence,
    }
