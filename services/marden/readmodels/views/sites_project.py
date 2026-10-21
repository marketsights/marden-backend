"""Project file: `project_file` (GET /api/sites/:slug/project-file, types.ts ProjectFile), spec 26.

Checklist and completeness score (views/sites_checklist.py); parties from the case research's chain
parties (each with its evidence and the entity it names); structure from the site's instruments
(size, drawn, seniority, pricing, maturity as filed; filed holder counts), the research tranches
that are not instruments (sponsor equity, conduit bonds) and the security / guarantee / reserve /
covenant terms as quoted; construction from the announced cost, the general contractor and the
on-site plants' generating units. The derived signals (cost per MW vs peers, cost-to-complete and
funding gaps, draws vs build pace, contractor stress) are Marden models (M v0): n/d until they run.
No collector reads county recorders yet, so no mechanic's lien is listed.
"""

from __future__ import annotations

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.sites_checklist import Ctx, checklist
from marden.readmodels.views.sites_common import (
    INSTRUMENT_FIELDS,
    Doc,
    holders,
    latest_output,
    nd,
    output_field,
    site_events,
    site_instruments,
    site_row,
    site_slugs,
    text_of,
    with_value,
)
from marden.readmodels.views.sites_debt import KIND_LABEL
from marden.readmodels.views.sites_links import campus_buildings, linked_campuses, site_plants
from marden.readmodels.views.sites_tabs import site_proceedings

SITE_FIELDS = (
    "sponsor", "chain_parties", "chain_parties_presumed", "lease_tenant", "guarantor", "lease_guarantee", "contractors",
    "power_utility", "utility", "lenders", "holders_text", "fund_holders", "lease_term_years", "lease_term",
    "lease_annual_rent_usd", "lease_rent_basis", "lease_tcv_usd", "rent_start", "outside_date", "lease_termination",
    "tranche_summary", "tranches", "debt_items", "collateral", "sponsor_liquidity", "reserves", "covenants",
    "deal_metrics", "total_project_cost_usd", "facility_terms", "expected_energization", "last_visible_change",
    "change_score", "first_delivery", "power_contract", "onsite_generation", "air_permits", "land", "open_questions",
)
MULTI_FIELDS = ("structure_term", "credit_support", "debt_facility", "rfs_statement", "large_load_request")
PLANT_FIELDS = ("equipment_mentions", "latest_change", "air_permit", "expected_cod")
CAMPUS_FIELDS = ("developer", "chip_types", "capex_usd", "chips")
EXTRA_INSTRUMENT_FIELDS = (*INSTRUMENT_FIELDS, "covenant_test")


def project_context(conn: Conn, slug: str, instruments: list[Doc]) -> Ctx:
    campuses, _ = linked_campuses(conn, slug)
    plants = site_plants(conn, slug, campuses)
    unit_rows: list[tuple[Row, dict[str, Row]]] = []
    for p in plants:
        for u in conn.execute("SELECT * FROM power_unit WHERE plant_id = %s ORDER BY id", (p["id"],)):
            facts = known_facts(conn, "unit", u["id"])
            if facts:  # a unit row is shown once its unit statement fact was known
                unit_rows.append((u, facts))
    buildings = campus_buildings(conn, campuses)
    statuses = [s for b in buildings if (s := known_facts(conn, "building", b["id"], ["status"]).get("status"))]
    proceedings: dict[str, list[dict[str, Row]]] = {}
    kinds = ("court", "county_board", "zoning", "city_council", "state_env_permit", "state_air_permit")
    for _, facts in site_proceedings(conn, slug, kinds):
        proceedings.setdefault(str(facts["proceeding_kind"]["value"]), []).append(facts)
    facts = known_facts(conn, "site", slug, SITE_FIELDS)
    questions = (facts.get("open_questions") or {}).get("value")
    instrument_facts = [known_facts(conn, "instrument", i["id"], EXTRA_INSTRUMENT_FIELDS) for i in instruments]
    return Ctx(
        facts=facts,
        multi=known_fact_rows(conn, "site", slug, MULTI_FIELDS),
        campus_facts=[known_facts(conn, "campus", c["id"], CAMPUS_FIELDS) for c in campuses],
        plant_facts=[known_facts(conn, "plant", p["id"], PLANT_FIELDS) for p in plants],
        unit_rows=unit_rows,
        instrument_facts=instrument_facts,
        building_status=statuses,
        proceedings=proceedings,
        opposition=site_events(conn, slug, kinds=["opposition"]),
        holders=holders(conn, [i["id"] for i in instruments]),
        open_questions=[q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else [],
        modeled_outside=_modeled_outside(conn, slug),
    )


def _modeled_outside(conn: Conn, slug: str) -> Doc | None:
    outside = output_field(latest_output(conn, "cushion", "site", slug), "projection", "outsideDate")
    return outside if isinstance(outside, dict) and outside.get("value") is not None else None


def parties(conn: Conn, ctx: Ctx) -> list[Doc]:
    out = []
    for name in ("chain_parties", "chain_parties_presumed"):
        row = ctx.facts.get(name)
        for item in (row["value"] if row and isinstance(row["value"], list) else []):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            ids = item.get("entity_ids") or []
            entity = conn.execute(
                "SELECT e.id, p.name AS parent FROM entity e LEFT JOIN entity p ON p.id = e.parent_id WHERE e.id = %s",
                (ids[0],),
            ).fetchone() if ids else None
            out.append({
                "role": str(item.get("role") or "n/d"), "name": str(item["name"]),
                "entityId": entity["id"] if entity else None, "parent": entity["parent"] if entity else None,
                "evidence": {"date": row["valid_from"].isoformat(), "text": str(item.get("what") or item["name"]),
                             "tag": item.get("tag") if item.get("tag") in ("F", "R", "M") else row["tag"],
                             "sourceName": row.get("source_name") or "", "sourceUrl": row.get("source_url") or ""},
            })
    return out


def _sourced_text(row: Row | None, tag: str = "F") -> Doc:
    if row is None or not (text := text_of(row["value"])):
        return nd(tag)
    return with_value(row, text)


def _billions(row: Row | None, tag: str = "F") -> Doc:
    if row is None or not isinstance(row["value"], int | float):
        return nd(tag)
    return with_value(row, round(row["value"] / 1e9, 3))


def tranches(conn: Conn, slug: str, instruments: list[Doc], ctx: Ctx) -> list[Doc]:
    out = []
    for inst in instruments:
        f = inst["facts"]
        seniority = (f.get("seniority") or {}).get("value")
        out.append({
            "name": inst["name"], "type": KIND_LABEL.get(inst.get("kind") or "", inst.get("kind") or "n/d"),
            "amountB": _billions(f.get("amount_usd"), "R"), "drawnB": _billions(f.get("drawn_usd")),
            "seniority": str(seniority) if seniority else "n/d", "pricing": _sourced_text(f.get("coupon")),
            "maturity": _sourced_text(f.get("maturity")), "holdersCount": holders(conn, [inst["id"]])["count"],
            "instrumentId": inst["id"],
        })
    covered = {i["id"] for i in instruments}
    row = ctx.facts.get("tranches")
    for item in (row["value"] if row and isinstance(row["value"], list) else []):
        if not isinstance(item, dict) or item.get("instrument_id") in covered or not item.get("name"):
            continue
        amount = item.get("amount_usd")
        out.append({
            "name": str(item["name"]), "type": "Equity" if "equity" in str(item["name"]).lower() else "Other",
            "amountB": with_value(row, round(amount / 1e9, 3)) if isinstance(amount, int | float) else nd("R"),
            "drawnB": nd("F"), "seniority": str(item.get("ranking") or "n/d"),
            "pricing": with_value(row, str(item["terms"])) if item.get("terms") else nd("F"),
            "maturity": nd("F"), "holdersCount": 0, "instrumentId": None,
        })
    return out


def _term_value(ctx: Ctx, *keys: str) -> Row | None:
    for row in ctx.multi.get("structure_term", []):
        key = str(row["value"].get("key") or "").lower() if isinstance(row["value"], dict) else ""
        if any(k in key for k in keys):
            return row
    return None


def structure(conn: Conn, slug: str, instruments: list[Doc], ctx: Ctx) -> Doc:
    completion = [r for r in ctx.multi.get("credit_support", [])
                  if isinstance(r["value"], dict) and "completion" in str(r["value"].get("type", "")).lower()]
    return {
        "tranches": tranches(conn, slug, instruments, ctx),
        "security": _sourced_text(ctx.facts.get("collateral") or _term_value(ctx, "collateral")),
        "completionGuarantee": _sourced_text(completion[0] if completion else _term_value(ctx, "completion")),
        "sponsorSupport": _sourced_text(
            ctx.facts.get("sponsor_liquidity") or _term_value(ctx, "equity cushion", "backstop")),
        "reserves": _sourced_text(ctx.facts.get("reserves") or _term_value(ctx, "reserves")),
        "sizingCovenants": _sourced_text(ctx.facts.get("covenants") or ctx.facts.get("deal_metrics")),
        "waterfall": _sourced_text(_term_value(ctx, "waterfall")),
        "hedges": _sourced_text(_term_value(ctx, "hedg")),
        "ratingTriggers": _sourced_text(_term_value(ctx, "rating")),
        "intercreditor": _sourced_text(_term_value(ctx, "intercreditor")),
    }


def construction(ctx: Ctx) -> Doc:
    announced = ctx.facts.get("total_project_cost_usd")
    contractors = ctx.facts.get("contractors")
    names = [str(c.get("name")) for c in contractors["value"] if isinstance(c, dict) and c.get("name")] \
        if contractors and isinstance(contractors["value"], list) else []
    equipment = []
    for unit, _ in ctx.unit_rows:
        label = " ".join(str(x) for x in (unit.get("oem"), unit.get("model")) if x) or "Generating units"
        equipment.append({"item": label, "oem": unit.get("oem") or "n/d", "units": unit.get("count"),
                          "neededBy": None, "delivery": nd("F"), "late": False})
    return {
        "declaredCostB": nd("F"), "announcedCostB": _billions(announced, "R"),
        "costPerMwM": nd("M"), "peerCostPerMwM": nd("M"), "costToCompleteGapB": nd("M"), "fundingGapB": nd("M"),
        "drawsVsProgress": [], "drawPaceFlag": None,
        "generalContractor": with_value(contractors, "; ".join(names)) if names else nd("F"),
        "liens": [], "contractorStress": nd("M"), "equipment": equipment,
    }


def project_file(conn: Conn, slug: str) -> Doc | None:
    if site_row(conn, slug) is None:
        return None
    instruments = site_instruments(conn, slug)
    ctx = project_context(conn, slug, instruments)
    items, completeness = checklist(ctx)
    return {"siteSlug": slug, "completeness": completeness, "checklist": items, "parties": parties(conn, ctx),
            "structure": structure(conn, slug, instruments, ctx), "construction": construction(ctx)}


@builder("project_file", keys=site_slugs)
def build_project_file(conn: Conn, slug: str) -> Doc | None:
    return project_file(conn, slug)
