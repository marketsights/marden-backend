"""What the cushion model reads for one site on its as-of day (point in time, marden.models.pit).

- the site's schedule facts (`SITE_FIELDS`): every row, newest first, so dated one-row-per-item fields
  (rfs_statement, schedule_change) keep their history;
- the power plants that serve it (plant `serves_sites`), with their COD, status and permit facts;
- its catalyst, opposition and permit events;
- the lease outside date (`outside_date`), with its tag and source, else a lease trigger reported in
  the site's own records ('... payment deferral if not operational by 2028'), else none (n/d);
- the rent, when disclosed (`lease_annual_rent_usd`, else contract value / term).

Nothing here is estimated: a value no source gives stays None and is reported as missing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models import cushion_dates as cd
from marden.models.pit import current_of, event_rows, fact_rows

SITE_FIELDS = (
    "rent_start", "outside_date", "expected_energization", "original_energization", "first_delivery",
    "rfs_statement", "schedule_change", "power_supply", "power_status", "power_contract", "onsite_generation",
    "land", "air_permits", "construction_stage", "pct_complete", "leases", "lease_annual_rent_usd",
    "lease_tcv_usd", "lease_term_years",
)
PLANT_FIELDS = (
    "serves_sites", "expected_cod", "original_cod", "status", "stage", "technology_type", "air_permit",
    "latest_change",
)
EVENT_KINDS = ("catalyst", "opposition", "permit")
_TRIGGER = re.compile(
    r"if\b[^.;]{0,40}?\bmissed|if not (?:operational|complete[d]?|in service)|slips? past|outside (?:completion )?date|"
    r"deferral if|grace[- ]period end|delivery delay window", re.I)


def text_of(row: Row | None) -> str:
    """The readable text of a fact value: a string, or the text / what / value of an object."""
    if row is None:
        return ""
    if "value" not in row:  # an event row
        return str(row.get("title") or "")
    value = row["value"]
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "what", "value"):
            if isinstance(value.get(key), str):
                return value[key]
    if isinstance(value, int | float):
        return str(value)
    return ""


@dataclass
class Plant:
    id: str
    name: str
    facts: dict[str, list[Row]]

    def current(self, fld: str) -> Row | None:
        return current_of(self.facts.get(fld, []))

    @property
    def is_supply_line(self) -> bool:
        """A gas lateral / pipeline or transmission line feeding the site, not generation."""
        return bool(re.search(r"\b(lateral|pipeline|transmission line)\b", self.name, re.I))


@dataclass
class Outside:
    value: str            # 'YYYY-MM' or 'YYYY' as the lease gives it
    month: int            # the month index the cushion compares with (the deadline is that month's end)
    assumed: str | None   # ISO date the cushion uses when the lease gives less than a month
    row: Row
    basis: str            # 'outside_date fact' | 'lease trigger in <field>'
    quote: str
    # a year only: the deadline is anywhere in that year, tested from its worst case (Jan 1: the month
    # index before January) to its best (Dec 31: December); `month` / `assumed` are the midpoint, Jul 1
    window: tuple[int, int] | None = None

    @property
    def year_only(self) -> bool:
        return self.window is not None


@dataclass
class Rent:
    monthly_usd: float
    row: Row
    basis: str


@dataclass
class SiteInputs:
    slug: str
    name: str
    as_of: date
    facts: dict[str, list[Row]]
    plants: list[Plant] = field(default_factory=list)
    events: list[Row] = field(default_factory=list)
    outside: Outside | None = None
    rent: Rent | None = None

    def rows(self, fld: str) -> list[Row]:
        return self.facts.get(fld, [])

    def current(self, fld: str) -> Row | None:
        return current_of(self.rows(fld))

    def text(self, fld: str) -> str:
        return text_of(self.current(fld))

    def fingerprint(self) -> list[Any]:
        """The input row ids this site's projection was built from (part of the inputs hash)."""
        ids = sorted(r["id"] for rows in self.facts.values() for r in rows)
        ids += sorted(r["id"] for p in self.plants for rows in p.facts.values() for r in rows)
        return [self.slug, self.as_of.isoformat(), ids, sorted(e["id"] for e in self.events)]


def _group(rows: list[Row]) -> dict[str, dict[str, list[Row]]]:
    out: dict[str, dict[str, list[Row]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        out[r["subject_id"]][r["field"]].append(r)
    return out


def load_sites(conn: Conn, as_of: date, slugs: list[str]) -> list[SiteInputs]:
    names = {r["slug"]: r["name"] for r in conn.execute(
        "SELECT slug, name FROM site WHERE slug = ANY(%s)", (slugs,)).fetchall()}
    site_facts = _group(fact_rows(conn, as_of, "site", slugs, SITE_FIELDS))
    plant_rows = _group(fact_rows(conn, as_of, "plant", None, PLANT_FIELDS))
    plant_names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM power_plant").fetchall()}
    serves: dict[str, list[str]] = defaultdict(list)
    for plant_id, facts in plant_rows.items():
        row = current_of(facts.get("serves_sites", []))
        if row is not None and isinstance(row["value"], list):
            for slug in row["value"]:
                serves[str(slug)].append(plant_id)
    events: dict[str, list[Row]] = defaultdict(list)
    for e in event_rows(conn, as_of, "site", slugs, EVENT_KINDS):
        events[e["subject_id"]].append(e)
    out = []
    for slug in slugs:
        if slug not in names:
            continue
        site = SiteInputs(slug, names[slug], as_of, dict(site_facts.get(slug, {})), events=events.get(slug, []))
        site.plants = [Plant(pid, plant_names.get(pid, pid), dict(plant_rows[pid])) for pid in sorted(serves[slug])]
        site.outside = outside_date(site)
        site.rent = rent(site)
        out.append(site)
    return out


def _outside_from(text: str, row: Row, basis: str, *, trigger_only: bool) -> Outside | None:
    exprs = cd.targets(text)
    if trigger_only:
        exprs = [w for w in exprs if _TRIGGER.search(cd.clause_of(text, w)) or _TRIGGER.search(
            text[max(0, w.start - 40):w.end + 25])]
    when = cd.leading(exprs)
    if when is None:
        return None
    if when.precision == "year":  # the deadline is somewhere in that year: a range, its midpoint Jul 1 central
        year = when.lo // 12
        return Outside(str(year), cd.month_index(year, 6), f"{year}-07-01", row, basis, text,
                       window=(cd.month_index(year, 1) - 1, cd.month_index(year, 12)))
    if when.rank <= cd.PRECISION_RANK["month"]:
        return Outside(cd.ym(when.lo), when.lo, None, row, basis, text)
    return Outside(cd.ym(when.hi), when.hi, cd.month_end(when.hi).isoformat(), row, basis, text)


def outside_date(site: SiteInputs) -> Outside | None:
    row = site.current("outside_date")
    if row is not None:
        found = _outside_from(text_of(row), row, "outside_date fact", trigger_only=False)
        if found is not None:
            return found
    candidates: list[Outside] = []
    for fld in ("rfs_statement", "schedule_change"):
        for r in site.rows(fld):
            found = _outside_from(text_of(r), r, f"lease trigger in {fld}", trigger_only=True)
            if found:
                candidates.append(found)
    for r in site.rows("leases"):
        for lease in r["value"] if isinstance(r["value"], list) else []:
            text = " ".join(str(lease.get(k) or "") for k in ("outside_date", "termination_rights"))
            found = _outside_from(text, r, "lease trigger in leases", trigger_only=not lease.get("outside_date"))
            if found:
                candidates.append(found)
    if not candidates:  # the plant's record of the same lease trigger, only when the site's records lack it
        for plant in site.plants:
            r = plant.current("expected_cod")
            if r is not None:
                found = _outside_from(text_of(r), r, f"lease trigger in plant {plant.id} expected_cod",
                                      trigger_only=True)
                if found:
                    candidates.append(found)
    if not candidates:
        return None
    # the newest record; filed over reported on the same day
    return max(candidates, key=lambda o: (o.row["valid_from"], o.row["tag"] == "F", o.row["id"]))


def rent(site: SiteInputs) -> Rent | None:
    annual = site.current("lease_annual_rent_usd")
    if annual is not None and isinstance(annual["value"], int | float) and annual["value"] > 0:
        return Rent(float(annual["value"]) / 12, annual, "lease_annual_rent_usd / 12")
    tcv, term = site.current("lease_tcv_usd"), site.current("lease_term_years")
    if (tcv is not None and term is not None and isinstance(tcv["value"], int | float)
            and isinstance(term["value"], int | float) and tcv["value"] > 0 and term["value"] > 0):
        return Rent(float(tcv["value"]) / (float(term["value"]) * 12), tcv,
                    "lease_tcv_usd / (lease_term_years x 12): the contract's average monthly rent")
    return None
