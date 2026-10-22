"""The researcher's worklist: what is still not disclosed on watched and financed subjects, and how to search it.

Subjects: every financed site (the `site` table: sites with debt chains), every instrument on one, and every
site or instrument on any firm's watchlist (watched first). Gaps, all read from what Marden already shows:

- `project_file`  the site's project-file checklist items in state 'n/d' (read model `project_file`, the
                  latest snapshot; 'n/p' items are not public by nature and never searched);
- `cushion`       the cushion model's missing inputs on the site (no outside date, no rent);
- `fact`          an instrument with no published size, pricing or maturity fact (`fact_current`).

`PLANS` says, per gap item, how a public search can find it: EDGAR full-text search terms (each query is one
subject phrase AND one term, both exact phrases), FERC eLibrary text search, an existing collector that
covers it, or why nothing Marden may read covers it (county permits, liens, inspections: no collector).
Subject phrases come from the site's own name and its SPV tracking names (or the instrument's issuer); a
one-word phrase ("Stingray") is searched only inside the filings of the subject's chain parties (their CIKs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from marden.core.db import Conn

ENTITY_SUFFIX = re.compile(r"\b(?:LLC|L\.L\.C\.|Inc\.?|LP|L\.P\.|Ltd\.?|Pty|Corp\.?|Company|Trust)\b")
QUOTED = re.compile(r"[\"“]([^\"”]+)[\"”]")
PAREN = re.compile(r"\(([^)]*)\)")
DESCRIPTORS = frozenset({"ABS", "GPU"})  # 'DataBank data center ABS', 'Colossus 2 GPU loans': not the name
MAX_PHRASES = 2


@dataclass(frozen=True)
class Plan:
    weight: int  # higher first
    edgar: tuple[str, ...] = ()  # EDGAR full-text search terms (exact phrases)
    ferc: bool = False  # FERC eLibrary text search on the subject's phrases
    collector: str | None = None  # an existing collector that covers it
    why_not: str | None = None  # nothing Marden may search covers it


NO_COUNTY = "county and city records (permits, inspections, liens, agendas) have no Marden collector yet"
NO_COURT = "court dockets have no Marden collector yet (PACER needs an account)"
PLANS: dict[str, Plan] = {
    "Lease: Outside date": Plan(10, ('"outside date"',)),
    "Lease: Commencement": Plan(9, ('"commencement date"',)),
    "Lease: Rent": Plan(8, ('"base rent"',)),
    "Lease: Term": Plan(6, ('"initial term"',)),
    "Lease: Termination rights": Plan(7, ('"termination"',)),
    "Lease: Force majeure": Plan(7, ('"force majeure"',)),
    "Lease: Escalators": Plan(3, ('"escalation"',)),
    "Lease: Residual value guarantees": Plan(3, ('"residual value guarantee"',)),
    "Structure: Sizing covenants": Plan(6, ('"debt service coverage"',)),
    "Structure: Reserves": Plan(4, ('"reserve account"',)),
    "Structure: Completion guarantees": Plan(5, ('"completion guarantee"',)),
    "Structure: Hedges": Plan(3, ('"interest rate swap"',)),
    "Structure: Intercreditor terms": Plan(3, ('"intercreditor"',)),
    "Structure: Cash waterfall": Plan(3, ('"waterfall"',)),
    "Structure: Sponsor support": Plan(4, ('"equity contribution"',)),
    "Structure: Rating triggers": Plan(2, ('"rating agency"',)),
    "Draws: Amount drawn vs committed per facility": Plan(6, ('"outstanding"',)),
    "Draws: Equity contributed": Plan(4, ('"equity contribution"',)),
    "Budget: Announced project cost": Plan(5, ('"billion"',)),
    "Parties: General contractor and major subcontractors": Plan(4, ('"general contractor"',)),
    "Parties: Lenders, agents and trustees": Plan(5, ('"administrative agent"',)),
    "Parties: Guarantors": Plan(4, ('"guarantor"',)),
    "Parties: Developer": Plan(4, ('"developer"',)),
    "Parties: Landlord SPV and parents": Plan(3, ('"landlord"',)),
    "Parties: Utility and grid operator": Plan(5, (), ferc=True),
    "Parties: Equipment makers": Plan(2, ('"turbine"',)),
    "Power: Interconnection and energy supply agreements": Plan(8, ('"interconnection"',), ferc=True),
    "Power: On-site generation": Plan(6, ('"behind-the-meter"',), ferc=True),
    "Power: Fuel supply and pipeline laterals": Plan(6, (), ferc=True),
    "Equipment: GPU deliveries": Plan(3, ('"GPUs"',)),
    "Equipment: Turbine, transformer and switchgear orders and delivery dates": Plan(3, ('"transformers"',)),
    "Permits: Air permits": Plan(5, collector="air_permits"),
    "Permits: Environmental permits": Plan(2, (), ferc=True),
    "Permits: Building permits": Plan(1, why_not=NO_COUNTY),
    "Permits: Water permits": Plan(1, why_not=NO_COUNTY),
    "Permits: Zoning": Plan(1, why_not=NO_COUNTY),
    "Permits: Land ownership and title": Plan(1, why_not=NO_COUNTY),
    "Construction: Certificates of occupancy": Plan(1, why_not=NO_COUNTY),
    "Construction: Inspections passed": Plan(1, why_not=NO_COUNTY),
    "Construction: Building footprints": Plan(1, why_not=NO_COUNTY),
    "Construction: Satellite change": Plan(1, collector="sentinel2"),
    "Construction: Milestones": Plan(4, ('"construction"',)),
    "Contractor: Mechanic's liens": Plan(1, why_not=NO_COUNTY),
    "Contractor: Lien releases": Plan(1, why_not=NO_COUNTY),
    "Contractor: Contractor disputes and lawsuits": Plan(1, why_not=NO_COURT),
    "Contractor: Change of contractor": Plan(2, ('"general contractor"',)),
    "Contractor: Workforce signals (hiring, layoffs, WARN notices)": Plan(
        1, why_not="state WARN notices have no Marden collector yet"),
    "Opposition: Agendas, petitions, lawsuits and moratoria": Plan(1, why_not=NO_COUNTY),
    "Opposition: State legislation": Plan(1, why_not="state legislatures have no Marden collector yet"),
    "Budget: Capital investment committed in tax abatement agreements": Plan(1, why_not=NO_COUNTY),
    "Budget: Declared construction value per building permit": Plan(1, why_not=NO_COUNTY),
    "Budget: Cost per MW vs peers": Plan(1, why_not="computed from other figures, not disclosed"),
    "Budget: Equipment cost signals": Plan(1, why_not="no public source states it"),
    # instrument fact gaps
    "Instrument: size": Plan(9, ('"aggregate principal amount"', '"commitments"')),
    "Instrument: pricing": Plan(8, ('"applicable margin"', '"interest rate"')),
    "Instrument: maturity": Plan(8, ('"maturity date"',)),
}
CUSHION_ITEMS = {"outside_date": "Lease: Outside date", "rent": "Lease: Rent"}
INSTRUMENT_FIELDS = {
    "Instrument: size": ("amount_usd", "commitment_usd"),
    "Instrument: pricing": ("coupon", "coupon_spread_bp", "coupon_rate_pct"),
    "Instrument: maturity": ("maturity",),
}


@dataclass
class Subject:
    type: str  # 'site' | 'instrument'
    id: str
    name: str
    phrases: list[str]
    ciks: list[str]
    watched: bool = False
    us: bool = True  # FERC is searched for US subjects only
    context: list[str] = field(default_factory=list)  # its parties' names and county: tell its namesakes apart


@dataclass
class Gap:
    subject: Subject
    item: str
    origin: str  # 'project_file' | 'cushion' | 'fact'
    detail: str | None = None
    plan: Plan = field(default_factory=lambda: Plan(0))

    def doc(self) -> dict[str, Any]:
        return {"subject": {"type": self.subject.type, "id": self.subject.id}, "item": self.item,
                "origin": self.origin, "detail": self.detail}


def _clean(text: str) -> str:
    """Whitespace folded, a qualifier after a comma dropped ('Polaris Forge 1, Buildings 2 and 3' ->
    'Polaris Forge 1') and trailing descriptive words dropped ('CloudHQ campuses' -> 'CloudHQ')."""
    text = re.sub(r"\s+", " ", text.replace("\u2019", "'"))
    text = re.sub(r",\s*(?=(?:LLC|L\.L\.C\.|Inc|LP|L\.P\.|Ltd)\b)", " ", text)  # 'Co. VII, LLC' stays whole
    text = text.split(",", 1)[0].strip(" ;-")
    words = text.split()
    while words and (words[-1][:1].islower() or words[-1] in DESCRIPTORS):
        words.pop()
    if words and words[0].lower() == "the":
        words.pop(0)
    return " ".join(words)


def name_phrases(name: str) -> list[str]:
    """'Project Jupiter (Stargate New Mexico)' -> ['Project Jupiter', 'Stargate New Mexico']; a parenthesis
    that is one word once cleaned ('Childress (Microsoft)', '(OpenAI servers)') names a party or the
    collateral, not the project, and is dropped."""
    quoted = [_clean(q) for q in QUOTED.findall(name)]
    parens = [p for p in (_clean(p) for p in PAREN.findall(name)) if len(p.split()) > 1]
    main = _clean(PAREN.sub(" ", QUOTED.sub(" ", name)))
    out = [p for p in (main, *parens, *quoted) if p and p.lower() != "project"]
    return list(dict.fromkeys(out))


def entity_phrases(names: list[str]) -> list[str]:
    """SPV names that are one entity's exact name ('APLD ComputeCo 2 LLC'), not a list or a description."""
    out = []
    for raw in names:
        n = _clean(PAREN.sub(" ", raw))
        if ENTITY_SUFFIX.search(n) and not re.search(r"\s(?:and|&)\s|/", n) and len(n) <= 60:
            out.append(n)
    return list(dict.fromkeys(out))


def is_entity_name(phrase: str) -> bool:
    """An SPV's or company's exact legal name: specific enough to stand on its own."""
    return bool(ENTITY_SUFFIX.search(phrase))


def searchable(phrase: str, ciks: list[str]) -> bool:
    """A one-word phrase is searched only within the chain parties' filings."""
    return len(phrase.split()) > 1 or bool(ciks)


def _cik(raw: Any) -> str | None:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits.zfill(10) if digits and int(digits) > 0 else None


def watched_refs(conn: Conn) -> set[tuple[str, str]]:
    return {(r["kind"], r["ref_id"]) for r in conn.execute("SELECT DISTINCT kind, ref_id FROM watchlist_item")}


def site_subjects(conn: Conn, watched: set[tuple[str, str]]) -> list[Subject]:
    rows = conn.execute(
        """SELECT s.slug, s.name, s.state, s.county, s.tracking_names,
                  coalesce(array_agg(DISTINCT e.cik) FILTER (WHERE e.cik IS NOT NULL
                           AND c.role IN ('developer', 'tenant')), '{}') AS ciks,
                  coalesce(array_agg(DISTINCT c.label) FILTER (WHERE c.role IN ('developer', 'tenant', 'end_user')),
                           '{}') AS parties
           FROM site s LEFT JOIN chain_party c ON c.site_slug = s.slug LEFT JOIN entity e ON e.id = c.entity_id
           GROUP BY s.slug, s.name, s.state, s.county, s.tracking_names ORDER BY s.slug""").fetchall()
    out = []
    for r in rows:
        ciks = sorted({c for c in (_cik(x) for x in r["ciks"]) if c})
        phrases = [p for p in [*name_phrases(r["name"]), *entity_phrases(list(r["tracking_names"] or []))]
                   if searchable(p, ciks)]
        us = not re.search(r"australia|canada|united kingdom|norway", r["state"] or "", re.I)
        context = [p for p in (_clean(re.split(r"[(;]", lbl)[0]) for lbl in r["parties"] or []) if len(p) > 2]
        context += [c for c in [r["county"] and f"{r['county']} County"] if c]
        context += entity_phrases(list(r["tracking_names"] or []))
        out.append(Subject("site", r["slug"], r["name"], phrases[:MAX_PHRASES], ciks, ("site", r["slug"]) in watched,
                           us, list(dict.fromkeys(context))))
    return out


def instrument_subjects(conn: Conn, watched: set[tuple[str, str]]) -> list[Subject]:
    watched_ids = [i for k, i in watched if k == "instrument"]
    rows = conn.execute(
        """SELECT i.id, i.name, i.issuer_name, e.cik AS issuer_cik, p.cik AS parent_cik
           FROM instrument i LEFT JOIN entity e ON e.id = i.issuer_entity_id LEFT JOIN entity p ON p.id = e.parent_id
           WHERE i.site_slug IS NOT NULL OR i.id = ANY(%s) ORDER BY i.id""", (watched_ids,)).fetchall()
    out = []
    for r in rows:
        ciks = sorted({c for c in (_cik(r["issuer_cik"]), _cik(r["parent_cik"])) if c})
        phrases = [p for p in entity_phrases([r["issuer_name"]]) if searchable(p, ciks)]
        out.append(Subject("instrument", r["id"], r["name"], phrases[:MAX_PHRASES], ciks,
                           ("instrument", r["id"]) in watched))
    return out


def checklist_gaps(conn: Conn, subjects: dict[str, Subject]) -> list[Gap]:
    rows = conn.execute(
        """SELECT DISTINCT ON (key) key, doc FROM read_model WHERE view = 'project_file' AND key = ANY(%s)
           ORDER BY key, as_of DESC""", (list(subjects),)).fetchall()
    out = []
    for r in rows:
        for it in (r["doc"] or {}).get("checklist") or []:
            if isinstance(it, dict) and it.get("state") == "n/d" and it.get("area") and it.get("item"):
                out.append(Gap(subjects[r["key"]], f"{it['area']}: {it['item']}", "project_file",
                               it.get("researchNote")))
    return out


def cushion_gaps(conn: Conn, subjects: dict[str, Subject]) -> list[Gap]:
    rows = conn.execute(
        """SELECT DISTINCT ON (subject_id) subject_id, output->'missing' AS missing FROM model_output
           WHERE model = 'cushion' AND subject_type = 'site' AND subject_id = ANY(%s)
           ORDER BY subject_id, as_of DESC, created_at DESC""", (list(subjects),)).fetchall()
    out = []
    for r in rows:
        for text in r["missing"] or []:
            key = str(text).split(":", 1)[0].strip()
            if key in CUSHION_ITEMS:
                out.append(Gap(subjects[r["subject_id"]], CUSHION_ITEMS[key], "cushion", str(text)[:300]))
    return out


def instrument_gaps(conn: Conn, subjects: dict[str, Subject]) -> list[Gap]:
    have = {(r["subject_id"], r["field"]) for r in conn.execute(
        """SELECT DISTINCT subject_id, field FROM fact_current WHERE subject_type = 'instrument'
           AND subject_id = ANY(%s)""", (list(subjects),))}
    out = []
    for sid, subject in subjects.items():
        for item, fields in INSTRUMENT_FIELDS.items():
            if not any((sid, f) in have for f in fields):
                out.append(Gap(subject, item, "fact", f"no published {' / '.join(fields)} fact"))
    return out


def collect(conn: Conn) -> list[Gap]:
    """Every gap, one per (subject, item), watched subjects and heavier items first."""
    watched = watched_refs(conn)
    sites = {s.id: s for s in site_subjects(conn, watched)}
    instruments = {s.id: s for s in instrument_subjects(conn, watched)}
    gaps: dict[tuple[str, str, str], Gap] = {}
    for g in [*checklist_gaps(conn, sites), *cushion_gaps(conn, sites), *instrument_gaps(conn, instruments)]:
        g.plan = PLANS.get(g.item, Plan(1, why_not="no search is defined for this item yet"))
        key = (g.subject.type, g.subject.id, g.item)
        if key in gaps:  # the checklist and the cushion model can name the same gap
            gaps[key].detail = gaps[key].detail or g.detail
            continue
        gaps[key] = g
    return sorted(gaps.values(), key=lambda g: (not g.subject.watched, -g.plan.weight, g.subject.type, g.subject.id,
                                                g.item))
