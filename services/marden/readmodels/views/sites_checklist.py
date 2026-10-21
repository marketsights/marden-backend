"""The project-file checklist (spec 26): one row per item and project, each sourced, n/d or n/p.

Each item names the facts that answer it, in order of preference; the first one Marden knew on the
build date gives the value (Sourced<string>, the fact's own tag and source). An item no source has
answered is n/d (counts against completeness); Insurance and Independent engineer items are n/p
(not public) unless a filed document discloses them. Completeness = sourced / (sourced + n/d), in
whole percent. An n/d item carries the case research's open question for that area, when there is one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from marden.core.db import Row
from marden.readmodels.views.sites_common import Doc, nd, text_of, with_value


@dataclass
class Ctx:
    """Everything the checklist reads for one site (filled by sites_project.project_context)."""
    facts: dict[str, Row]
    multi: dict[str, list[Row]]
    campus_facts: list[dict[str, Row]] = field(default_factory=list)
    plant_facts: list[dict[str, Row]] = field(default_factory=list)
    unit_rows: list[tuple[Row, dict[str, Row]]] = field(default_factory=list)
    instrument_facts: list[dict[str, Row]] = field(default_factory=list)
    building_status: list[Row] = field(default_factory=list)
    proceedings: dict[str, list[dict[str, Row]]] = field(default_factory=dict)
    opposition: list[Row] = field(default_factory=list)
    holders: dict[str, Any] = field(default_factory=dict)
    holder_row: Row | None = None
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    modeled_outside: Doc | None = None  # the cushion model's outside date (Sourced, its source's tag)


Answer = Doc | None
Resolver = Callable[[Ctx], Answer]


def fact(*names: str) -> Resolver:
    def resolve(ctx: Ctx) -> Answer:
        for name in names:
            row = ctx.facts.get(name)
            if row is not None and (text := text_of(row["value"])):
                return with_value(row, text)
        return None
    return resolve


def term(*keys: str) -> Resolver:
    """A structure_term row whose key contains one of `keys`."""
    def resolve(ctx: Ctx) -> Answer:
        for row in ctx.multi.get("structure_term", []):
            key = str((row["value"] or {}).get("key") or "").lower() if isinstance(row["value"], dict) else ""
            if any(k in key for k in keys) and (text := text_of(row["value"])):
                return with_value(row, text)
        return None
    return resolve


def _undisclosed(name: Any) -> bool:
    text = str(name or "").strip().lower()
    return not text or text in ("n/d", "unknown") or "not disclosed" in text


def party(*roles: str) -> Resolver:
    """chain_parties entries (case research) of these roles."""
    def resolve(ctx: Ctx) -> Answer:
        for name in ("chain_parties", "chain_parties_presumed"):
            row = ctx.facts.get(name)
            items = row["value"] if row and isinstance(row["value"], list) else []
            hits = [i for i in items if isinstance(i, dict) and any(r in str(i.get("role", "")).lower() for r in roles)
                    and not _undisclosed(i.get("name"))]
            if hits:
                return with_value(row, "; ".join(f"{i.get('name')} ({i.get('role')})" for i in hits))
        return None
    return resolve


def multi(name: str, match: Callable[[Any], bool] = lambda v: True) -> Resolver:
    def resolve(ctx: Ctx) -> Answer:
        rows = [r for r in ctx.multi.get(name, []) if match(r["value"])]
        if rows and (text := "; ".join(t for r in rows if (t := text_of(r["value"], 200)))):
            return with_value(rows[0], text[:400])
        return None
    return resolve


def from_list(rows_of: Callable[[Ctx], list[dict[str, Row]]], *names: str) -> Resolver:
    def resolve(ctx: Ctx) -> Answer:
        for facts in rows_of(ctx):
            for name in names:
                row = facts.get(name)
                if row is not None and (text := text_of(row["value"])):
                    return with_value(row, text)
        return None
    return resolve


def first(*resolvers: Resolver) -> Resolver:
    def resolve(ctx: Ctx) -> Answer:
        for r in resolvers:
            if (answer := r(ctx)) is not None:
                return answer
        return None
    return resolve


def units(ctx: Ctx) -> Answer:
    if not ctx.unit_rows:
        return None
    parts, rows = [], []
    for unit, facts in ctx.unit_rows:
        label = " ".join(str(x) for x in (unit.get("oem"), unit.get("model")) if x) or "units"
        parts.append(f"{unit['count']} x {label}" if unit.get("count") else label)
        rows.extend(facts.values())
    if not rows:
        return None
    return with_value(rows[0], "; ".join(parts))


def holders_answer(ctx: Ctx) -> Answer:
    summary = ctx.holders
    if summary.get("count"):
        return {"value": f"{summary['count']} filers, ${summary['par'] / 1e6:,.1f}mm par", "tag": "F",
                "asOf": summary["period"].isoformat(), "sourceName": ", ".join(summary["filings"])}
    return fact("holders_text", "fund_holders")(ctx)


def buildings_answer(ctx: Ctx) -> Answer:
    if not ctx.building_status:
        return None
    return with_value(ctx.building_status[0], f"{len(ctx.building_status)} buildings with footprints")


def proceedings(*kinds: str) -> Resolver:
    def resolve(ctx: Ctx) -> Answer:
        found = [f["proceeding"] for k in kinds for f in ctx.proceedings.get(k, []) if f.get("proceeding")]
        if not found:
            return None
        return with_value(found[0], "; ".join(str(r["value"]) for r in found)[:400])
    return resolve


def opposition_answer(ctx: Ctx) -> Answer:
    if ctx.opposition:
        ev = ctx.opposition[0]
        n = len(ctx.opposition)
        out = {"value": f"{n} opposition record{'s' if n != 1 else ''}; latest: {ev['title']}", "tag": ev["tag"],
               "asOf": ev["event_date"].isoformat(), "sourceName": ev["source_name"]}
        if ev.get("source_url"):
            out["sourceUrl"] = ev["source_url"]
        return out
    return proceedings("court", "county_board", "zoning", "city_council")(ctx)


def mentions(names: tuple[str, ...], *words: str) -> Resolver:
    """A text fact that mentions one of `words` (insurance in a reserves note)."""
    def resolve(ctx: Ctx) -> Answer:
        for name in names:
            rows = ctx.multi.get(name) or ([ctx.facts[name]] if name in ctx.facts else [])
            for row in rows:
                text = text_of(row["value"], 2000) or ""
                if any(w in text.lower() for w in words):
                    return with_value(row, text[:400])
        return None
    return resolve


def none(ctx: Ctx) -> Answer:
    return None


def _is_completion(value: Any) -> bool:
    return isinstance(value, dict) and "completion" in str(value.get("type", "")).lower()


def _campuses(ctx: Ctx) -> list[dict[str, Row]]:
    return ctx.campus_facts


def _plants(ctx: Ctx) -> list[dict[str, Row]]:
    return ctx.plant_facts


def _instruments(ctx: Ctx) -> list[dict[str, Row]]:
    return ctx.instrument_facts


# (area, item, resolver, not public when unanswered)
ITEMS: tuple[tuple[str, str, Resolver, bool], ...] = (
    ("Parties", "Sponsor and equity investors", first(fact("sponsor"), party("sponsor", "equity")), False),
    ("Parties", "Developer", first(party("developer"), from_list(_campuses, "developer")), False),
    ("Parties", "Landlord SPV and parents",
     party("project company", "spv", "landlord", "borrower", "issuer", "owner"), False),
    ("Parties", "Tenant and end user", first(fact("lease_tenant"), party("tenant", "end user")), False),
    ("Parties", "Guarantors", first(fact("guarantor", "lease_guarantee"), party("guarantor")), False),
    ("Parties", "General contractor and major subcontractors", first(fact("contractors"), party("contractor")), False),
    ("Parties", "Equipment makers", first(units, party("equipment", "supplier", "oem", "manufacturer", "vendor"),
                                           from_list(_campuses, "chip_types")), False),
    ("Parties", "Utility and grid operator", first(fact("power_utility", "utility"), party("utility")), False),
    ("Parties", "Lenders, agents and trustees",
     first(fact("lenders"), party("lender", "arranger", "agent", "trustee")), False),
    ("Parties", "Holders", holders_answer, False),
    ("Lease", "Term", fact("lease_term_years", "lease_term"), False),
    ("Lease", "Rent", fact("lease_annual_rent_usd", "lease_rent_basis", "lease_tcv_usd"), False),
    ("Lease", "Escalators", term("escalat"), False),
    ("Lease", "Commencement", fact("rent_start"), False),
    ("Lease", "Outside date", first(fact("outside_date"), lambda ctx: ctx.modeled_outside), False),
    ("Lease", "Termination rights", first(fact("lease_termination"), term("termination")), False),
    ("Lease", "Force majeure", term("force majeure"), False),
    ("Lease", "Residual value guarantees",
     first(term("residual"), mentions(("lease_guarantee", "guarantor"), "rvg", "residual")), False),
    ("Structure", "Debt tranches and sizes", fact("tranche_summary", "tranches", "debt_items"), False),
    ("Structure", "Seniority", from_list(_instruments, "seniority"), False),
    ("Structure", "Security package", first(fact("collateral"), term("collateral")), False),
    ("Structure", "Completion guarantees", first(multi("credit_support", _is_completion), term("completion")), False),
    ("Structure", "Sponsor support", first(fact("sponsor_liquidity"), term("equity cushion", "backstop")), False),
    ("Structure", "Reserves", first(fact("reserves"), term("reserves")), False),
    ("Structure", "Sizing covenants",
     first(fact("covenants", "deal_metrics"), from_list(_instruments, "covenant_test")), False),
    ("Structure", "Cash waterfall", term("waterfall"), False),
    ("Structure", "Hedges", first(term("hedg"), mentions(("covenants",), "hedg")), False),
    ("Structure", "Rating triggers", first(term("rating"), mentions(("covenants",), "rating")), False),
    ("Structure", "Intercreditor terms", term("intercreditor"), False),
    ("Budget", "Declared construction value per building permit", none, False),
    ("Budget", "Capital investment committed in tax abatement agreements", term("irb", "abatement"), False),
    ("Budget", "Announced project cost", fact("total_project_cost_usd"), False),
    ("Budget", "Cost per MW vs peers", none, False),
    ("Budget", "Equipment cost signals", none, False),
    ("Draws", "Amount drawn vs committed per facility", first(fact("facility_terms"), multi("debt_facility"),
                                                              from_list(_instruments, "drawn_usd")), False),
    ("Draws", "Equity contributed", term("equity cushion"), False),
    ("Construction", "Milestones", first(multi("rfs_statement"), fact("expected_energization")), False),
    ("Construction", "Satellite change", fact("last_visible_change", "change_score"), False),
    ("Construction", "Building footprints", buildings_answer, False),
    ("Construction", "Certificates of occupancy", none, False),
    ("Construction", "Inspections passed", none, False),
    ("Contractor", "Mechanic's liens", none, False),
    ("Contractor", "Lien releases", none, False),
    ("Contractor", "Contractor disputes and lawsuits", none, False),
    ("Contractor", "Change of contractor", none, False),
    ("Contractor", "Workforce signals (hiring, layoffs, WARN notices)", none, False),
    ("Equipment", "Turbine, transformer and switchgear orders and delivery dates",
     first(units, from_list(_plants, "equipment_mentions")), False),
    ("Equipment", "GPU deliveries",
     first(multi("debt_facility", lambda v: isinstance(v, dict) and bool(v.get("gpu_count"))),
           from_list(_campuses, "chips"), fact("first_delivery")), False),
    ("Power", "Interconnection and energy supply agreements",
     first(fact("power_contract"), multi("large_load_request")), False),
    ("Power", "On-site generation", fact("onsite_generation"), False),
    ("Power", "Fuel supply and pipeline laterals", first(party("fuel"), from_list(_plants, "latest_change")), False),
    ("Permits", "Air permits", first(fact("air_permits"), from_list(_plants, "air_permit")), False),
    ("Permits", "Building permits", none, False),
    ("Permits", "Water permits", none, False),
    ("Permits", "Zoning", proceedings("zoning"), False),
    ("Permits", "Environmental permits", proceedings("state_env_permit", "state_air_permit"), False),
    ("Permits", "Land ownership and title", first(fact("land"), term("title")), False),
    ("Opposition", "Agendas, petitions, lawsuits and moratoria", opposition_answer, False),
    ("Opposition", "State legislation", none, False),
    ("Insurance", "Builder's risk and delay-in-start-up cover",
     mentions(("reserves", "structure_term"), "insurance"), True),
    ("Independent engineer", "Independent engineer reports and lender site visits", none, True),
)

STOP_WORDS = frozenset({"and", "the", "with", "from", "per", "for", "orders", "dates", "signals", "terms", "major"})


def research_note(ctx: Ctx, item: str) -> str | None:
    """The case research's open question that names this item, if any."""
    words = {w for w in "".join(c if c.isalnum() else " " for c in item.lower()).split()
             if len(w) >= 4 and w not in STOP_WORDS}
    for q in ctx.open_questions:
        text = f" {q.get('item', '')} ".lower()
        if any(f" {w}" in text for w in words):
            return f"Open question in the case research: {q.get('item')}"
    return None


def checklist(ctx: Ctx) -> tuple[list[Doc], int]:
    items: list[Doc] = []
    for area, item, resolve, not_public in ITEMS:
        answer = resolve(ctx)
        if answer is not None:
            items.append({"area": area, "item": item, "state": "sourced", "value": answer})
            continue
        row: Doc = {"area": area, "item": item, "state": "n/p" if not_public else "n/d", "value": nd("F")}
        if not not_public and (note := research_note(ctx, item)):
            row["researchNote"] = note
        if area == "Independent engineer":
            row["researchNote"] = "Not public; Marden's evidence trail (Evidence tab) is the substitute."
        items.append(row)
    counted = [i for i in items if i["state"] != "n/p"]
    sourced = sum(i["state"] == "sourced" for i in counted)
    return items, round(100 * sourced / len(counted)) if counted else 0
