"""Site detail Debt and holders and Lease terms tabs: `site_tab:debt` (SiteDebtTab) and
`site_tab:lease` (SiteLeaseTab) of the app's site-tab types (apps/api/src/contracts/site-tabs.ts), spec 04.

Debt: the SPV (first tracking name), the capital stack from the case research's tranche summary,
every instrument that needs the site finished plus the tenant's own V1 notes exposed to the site
(instrument `exposure_links`, role Tenant), each with its size (F/R/M as filed), the latest dealer
quote (R), the Marden price and recovery (price / recovery models; n/d until they run), the
recovery inputs the recovery model reports, the named lenders and filed holders.
Lease: the terms as filed or reported (parties, rent start, outside date, force majeure, rent, term,
termination, guarantee); a term nobody has sourced is n/d with where it will come from. What the
terms mean for lenders and today's position need the cushion model: n/d until it runs.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.sites_common import (
    INSTRUMENT_FIELDS,
    Doc,
    chain_parties,
    dealer_quote,
    holders,
    holders_note,
    latest_output,
    main_instrument,
    msourced,
    nd,
    output_field,
    party_label,
    price_sourced,
    site_instruments,
    site_row,
    site_slugs,
    subjects_listing,
    text_of,
    with_value,
)
from marden.readmodels.views.sites_detail import model_value, recovery_value

KIND_LABEL = {"bond": "Bond", "construction_loan": "Construction loan", "term_loan": "Term loan",
              "ddtl": "Delayed-draw term loan", "abs": "ABS", "facility": "Facility", "other": "Other"}
LOAN_KINDS = frozenset({"construction_loan", "term_loan", "ddtl", "facility"})
RECOVERY_INPUTS = (("shell_value", "Shell value"), ("equipment", "Equipment"), ("reletting_months", "Re-letting time"),
                   ("claims_ahead", "Claims ahead of the loan"))


def tenant_notes(conn: Conn, site: Row) -> list[Doc]:
    """The tenant's own V1 instruments exposed to this site (exposure_links role Tenant)."""
    case = site.get("legacy_group")
    if not case:
        return []
    candidates = subjects_listing(conn, "instrument", "exposure_links", {"case": case, "role": "Tenant"})
    rows = conn.execute(
        """SELECT * FROM instrument WHERE id = ANY(%s) AND in_v1 AND (site_slug IS NULL OR site_slug <> %s)
           ORDER BY id""",
        (candidates, site["slug"]),
    ).fetchall()
    out = []
    for r in rows:
        facts = known_facts(conn, "instrument", r["id"], INSTRUMENT_FIELDS)
        links = (facts.get("exposure_links") or {}).get("value") or []
        if any(isinstance(x, dict) and x.get("case") == case and x.get("role") == "Tenant" for x in links):
            out.append({**r, "facts": facts})
    return out


def instrument_sub(inst: Doc) -> str:
    facts = inst["facts"]
    parts = [KIND_LABEL.get(inst.get("kind") or "", (inst.get("kind") or "").replace("_", " ").capitalize())]
    for field, prefix in (("coupon", ""), ("maturity", "matures "), ("seniority", "")):
        if (row := facts.get(field)) and isinstance(row["value"], str | int | float):
            parts.append(f"{prefix}{row['value']}")
    return " · ".join(p for p in parts if p)


def instrument_row(conn: Conn, inst: Doc, payer: str | None = None) -> Doc:
    amount = inst["facts"].get("amount_usd")
    size = with_value(amount, round(amount["value"] / 1e9, 3)) if amount and isinstance(
        amount["value"], int | float) else nd("R")
    quote: Any = price_sourced(dealer_quote(conn, inst["id"]), as_text=True)
    if quote is None:
        quote = "private" if inst.get("kind") in LOAN_KINDS else ("traded" if inst.get("kind") == "bond" else None)
    price = model_value(conn, "price", inst["id"], "price")
    recovery = recovery_value(conn, inst["id"])
    return {"instrumentId": inst["id"], "payerSlug": payer, "name": inst["name"], "sub": instrument_sub(inst),
            "sizeB": size, "quote": quote, "mardenPrice": price, "recovery": recovery}


def capital_stack(conn: Conn, slug: str) -> list[Doc]:
    row = known_facts(conn, "site", slug, ["tranche_summary"]).get("tranche_summary")
    items = row["value"] if row and isinstance(row["value"], list) else []
    out = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("amount_usd"), int | float):
            continue
        kind = "equity" if "equity" in str(item.get("seniority") or item.get("tranche") or "").lower() else "loan"
        out.append({"label": str(item.get("tranche") or "n/d"), "kind": kind,
                    "amountB": with_value(row, round(item["amount_usd"] / 1e9, 3))})
    return out


def lenders(conn: Conn, slug: str) -> list[Doc]:
    facts = known_facts(conn, "site", slug, ["holders_text", "lenders"])
    out: list[Doc] = []
    if row := facts.get("holders_text"):
        v = row["value"] if isinstance(row["value"], dict) else {}
        for bank in v.get("banks") or []:
            out.append({"name": str(bank), "role": with_value(row, "Bank lender"), "share": nd("F")})
        for fund in v.get("funds") or []:
            if isinstance(fund, dict) and fund.get("fund"):
                out.append({"name": str(fund["fund"]), "role": with_value(row, "Fund holder"), "share": nd("F")})
        for bdc in v.get("bdcs") or []:
            name = bdc.get("name") if isinstance(bdc, dict) else bdc
            if name:
                out.append({"name": str(name), "role": with_value(row, "BDC holder"), "share": nd("F")})
    if not out and (row := facts.get("lenders")) and isinstance(row["value"], str):
        out.append({"name": row["value"], "role": with_value(row, "Lenders"), "share": nd("F")})
    return out


def recovery_card(conn: Conn, parties: dict[str, Row], main: Doc | None) -> Doc:
    out = latest_output(conn, "recovery", "instrument", main["id"]) if main else None
    inputs_out = output_field(out, "inputs")
    inputs = []
    for key, label in RECOVERY_INPUTS:
        value = inputs_out.get(key) if isinstance(inputs_out, dict) else None
        item: Doc = {"label": label, "value": msourced(out, text_of(value) if not isinstance(value, dict) else value)}
        if item["value"]["value"] is None:
            item["pending"] = "recovery model"
        inputs.append(item)
    return {"ifWalks": party_label(parties, "tenant") or "n/d",
            "range": recovery_value(conn, main["id"]) if main else nd("M"), "inputs": inputs}


def debt_tab(conn: Conn, slug: str) -> Doc | None:
    site = site_row(conn, slug)
    if site is None:
        return None
    parties = chain_parties(conn, slug)
    instruments = site_instruments(conn, slug)
    tenant = parties.get("tenant")
    notes = tenant_notes(conn, site)
    main = main_instrument(instruments)
    names = list(site.get("tracking_names") or [])
    doc: Doc = {
        "capitalStack": capital_stack(conn, slug),
        "instruments": [instrument_row(conn, i) for i in instruments]
        + [instrument_row(conn, i, tenant["entity_id"] if tenant else None) for i in notes],
        "recovery": recovery_card(conn, parties, main),
        "lenders": lenders(conn, slug),
        "holdersNote": holders_note(holders(conn, [i["id"] for i in instruments])),
    }
    if names:
        doc = {"spvName": names[0], **doc}
    return doc


@builder("site_tab:debt", keys=site_slugs)
def build_debt(conn: Conn, slug: str) -> Doc | None:
    return debt_tab(conn, slug)


# ---------------------------------------------------------------- lease terms
LEASE_FACTS = ("lease_tenant", "rent_start", "outside_date", "lease_termination", "lease_annual_rent_usd",
               "lease_rent_basis", "lease_term_years", "lease_term", "lease_guarantee", "leases")


def _term(key: str, label: str, value: Doc, pending: str) -> Doc:
    term: Doc = {"key": key, "label": label, "value": value}
    if value["value"] is None:
        term["pending"] = pending
    return term


def _structure_term(conn: Conn, slug: str, *keys: str) -> Row | None:
    for row in known_fact_rows(conn, "site", slug, ["structure_term"])["structure_term"]:
        v = row["value"] if isinstance(row["value"], dict) else {}
        if str(v.get("key") or "").lower() in keys:
            return row
    return None


def _text(row: Row | None, tag: str = "F") -> Doc:
    if row is None:
        return nd(tag)
    return with_value(row, text_of(row["value"]))


def _lease_outside(facts: dict[str, Row]) -> Doc:
    if row := facts.get("outside_date"):
        return _text(row)
    leases = facts.get("leases")
    for item in (leases["value"] if leases and isinstance(leases["value"], list) else []):
        if isinstance(item, dict) and item.get("outside_date"):
            return with_value(leases, str(item["outside_date"]))
    return nd("F")


def _rent(facts: dict[str, Row]) -> Doc:
    if (row := facts.get("lease_annual_rent_usd")) and isinstance(row["value"], int | float):
        return with_value(row, f"${row['value'] / 1e9:,.2f}B a year")
    return _text(facts.get("lease_rent_basis"))


def _term_years(facts: dict[str, Row]) -> Doc:
    if (row := facts.get("lease_term_years")) and isinstance(row["value"], int | float):
        return with_value(row, f"{row['value']:g} years")
    return _text(facts.get("lease_term"))


def lease_tab(conn: Conn, slug: str) -> Doc | None:
    site = site_row(conn, slug)
    if site is None:
        return None
    facts = known_facts(conn, "site", slug, LEASE_FACTS)
    parties = chain_parties(conn, slug)
    tenant = party_label(parties, "tenant")
    fm = _structure_term(conn, slug, "force majeure")
    terms = [
        _term("parties", "Parties", _text(facts.get("lease_tenant")), "lease or tenant filing"),
        _term("rent_start", "Rent start", _text(facts.get("rent_start")), "lease or landlord filing"),
        _term("outside_date", "Outside date", _lease_outside(facts), "lease, offering memorandum or credit agreement"),
        _term("force_majeure", "Force majeure", _text(fm), "lease or tenant notice"),
        _term("rent", "Rent", _rent(facts), "lease or landlord filing"),
        _term("term", "Term", _term_years(facts), "lease or landlord filing"),
        _term("termination", "Termination rights", _text(facts.get("lease_termination")), "lease"),
        _term("guarantee", "Parent guarantee", _text(facts.get("lease_guarantee")), "lease or guaranty"),
    ]
    cushion = latest_output(conn, "cushion", "site", slug)
    today = output_field(cushion, "leasePosition")
    return {
        "title": f"{tenant}'s lease" if tenant else "Lease",
        "terms": terms,
        "outcomes": [o for o in (output_field(cushion, "leaseOutcomes") or []) if isinstance(o, dict)],
        "today": msourced(cushion, today) if today is not None else nd("M"),
    }


@builder("site_tab:lease", keys=site_slugs)
def build_lease(conn: Conn, slug: str) -> Doc | None:
    return lease_tab(conn, slug)
