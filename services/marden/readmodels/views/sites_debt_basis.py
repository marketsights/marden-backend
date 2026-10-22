"""What the "Debt that needs this site finished" numbers are (spec 04; SiteDetail.debtExposed.basis and
SiteDebtTab.basis, DebtBasis in the app's types), for the site's main instrument, read from its facts:

- size: what the amount is, as its facts label it: drawn (`amount_basis` 'drawn' or a `drawn_usd` fact
  equal to it), par held by filers ('holder_par'), committed (a `commitment_usd` fact equal to it), else
  'Facility size as reported'. `reported` quotes the clause that states the amount in the site's
  research (`debt_items` naming the instrument), with that record's source; `borrower` is the site's
  `debt_facility` borrower; `tranches` is what the filing cited for the amount itself says
  (`structure_term` facts from that document), so a size taken from press is not read as filed.
- quote: the dealer quote's record (date, source, tag); bid and ask are n/d unless a record states
  them; a range of quotes in the same report ('89 to 91 cents') is shown as the range, and the quote
  as its mid only when it is that range's midpoint.
- model: the Marden price and what it prices (the instrument on its own terms).
- vsQuote: 'Model value: 85.9; -4.1 points vs dealer quote (Sep 18)', computed from the two values.
- recovery: the recovery model's number (derived; price v0.2 prices with it), else the price model's
  default recovery for the collateral (an assumption, said so), else the research archive's estimate, else
  n/d; with the debt class, the scenario, the assumptions, the method page, and (views/recovery_detail) the
  key inputs with their sources (`lines`) and the price change per 10c of recovery (`sensitivity`).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows
from marden.readmodels.views.instrument_inputs import latest_output as instrument_output
from marden.readmodels.views.instrument_inputs import method_version, num
from marden.readmodels.views.recovery_detail import detail as recovery_detail
from marden.readmodels.views.recovery_text import NO_SITE_MODEL, SITE_MODEL
from marden.readmodels.views.sites_common import Doc, nd, output_field, with_value
from marden.readmodels.views.sites_recovery import reason_nd

MINUS, DASH = "\u2212", "\u2013"
MONEY = re.compile(r"\$\s?\d[\d.,]*\s*(?:billion|bn|b)\b", re.I)
RANGE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*(?:to|-|\u2013)\s*(\d{2,3}(?:\.\d+)?)\s*(?:cents|c\b)", re.I)
PRICING_DOC = "docs/methods/pricing-v0.1.md"
RECOVERY_DOC = "docs/methods/recovery-v0.1.md"


def short_date(d: date) -> str:
    return f"{d:%b} {d.day}"


def _rows(conn: Conn, slug: str, field: str) -> list[Row]:
    return known_fact_rows(conn, "site", slug, [field])[field]


def research_item(conn: Conn, slug: str, instrument_id: str) -> tuple[Row, dict[str, Any]] | None:
    """The site research's debt item that names the instrument (`debt_items`)."""
    for row in _rows(conn, slug, "debt_items"):
        for item in row["value"] if isinstance(row["value"], list) else []:
            if isinstance(item, dict) and instrument_id in (item.get("instrument_ids") or []):
                return row, item
    return None


def _item_sourced(row: Row, item: dict[str, Any], text: str | None) -> Doc:
    out = with_value(row, text)
    if item.get("source_url"):
        out["sourceUrl"] = item["source_url"]
    return out


def size_label(facts: dict[str, Row]) -> str:
    amount = num((facts.get("amount_usd") or {}).get("value"))
    basis = (facts.get("amount_basis") or {}).get("value")
    if basis == "drawn" or (amount is not None and num((facts.get("drawn_usd") or {}).get("value")) == amount):
        return "Drawn amount"
    if basis == "holder_par":
        return "Par held by filers"
    if amount is not None and num((facts.get("commitment_usd") or {}).get("value")) == amount:
        return "Committed facility size"
    return "Facility size as reported"


def size_basis(conn: Conn, slug: str, inst: Doc, research: tuple[Row, dict[str, Any]] | None) -> Doc:
    facts = inst["facts"]
    amount = facts.get("amount_usd")
    clause = None
    if research is not None:
        parts = re.split(r";\s+|\.\s+(?=[A-Z\"])", str(research[1].get("quote") or ""))
        clause = next((p.strip() for p in parts if MONEY.search(p)), None)
    reported = _item_sourced(*research, clause) if research is not None and clause else (
        with_value(amount, f"${amount['value'] / 1e9:,.1f}B") if amount and num(amount["value"]) else nd("F"))
    doc: Doc = {"label": size_label(facts), "reported": reported, "borrower": nd("F"), "tranches": nd("F")}
    for row in _rows(conn, slug, "debt_facility"):
        v = row["value"] if isinstance(row["value"], dict) else {}
        if v.get("borrower") and (amount is None or num(v.get("size_usd")) in (None, num(amount["value"]))):
            doc["borrower"] = with_value(row, str(v["borrower"]))
            break
    url = (amount or {}).get("source_url")
    said = [str(r["value"].get("text")) for r in _rows(conn, slug, "structure_term")
            if url and r.get("source_url") == url and isinstance(r["value"], dict) and r["value"].get("text")]
    if said and amount is not None:
        doc["tranches"] = with_value(amount, " ".join(said))
    return doc


def quote_basis(inst: Doc, quote: Row | None, research: tuple[Row, dict[str, Any]] | None) -> Doc | None:
    if quote is None:
        return None
    price = float(quote["price"])
    src: Doc = {"value": round(price, 4), "tag": quote["tag"], "asOf": quote["obs_date"].isoformat(),
                "sourceName": quote["source_name"]}
    if quote.get("source_url"):
        src["sourceUrl"] = quote["source_url"]
    doc: Doc = {"refersTo": f"{inst['name']} ({inst['id']}); the quote record names no tranche",
                "price": src, "bid": nd(quote["tag"]), "ask": nd(quote["tag"]), "mid": nd(quote["tag"]),
                "range": nd(quote["tag"])}
    same_report = research is not None and research[1].get("source_url") == quote.get("source_url")
    found = RANGE.search(str(research[1].get("quote") or "")) if research is not None and same_report else None
    if found and research is not None:
        lo, hi = float(found.group(1)), float(found.group(2))
        doc["range"] = _item_sourced(*research, f"{found.group(1)}{DASH}{found.group(2)}")
        if abs((lo + hi) / 2 - price) < 0.05:
            doc["mid"] = src
            doc["note"] = (f"The report gives a range of quotes, {found.group(1)}{DASH}{found.group(2)}, not a bid "
                           f"and an ask; {price:.1f} is its midpoint.")
    doc.setdefault("note", "The quote record carries one price; no bid or ask is on file.")
    return doc


def vs_quote(model: Doc, quote: Doc | None) -> str | None:
    mp = num(model.get("value"))
    if quote is None or mp is None:
        return None
    gap = round(mp - quote["price"]["value"], 1)
    sign = MINUS if gap < 0 else "+" if gap > 0 else ""
    when = short_date(date.fromisoformat(quote["price"]["asOf"]))
    return f"Model value: {mp:.1f}; {sign}{abs(gap):.1f} points vs dealer quote ({when})"


def debt_class(inst: Doc) -> str:
    seniority = (inst["facts"].get("seniority") or {}).get("value")
    rank = str(seniority).split(",")[0].strip().lower() if seniority else ""
    return " ".join(p for p in (rank, str(inst.get("name") or "debt").lower()) if p)


def recovery_basis(conn: Conn, inst: Doc, recovery: Doc, tenant: str | None) -> Doc:
    """How the recovery shown for the instrument is reached, and whether it is derived or assumed."""
    price_out = instrument_output(conn, "price", inst["id"], "instrument")
    rec_out = instrument_output(conn, "recovery", inst["id"], "instrument")
    chain = output_field(price_out, "basis", "chain") or {}
    collateral = chain.get("collateral") if isinstance(chain, dict) else None
    who = tenant or (chain.get("tenantLabel") if isinstance(chain, dict) else None) or "the tenant"
    klass = debt_class(inst)
    doc: Doc = {"debtClass": (inst["facts"].get("seniority") or {}).get("value") or "n/d",
                "scenario": f"{who} walks and the loan defaults; lenders are repaid from the collateral"
                            + (f" ({collateral})" if collateral else ""),
                "value": recovery}
    info = recovery_detail(conn, inst["id"], price_out, rec_out)
    if info is not None:  # the key inputs with their sources, and how much the recovery moves the price
        doc |= {"lines": [x for x in info["lines"] if x["label"] != "Not counted"],  # in the assumptions below
                "sensitivity": info["sensitivity"]}
    source = recovery.get("sourceName")
    cents = num(output_field(price_out, "recovery"))
    pct = None if cents is None else round(cents * 100 if cents <= 1 else cents)
    if recovery.get("value") and source == "Marden recovery model":
        method = output_field(rec_out, "method")
        shown = f"{pct}c" if pct is not None else str(recovery["value"]).replace(f" ({SITE_MODEL})", "")
        used = ", used in the Marden price" if output_field(price_out, "recoveryBasis") == "site_waterfall" else ""
        doc |= {"basis": "derived", "headline": f"Derived: {shown} on {klass} ({SITE_MODEL}{used})",
                "assumptions": [f"Method: {str(method).replace('_', ' ')}" if method else "Method: recovery model",
                                *[f"Not counted: {x}" for x in output_field(rec_out, "basis", "notInV0") or []]],
                "method": {**recovery, "value": "Recovery " + (f"v{rec_out['version']}" if rec_out else "model"),
                           "sourceUrl": RECOVERY_DOC}}
        return doc
    not_derived = reason_nd(rec_out).removeprefix("Not derived: ")
    if source == "Marden price model" and pct is not None:
        on = {"buildings": "flat on the buildings", "gpus": "on the GPUs, decaying with the chips' half-life"}.get(
            str(collateral), f"on the {collateral}" if collateral else "on the collateral")
        version = method_version(price_out) if price_out is not None else "price"
        doc |= {"basis": "assumption", "headline": f"Assumption, not derived: {pct}c on {klass} ({NO_SITE_MODEL})",
                "assumptions": [f"{pct}% of par recovered at default, {on}: the price model's default, used only "
                                "where no site collateral model gives a number", f"Not derived from this site: "
                                f"{not_derived}"],
                "method": {**recovery, "value": f"Pricing {version.removeprefix('price ')}, facility recovery",
                           "sourceUrl": PRICING_DOC}}
        return doc
    if recovery.get("value"):
        doc |= {"basis": "archive", "headline": f"Research estimate, not derived by Marden's models: "
                                                f"{recovery['value']} on {klass}",
                "assumptions": [f"Not derived by the recovery model: {not_derived}"], "method": recovery}
        return doc
    doc |= {"basis": "nd", "headline": f"n/d: {not_derived}", "assumptions": [], "method": nd("M")}
    return doc


def debt_basis(conn: Conn, slug: str, inst: Doc, model: Doc, quote: Row | None, recovery: Doc,
               tenant: str | None) -> Doc:
    research = research_item(conn, slug, inst["id"])
    q = quote_basis(inst, quote, research)
    terms = [str(inst["facts"][f]["value"]) for f in ("coupon", "maturity") if inst["facts"].get(f)]
    return {
        "instrument": f"{inst['name']} ({inst['id']})",
        "size": size_basis(conn, slug, inst, research),
        "quote": q,
        "model": {"refersTo": f"{inst['name']} ({inst['id']})" + (f" on its terms: {', '.join(terms)}" if terms
                                                                   else ""), "price": model},
        "vsQuote": vs_quote(model, q),
        "recovery": recovery_basis(conn, inst, recovery, tenant),
    }
