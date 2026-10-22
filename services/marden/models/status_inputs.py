"""Inputs of the status model for one as-of day: every published statement about each financed site, its
chain parties, its instruments and (through approved links) its campuses and plants.

Reads go through `marden.models.pit` (point in time, publication lags, the unlicensed-price switch). One
extra rule for backtests: a dossier note in the seed archive (debt notes, rating text, lease terms ...) is
dated by its record, but the note itself was written when the archive was compiled, so it is public only
from the archive's own date (`document.doc_date`), not from the record date. Dated items (risk evidence,
schedule changes, ready-for-service statements) are public on their own date. Tag M rows (the archive's
legacy judgments and other models' estimates) are never inputs: only filed (F) and reported (R)
statements can trip a status rule. The Marden price is the one model input, read from the price model's
`model_output`; for a fixed coupon it comes with its rate-neutral reading (`status_credit`: the spread move
since the reference day, on the reference day's Treasury curve), which rule L2 reads instead of the price.

Authorizations (status v0.4): the permits and queue positions on file for the site and its approved plants
on the day (`air_permits` on the site and plants, the plants' `queue_ids` with each queue position's
`status_class`), read point in time like every fact. With the permit and queue events they tell whether a
withdrawn permit or queue position was replaced (`status_power`).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit, status_credit
from marden.models.price_curve import CurveBook, load_curves

ARCHIVE_SOURCES = ("seed_marden_data",)
DATED_ITEM_FIELDS = frozenset({"risk_evidence", "schedule_change", "rfs_statement"})
SITE_FIELDS = (
    "risk_evidence", "schedule_change", "rfs_statement", "tenant_rating", "lease_termination", "lease_tenant",
    "leases", "structure_term", "credit_support", "debt_facility", "tranches", "debt_items", "power_contract",
    "power_detail", "power_status", "power_supply", "onsite_generation", "expected_energization",
    "original_energization", "place",
)
ENTITY_FIELDS = ("funding_fact",)
INSTRUMENT_FIELDS = ("research_terms", "status")
AUTH_FIELDS = ("air_permits",)  # site and plant: every matched permit {number, ids, status, ...}
QUEUE_FIELDS = ("queue_ids",)  # plant: its interconnection queue positions (ISO queue ids)
QUEUE_STATUS = "status_class"  # queue_project: active | under_construction | in_service | suspended | withdrawn ...
EXCLUDED_EVENT_KINDS = ("catalyst",)  # scheduled future steps, not things that happened
PRICE_MODEL = "price"
CURVES_SINCE = date(2025, 1, 1)  # the Treasury curve archive starts 2025-01-02 (as the price model's)
ROLES = ("end_user", "tenant", "developer", "lenders")
GENERIC_WORDS = frozenset({
    "project", "projects", "stargate", "data", "center", "centre", "campus", "campuses", "phase", "phases",
    "building", "buildings", "site", "sites", "loans", "loan", "gpu", "gpus", "abs", "ddtl", "the", "and", "of",
    "for", "with", "servers", "server", "ai", "llc", "inc", "i", "ii", "iii", "iv",
})

# Which keys of a JSON value hold the statement, per field (other keys are ids, URLs, amounts).
TEXT_KEYS: dict[str, tuple[str, ...]] = {
    "risk_evidence": ("headline", "summary"),
    "schedule_change": ("what",),
    "rfs_statement": ("text",),
    "debt_facility": ("notes", "status"),
    "tranches": ("note", "terms"),
    "debt_items": ("note",),
    "leases": ("termination_rights", "quote"),
    "structure_term": ("text",),
    "credit_support": ("covers",),
    "funding_fact": ("label", "value", "note"),
    "research_terms": ("note", "terms"),
}


@dataclass(frozen=True)
class Item:
    """One published statement: a fact (or one element of a list-valued fact) or an event title."""

    text: str
    day: date  # the day it became public
    tag: str
    source_name: str
    source_url: str | None
    origin: str  # 'fact:<id>' | 'event:<id>'
    subject: str  # '<subject_type>:<id>'
    field: str  # fact field, or 'event:<kind>'
    value: Any = None
    generic: bool = False  # a news item filed under several sites: counts only if it names this site

    def sourced(self, limit: int = 220) -> dict[str, Any]:
        text = self.text if len(self.text) <= limit else self.text[: limit - 1].rstrip() + "…"
        out: dict[str, Any] = {"value": text, "tag": self.tag, "asOf": self.day.isoformat(),
                               "sourceName": self.source_name}
        if self.source_url:
            out["sourceUrl"] = self.source_url
        return out


@dataclass(frozen=True)
class Authorization:
    """A permit or queue position on file on the as-of day, as its record states it (status v0.4)."""

    subject: str  # 'site:<slug>' | 'plant:<id>'
    kind: str  # 'permit' | 'queue'
    key: str  # the permit number or queue id
    ids: tuple[str, ...]  # every number the record gives (permit, PSD / GHG ids, project) or the queue id
    status: str  # the record's status ('issued', 'pending', 'withdrawn'; a queue's status_class)
    day: date  # the day it became public
    source_name: str
    origin: str  # 'fact:<id>'


@dataclass
class Party:
    role: str
    label: str
    entity_id: str | None
    names: list[str]


@dataclass
class Price:
    instrument_id: str
    value: float
    as_of: date  # the price output's day
    method_version: str | None
    source_url: str | None
    coupon_type: str | None = None  # 'fixed' | 'floating' (the output's basis.terms)
    credit: status_credit.CreditTest | None = None  # fixed coupon: the rate-neutral reading
    credit_missing: str | None = None  # fixed coupon without a rate-neutral reading: what is missing


@dataclass
class SiteInputs:
    slug: str
    name: str
    keywords: list[str]
    parties: dict[str, Party]
    instruments: list[str]
    items: list[Item] = field(default_factory=list)  # site facts and events, campus / plant events
    instrument_items: list[Item] = field(default_factory=list)
    prices: list[Price] = field(default_factory=list)
    links: dict[str, list[str]] = field(default_factory=dict)  # approved campuses and their plants
    authorizations: list[Authorization] = field(default_factory=list)  # site and plant permits, queue positions


@dataclass
class Inputs:
    as_of: date
    sites: dict[str, SiteInputs]
    entity_items: dict[str, list[Item]]
    approved_links: int
    proposed_links: int


def _strings(value: Any, keys: tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v, keys)]
    if isinstance(value, dict):
        picked = [value.get(k) for k in keys] if keys else list(value.values())
        return [s for v in picked if v is not None for s in _strings(v, None)]
    return []


def _texts(row: Row) -> list[tuple[str, Any]]:
    """(statement text, the element it came from) for a fact row; list values give one per element."""
    value, keys = row["value"], TEXT_KEYS.get(row["field"])
    if isinstance(value, list) and keys:
        out = []
        for element in value:
            text = " ".join(_strings(element, keys)).strip()
            if text:
                out.append((text, element))
        return out
    if isinstance(value, dict) and keys:
        text = " ".join(_strings(value, keys)).strip()
        return [(text, value)] if text else []
    if isinstance(value, str | int | float) and not isinstance(value, bool):
        return [(str(value), value)]
    return []


def _archive_days(conn: Conn, rows: list[Row]) -> dict[int, date]:
    ids = sorted({r["document_id"] for r in rows if r.get("document_id")})
    if not ids:
        return {}
    found = conn.execute(
        "SELECT id, doc_date, fetched_at FROM document WHERE id = ANY(%s) AND source_id = ANY(%s)",
        (ids, list(ARCHIVE_SOURCES)),
    ).fetchall()
    return {r["id"]: r["doc_date"] or r["fetched_at"].date() for r in found}


def fact_items(conn: Conn, as_of: date, subject_type: str, ids: list[str], fields: tuple[str, ...]) -> list[Item]:
    rows = [r for r in pit.fact_rows(conn, as_of, subject_type, ids, fields) if r["tag"] in ("F", "R")]
    archive = _archive_days(conn, rows)
    items: list[Item] = []
    for r in rows:
        day = pit.public_date(r)
        if r["field"] not in DATED_ITEM_FIELDS and r.get("document_id") in archive:
            day = max(day, archive[r["document_id"]])
        if day > as_of:
            continue
        for text, element in _texts(r):
            items.append(Item(text, day, r["tag"], r["source_name"], r.get("source_url"), f"fact:{r['id']}",
                              f"{subject_type}:{r['subject_id']}", r["field"], element))
    return items


def event_items(conn: Conn, as_of: date, subject_type: str, ids: list[str]) -> list[Item]:
    if not ids:
        return []
    items = []
    for r in pit.event_rows(conn, as_of, subject_type, ids):
        if r["kind"] in EXCLUDED_EVENT_KINDS or r["event_date"] > as_of:
            continue
        items.append(Item(r["title"], r["event_date"], r["tag"], r["source_name"], r.get("source_url"),
                          f"event:{r['id']}", f"{subject_type}:{r['subject_id']}", f"event:{r['kind']}",
                          {"kind": r["kind"], "fact_id": r.get("fact_id")}))
    return items


def _keywords(site: Row, party_names: list[str], place: str | None) -> list[str]:
    taken = {n.lower() for n in party_names}
    words: list[str] = []
    name = site["name"]
    city = (place or "").split(",")[0]
    for chunk in [re.sub(r"\(.*?\)", " ", name), *re.findall(r"\((.*?)\)", name), city, site["county"] or ""]:
        for word in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'\-]+", chunk):
            if len(word) >= 4 and word.lower() not in GENERIC_WORDS and word.lower() not in taken:
                words.append(word)
    for tracked in site["tracking_names"] or []:
        first = re.split(r"[ ,/]", tracked.strip())[0]
        if len(first) >= 4 and first.lower() not in GENERIC_WORDS and first.lower() not in taken:
            words.append(first)
    return sorted(set(words))


def _names(label: str, entity: Row | None) -> list[str]:
    names = {re.sub(r"\s*\(.*?\)\s*", " ", label).strip()}
    names.update(p.strip() for p in re.split(r"\s*(?:/|,|;| and )\s*", re.sub(r"\(.*?\)", "", label)) if p.strip())
    if entity:
        names.add(entity["name"])
        names.update(entity["aliases"] or [])
        names.add(re.sub(r",?\s+(Inc\.?|Corp(?:oration)?\.?|Ltd\.?|LLC|plc|Co\.?)$", "", entity["name"]).strip())
    return sorted(n for n in names if len(n) >= 3 and n.lower() not in GENERIC_WORDS)


def _prices(conn: Conn, as_of: date, instrument_ids: list[str]) -> dict[str, Price]:
    if not instrument_ids:
        return {}
    rows = conn.execute(
        """SELECT DISTINCT ON (subject_id) subject_id, as_of, version, output FROM model_output
            WHERE model = %s AND subject_type = 'instrument' AND subject_id = ANY(%s) AND as_of <= %s
            ORDER BY subject_id, as_of DESC, created_at DESC, id DESC""",
        (PRICE_MODEL, instrument_ids, as_of),
    ).fetchall()
    pit.note_outputs(PRICE_MODEL, "instrument", rows)
    return prices_from_outputs(conn, as_of, {r["subject_id"]: (r["as_of"], r["output"], r["version"]) for r in rows})


def first_spreads(conn: Conn, as_of: date, instrument_ids: list[str]) -> dict[str, tuple[date, float]]:
    """(day, spreadBp) of each instrument's first stored Marden price with a spread, on or before `as_of`:
    the rate-neutral test's reference when the instrument has no issue date."""
    if not instrument_ids:
        return {}
    rows = conn.execute(
        """SELECT DISTINCT ON (subject_id) subject_id, as_of, (output->>'spreadBp')::float AS spread FROM model_output
            WHERE model = %s AND subject_type = 'instrument' AND subject_id = ANY(%s) AND as_of <= %s
              AND jsonb_typeof(output->'spreadBp') = 'number' AND jsonb_typeof(output->'price') = 'number'
            ORDER BY subject_id, as_of, created_at DESC, id DESC""",
        (PRICE_MODEL, instrument_ids, as_of),
    ).fetchall()
    pit.note_outputs(PRICE_MODEL, "instrument", rows)
    return {r["subject_id"]: (r["as_of"], float(r["spread"])) for r in rows}


def prices_from_outputs(conn: Conn, as_of: date, outputs: dict[str, tuple[date, Any, str | None]],
                        book: CurveBook | None = None) -> dict[str, Price]:
    """Price inputs from price-model outputs {instrument: (output day, output, version)}: the Marden price
    and, for a fixed coupon, its rate-neutral reading (status_credit). The Treasury curves are read point
    in time as of `as_of` (a curve is public on its date)."""
    out: dict[str, Price] = {}
    fixed = [i for i, (_, o, _) in outputs.items() if status_credit.coupon_type(o) == "fixed"]
    firsts = first_spreads(conn, as_of, fixed)
    if fixed and book is None:
        book = load_curves(conn, as_of, since=CURVES_SINCE)
    for iid, (day, output, version) in sorted(outputs.items()):
        value, method, url = price_value(output)
        if value is None:
            continue
        price = Price(iid, value, day, method or version, url, status_credit.coupon_type(output))
        if price.coupon_type == "fixed":
            assert book is not None
            price.credit, price.credit_missing = status_credit.credit_test(iid, output, day, book, firsts.get(iid))
        out[iid] = price
    return out


def price_value(output: Any) -> tuple[float | None, str | None, str | None]:
    """The Marden price in a price-model output: `mardenPrice` / `price` (Sourced<number>) or `value`."""
    if not isinstance(output, dict):
        return None, None, None
    for key in ("mardenPrice", "price"):
        sourced = output.get(key)
        if isinstance(sourced, dict) and isinstance(sourced.get("value"), int | float):
            return float(sourced["value"]), sourced.get("methodVersion"), sourced.get("sourceUrl")
    if isinstance(output.get("value"), int | float):
        return float(output["value"]), output.get("methodVersion"), None
    plain = output.get("price")  # the price model's own output: {price: number, methodVersion, sourceUrl}
    if isinstance(plain, int | float) and not isinstance(plain, bool):
        return float(plain), output.get("methodVersion"), output.get("sourceUrl")
    return None, None, None


def _current(rows: list[Row]) -> dict[tuple[str, str], Row]:
    """(subject_id, field) -> the row a reader takes on the day (`pit.current_of`), tags F and R only."""
    by_key: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for r in rows:
        if r["tag"] in ("F", "R"):
            by_key[(r["subject_id"], r["field"])].append(r)
    return {k: row for k, rs in by_key.items() if (row := pit.current_of(rs)) is not None}


def authorizations(conn: Conn, as_of: date, slugs: list[str], plant_ids: list[str]) -> list[Authorization]:
    """The permits and queue positions on file on `as_of` for these sites and plants (point in time)."""
    rows = list(_current(pit.fact_rows(conn, as_of, "site", slugs, AUTH_FIELDS)).values()) if slugs else []
    if plant_ids:
        rows += _current(pit.fact_rows(conn, as_of, "plant", plant_ids, AUTH_FIELDS + QUEUE_FIELDS)).values()
    out: list[Authorization] = []
    queues: dict[str, Row] = {}
    for r in rows:
        subject, day, origin = f"{r['subject_type']}:{r['subject_id']}", pit.public_date(r), f"fact:{r['id']}"
        if r["field"] in QUEUE_FIELDS:
            for qid in r["value"] if isinstance(r["value"], list) else []:
                if isinstance(qid, str) and qid.strip():
                    queues[f"{(r['source_name'] or '').lower()}:{qid.strip()}"] = r
            continue
        for p in r["value"] if isinstance(r["value"], list) else []:
            if isinstance(p, dict) and p.get("number"):
                ids = tuple(str(i) for i in [p["number"], *(p.get("ids") or [])])
                out.append(Authorization(subject, "permit", str(p["number"]), ids, str(p.get("status") or ""),
                                         day, r["source_name"], origin))
    if queues:
        found = _current(pit.fact_rows(conn, as_of, "queue_project", sorted(queues), [QUEUE_STATUS]))
        for qid, plant_row in sorted(queues.items()):
            status = found.get((qid, QUEUE_STATUS))
            if status is None:
                continue  # a queue id without a status on file says nothing about the position
            out.append(Authorization(
                f"plant:{plant_row['subject_id']}", "queue", qid.split(":", 1)[1], (qid.split(":", 1)[1],),
                str(status["value"]), max(pit.public_date(plant_row), pit.public_date(status)),
                status["source_name"], f"fact:{status['id']}"))
    return out


def _mark_generic(items: list[Item]) -> list[Item]:
    """News filed under several sites (the same URL or title) is not about any one of them by itself."""
    sites_by_key: dict[str, set[str]] = defaultdict(set)
    for it in items:
        if it.field == "event:news":
            sites_by_key[it.source_url or it.text].add(it.subject)
    return [
        Item(**{**it.__dict__, "generic": True})
        if it.field == "event:news" and len(sites_by_key[it.source_url or it.text]) > 1 else it
        for it in items
    ]


def load(conn: Conn, as_of: date, slugs: list[str] | None = None) -> Inputs:
    sql = "SELECT slug, name, county, state, tracking_names FROM site"
    site_rows = conn.execute(sql + " ORDER BY slug").fetchall()
    all_slugs = [s["slug"] for s in site_rows]
    wanted = [s for s in site_rows if slugs is None or s["slug"] in slugs]
    parties_rows = conn.execute(
        """SELECT cp.site_slug, cp.role, cp.label, cp.entity_id, e.name, e.aliases FROM chain_party cp
           LEFT JOIN entity e ON e.id = cp.entity_id"""
    ).fetchall()
    by_site: dict[str, dict[str, Party]] = defaultdict(dict)
    for r in parties_rows:
        entity = {"name": r["name"], "aliases": r["aliases"]} if r["entity_id"] else None
        by_site[r["site_slug"]][r["role"]] = Party(r["role"], r["label"], r["entity_id"], _names(r["label"], entity))
    instruments: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT id, site_slug FROM instrument WHERE site_slug IS NOT NULL ORDER BY id").fetchall():
        instruments[r["site_slug"]].append(r["id"])
    links = conn.execute("SELECT site_slug, campus_id, status FROM site_link").fetchall()
    approved: dict[str, list[str]] = defaultdict(list)
    for r in links:
        if r["status"] == "approved":
            approved[r["site_slug"]].append(r["campus_id"])
    plants: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT plant_id, campus_id FROM plant_serves").fetchall():
        plants[r["campus_id"]].append(r["plant_id"])

    # Every site's events are read, so news filed under several sites is recognised as generic.
    site_items = fact_items(conn, as_of, "site", [s["slug"] for s in wanted], SITE_FIELDS)
    site_items += _mark_generic(event_items(conn, as_of, "site", all_slugs))
    campus_ids = sorted({c for s in wanted for c in approved.get(s["slug"], [])})
    plant_ids = sorted({p for c in campus_ids for p in plants.get(c, [])})
    linked = event_items(conn, as_of, "campus", campus_ids) + event_items(conn, as_of, "plant", plant_ids)
    entity_ids = sorted({p.entity_id for s in wanted for p in by_site[s["slug"]].values() if p.entity_id})
    entity_items: dict[str, list[Item]] = defaultdict(list)
    for it in fact_items(conn, as_of, "entity", entity_ids, ENTITY_FIELDS) + event_items(conn, as_of, "entity",
                                                                                        entity_ids):
        entity_items[it.subject.split(":", 1)[1]].append(it)
    inst_ids = sorted({i for s in wanted for i in instruments.get(s["slug"], [])})
    inst_items = fact_items(conn, as_of, "instrument", inst_ids, INSTRUMENT_FIELDS)
    inst_items += event_items(conn, as_of, "instrument", inst_ids)
    prices = _prices(conn, as_of, inst_ids)
    auths = authorizations(conn, as_of, [s["slug"] for s in wanted], plant_ids)

    sites: dict[str, SiteInputs] = {}
    for s in wanted:
        slug = s["slug"]
        own = [it for it in site_items if it.subject == f"site:{slug}"]
        place = next((it.text for it in own if it.field == "place"), None)
        party_names = [n for p in by_site[slug].values() for n in p.names]
        campuses = approved.get(slug, [])
        subjects = {f"campus:{c}" for c in campuses} | {f"plant:{p}" for c in campuses for p in plants.get(c, [])}
        own += [it for it in linked if it.subject in subjects]
        mine = set(instruments.get(slug, []))
        sites[slug] = SiteInputs(
            slug, s["name"], _keywords(s, party_names, place), by_site[slug], sorted(mine), own,
            [it for it in inst_items if it.subject.split(":", 1)[1] in mine],
            [prices[i] for i in sorted(mine) if i in prices],
            {c: plants.get(c, []) for c in campuses},
            [a for a in auths if a.subject in subjects or a.subject == f"site:{slug}"],
        )
    return Inputs(as_of, sites, dict(entity_items), sum(r["status"] == "approved" for r in links),
                  sum(r["status"] == "proposed" for r in links))
