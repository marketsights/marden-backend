"""Payer page (spec 08): `payer` (GET /api/payers/:slug, Payer), keyed by entity id.

The sites are every site the payer is tenant or end user of (`chain_party`) plus the sites its own
leases, compute contracts and guarantees on the obligation map are linked to; non-US sites are left out
as on the sites list. Each site row is the site's SiteSummary values (sites.site_summary: the status
model's chain status, the cushion model's rent start and cushion, debt summed from its instruments),
with the outside date from the cushion model's projection, else the site's `outside_date` fact. The KPI
totals are sums of those rows (spec 08 acceptance), so site debt and GW always match the table.

- rating: the tenant's latest rating statement across its sites (`tenant_rating`, the text the status
  model's T1 rule reads), else the entity's `rating_summary`; n/d when neither exists.
- notesB / noteHolders: its own bonds and converts (issuer = the entity or its subsidiaries), sizes
  summed; funds holding them from N-PORT / BDC filings.
- cds5y: the bond-implied 5y CDS of the price engine's `cds` output for the entity (`spreadBp`, its as-of
  and method version, tag M); n/d where the price engine has no CDS for the payer (no traded bonds).
- paidBy: the next link up (payers_paid_by.py): the end users of the sites it is tenant of, and payers of
  the leases and compute contracts it is owed; each with its own worst link in the status model's outputs
  (a party with no judged link has no status and is not listed), the rule and statement behind it, and
  its dated funding, commitment and rating statements.
- credit: funding statements (`funding_fact`) and the statements behind its amber or red links.
- stressSummary: the `stress` model's payer summary, omitted until that model runs.
"""

from __future__ import annotations

from marden.core.db import Conn, Row
from marden.readmodels.registry import builder
from marden.readmodels.views.payers_common import (
    OWED_TYPES,
    Doc,
    billions_of,
    chain_roles,
    edge_inputs,
    edge_known,
    edge_sites,
    entity_row,
    latest_output,
    nd,
    num,
    obligation_rows,
    output_field,
    payer_ids,
    site_names,
    summed,
    tripped_links,
    with_value,
)
from marden.readmodels.views.payers_debt import holder_funds
from marden.readmodels.views.payers_paid_by import funding_rows, funding_text, paid_by, rating_statement, rule_label
from marden.readmodels.views.sites import projection_of, site_summary
from marden.readmodels.views.sites_common import gw_text, is_us, known_facts, site_rows

NOTE_KINDS = ("bond", "convert")


# ---------------------------------------------------------------- sites
def payer_site_slugs(conn: Conn, entity_id: str) -> list[str]:
    slugs = {p["site_slug"] for p in chain_roles(conn, entity_id) if p["role"] in ("tenant", "end_user")}
    known = {r["slug"] for r in site_rows(conn)}
    for ob in obligation_rows(conn, entity_id):
        if ob["from_entity_id"] != entity_id or ob["type"] not in OWED_TYPES:
            continue
        facts, rows = edge_inputs(conn, ob)
        if edge_known(facts, rows):
            slugs.update(edge_sites(ob, facts, known))
    return sorted(slugs)


def payer_role(conn: Conn, entity_id: str) -> str:
    """'end_user' when it is end user on more sites than it is tenant on, or (no chain role) when it owes
    compute contracts but no lease; else 'tenant'."""
    roles = [p["role"] for p in chain_roles(conn, entity_id)]
    if roles.count("end_user") or roles.count("tenant"):
        return "end_user" if roles.count("end_user") > roles.count("tenant") else "tenant"
    types = {ob["type"] for ob in obligation_rows(conn, entity_id) if ob["from_entity_id"] == entity_id}
    return "end_user" if "Compute purchase" in types and "Lease" not in types else "tenant"


def site_mw(conn: Conn, slug: str) -> tuple[Row, float] | None:
    """The MW the site's GW column shows (sites_common.gw_value: gross MW, else critical IT MW)."""
    facts = known_facts(conn, "site", slug, ["gross_mw", "critical_it_mw"])
    for field in ("gross_mw", "critical_it_mw"):
        if (row := facts.get(field)) and num(row["value"]) is not None:
            return row, float(num(row["value"]))
    return None


def outside_date(conn: Conn, slug: str, projection: Doc | None) -> Doc:
    given = (projection or {}).get("outsideDate")
    if isinstance(given, dict) and given.get("value") is not None:
        return given
    row = known_facts(conn, "site", slug, ["outside_date"]).get("outside_date")
    if row is not None and isinstance(row["value"], str) and row["value"]:
        return with_value(row, row["value"])
    return nd("R")


def site_row(conn: Conn, site: Row) -> tuple[Doc, Doc]:
    """(Payer.sites row, the SiteSummary it was cut from)."""
    summary = site_summary(conn, site)
    projection = projection_of(latest_output(conn, "cushion", "site", site["slug"]))
    months = (summary.get("cushionMonths") or {}).get("value")
    return {
        "slug": site["slug"],
        "name": site["name"],
        "gw": summary["gw"].get("value") or "n/d",
        "rentStartP50": summary["rentStartP50"],
        "outsideDate": outside_date(conn, site["slug"], projection),
        "cushionMonths": months if isinstance(months, int | float) else None,
        "debtB": float(summary["debtB"].get("value") or 0.0),
        "status": summary["status"],
    }, summary


def _site_order(row: Doc) -> tuple:
    return (row["rentStartP50"] is None, row["rentStartP50"] or "", row["name"])


# ---------------------------------------------------------------- KPIs
def gw_leased(conn: Conn, slugs: list[str]) -> Doc:
    found = [m for s in slugs if (m := site_mw(conn, s))]
    if not found:
        return nd("R")
    parts = [with_value(row, mw) for row, mw in found]
    total = summed(parts)
    total["value"] = gw_text(total["value"])
    return total


def own_notes(conn: Conn, entity_id: str) -> list[Row]:
    return conn.execute(
        """SELECT i.id FROM instrument i JOIN entity e ON e.id = i.issuer_entity_id
            WHERE (e.id = %(e)s OR e.parent_id = %(e)s) AND i.kind = ANY(%(kinds)s) ORDER BY i.id""",
        {"e": entity_id, "kinds": list(NOTE_KINDS)},
    ).fetchall()


def notes_kpis(conn: Conn, entity_id: str) -> tuple[Doc, Doc]:
    ids, sizes = [], []
    for inst in own_notes(conn, entity_id):
        facts = known_facts(conn, "instrument", inst["id"], ["amount_usd", "status"])
        if (facts.get("status") or {}).get("value") in ("repaid", "cancelled", "canceled"):
            continue
        ids.append(inst["id"])
        if (row := facts.get("amount_usd")) is not None:
            sizes.append(billions_of(row, "F"))
    notes = summed(sizes, "F")
    funds, period = holder_funds(conn, ids)
    if not ids or period is None:
        return notes, nd("F")
    holders = {"value": len(funds), "tag": "F", "asOf": period.isoformat(), "sourceName": "N-PORT and BDC filings"}
    return notes, holders


def cds_text(out: Row | None) -> Doc:
    """The 5y CDS KPI from the price engine's `cds` output (subject ('entity', id)): `spreadBp`, the
    bond-implied 5-year spread, shown as '240bp' with the output's as-of and method ('price v0.1')."""
    spread = num(output_field(out, "spreadBp"))
    if out is None or spread is None:
        return nd("M")
    return {
        "value": f"{float(spread):.0f}bp",
        "tag": "M",
        "asOf": out["as_of"].isoformat(),
        "methodVersion": str(out["version"]),
        "sourceName": str(output_field(out, "label") or "Marden bond-implied CDS"),
    }


# ---------------------------------------------------------------- rail
def credit_facts(conn: Conn, entity_id: str) -> dict[str, Doc]:
    credit: dict[str, Doc] = {}
    for row in funding_rows(conn, entity_id):
        if (got := funding_text(row)) is not None:
            credit.setdefault(got[0], with_value(row, got[1]))
    names = site_names(conn)
    grouped: dict[tuple[str, str], tuple[list[str], Doc]] = {}
    for slug, link, fact in tripped_links(conn, entity_id):
        rule = rule_label(link)
        sites, _ = grouped.setdefault((rule, str(fact.get("value"))), ([], fact))
        sites.append(names.get(slug, slug))
    for (rule, _), (sites, fact) in grouped.items():
        credit.setdefault(f"{rule}: {', '.join(sorted(sites))}", fact)
    return credit


def stress_summary(conn: Conn, entity_id: str) -> Doc | None:
    out = latest_output(conn, "stress", "entity", entity_id)
    raw = output_field(out, "stressSummary") or (out["output"] if out else None)
    if not isinstance(raw, dict) or not isinstance(raw.get("scenario"), str):
        return None
    losses = {k: num(raw.get(k)) for k in ("siteDebtLossB", "notesLossB")}
    if any(v is None for v in losses.values()):
        return None
    return {"scenario": raw["scenario"], **losses}


# ---------------------------------------------------------------- builder
def payer_doc(conn: Conn, entity: Row) -> Doc:
    by_slug = {s["slug"]: s for s in site_rows(conn)}
    sites = [by_slug[s] for s in payer_site_slugs(conn, entity["id"]) if s in by_slug and is_us(by_slug[s])]
    rows, summaries = [], []
    for site in sites:
        row, summary = site_row(conn, site)
        rows.append(row)
        summaries.append(summary)
    rows.sort(key=_site_order)
    notes, holders = notes_kpis(conn, entity["id"])
    doc: Doc = {
        "slug": entity["id"],
        "name": entity["name"],
        "role": payer_role(conn, entity["id"]),
        "kpis": {
            "siteDebtB": summed([s["debtB"] for s in summaries]),
            "gwLeased": gw_leased(conn, [s["slug"] for s in sites]),
            "rating": rating_statement(conn, entity["id"]),
            "cds5y": cds_text(latest_output(conn, "cds", "entity", entity["id"])),
            "notesB": notes,
            "noteHolders": holders,
        },
        "sites": rows,
        "paidBy": paid_by(conn, entity, {r["slug"] for r in rows}),
        "credit": credit_facts(conn, entity["id"]),
    }
    if (stress := stress_summary(conn, entity["id"])) is not None:
        doc["stressSummary"] = stress
    return doc


@builder("payer", keys=payer_ids)
def build_payer(conn: Conn, key: str) -> Doc | None:
    """Payer for one tenant or end user (entity id); None for an entity that is not a payer."""
    entity = entity_row(conn, key)
    if entity is None or key not in payer_ids(conn):
        return None
    return payer_doc(conn, entity)
