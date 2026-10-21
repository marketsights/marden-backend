"""Site detail header and Schedule tab: `site_detail` (GET /api/sites/:slug, SiteDetail) and
`site_schedule` (GET /api/sites/:slug/schedule, SiteSchedule), specs 04 and 16.

SiteDetail is the site's SiteSummary (views/sites.py: same chain, status, cushion and debt as the
lists) plus the facts line ("2.46 GW · $18.0B debt · 4 buildings"), tracking names, tab counts
(the same counts the Evidence, Opposition and Material events tabs list), the main instrument that
needs the site finished (size, dealer quote R, Marden price and recovery from the price / recovery
models, filed holders) and the material events. The Schedule tab's projection, milestones and
cushion history come from the `cushion` model; imagery from Sentinel-2 facts; documents are the
latest filed records (F first).
"""

from __future__ import annotations

from datetime import date

from marden.core.db import Conn
from marden.readmodels.registry import builder
from marden.readmodels.views.sites import site_summary
from marden.readmodels.views.sites_common import (
    Doc,
    day,
    dealer_quote,
    event_evidence,
    holders,
    holders_note,
    latest_output,
    main_instrument,
    material_events,
    msourced,
    nd,
    output_field,
    output_history,
    price_sourced,
    severity,
    site_events,
    site_instruments,
    site_row,
    site_slugs,
    with_value,
)
from marden.readmodels.views.sites_links import campus_buildings, imagery, linked_campuses
from marden.readmodels.views.sites_tabs import evidence_records, opposition_tab, site_event_log


def facts_line(summary: Doc, buildings: int) -> str:
    parts = []
    if summary["gw"]["value"]:
        parts.append(summary["gw"]["value"])
    debt = summary["debtB"]["value"]
    if isinstance(debt, int | float):
        parts.append(f"${debt:,.1f}B debt")
    if buildings:
        parts.append(f"{buildings} buildings")
    return " · ".join(parts) or "n/d"


def model_value(conn: Conn, model: str, instrument_id: str, *path: str) -> Doc:
    out = latest_output(conn, model, "instrument", instrument_id)
    return msourced(out, output_field(out, *path))


def recovery_value(conn: Conn, instrument_id: str) -> Doc:
    """Recovery if the tenant walks: the recovery model, else the price model's recovery."""
    value = model_value(conn, "recovery", instrument_id, "recovery")
    if value["value"] is None:
        value = model_value(conn, "recovery", instrument_id, "range")
    if value["value"] is None:
        value = model_value(conn, "price", instrument_id, "recovery")
    if isinstance(value.get("value"), int | float):
        value = {**value, "value": f"{value['value']:g}"}
    return value


def debt_exposed(conn: Conn, instruments: list[Doc]) -> Doc | None:
    inst = main_instrument(instruments)
    if inst is None:
        return None
    amount = inst["facts"].get("amount_usd")
    size = with_value(amount, round(amount["value"] / 1e9, 3)) if amount and isinstance(
        amount["value"], int | float) else nd("R")
    doc: Doc = {
        "instrumentId": inst["id"], "name": inst["name"], "sizeB": size,
        "mardenPrice": model_value(conn, "price", inst["id"], "price"),
        "recovery": recovery_value(conn, inst["id"]),
        "holdersNote": holders_note(holders(conn, [inst["id"]])),
    }
    quote = price_sourced(dealer_quote(conn, inst["id"]), as_text=True)
    if quote is not None:
        doc["dealerQuote"] = quote
    return doc


def site_detail(conn: Conn, slug: str) -> Doc | None:
    site = site_row(conn, slug)
    if site is None:
        return None
    instruments = site_instruments(conn, slug)
    summary = site_summary(conn, site, instruments=instruments)
    campuses, _ = linked_campuses(conn, slug)
    n_buildings = len(campus_buildings(conn, campuses))
    events = site_events(conn, slug)
    material = []
    for ev, _ in material_events(conn, slug, events):
        item = {**event_evidence(ev), "severity": severity(ev)}
        if ev.get("rule_code"):
            item["ruleCode"] = ev["rule_code"]
        material.append(item)
    return {
        **summary,
        "facts": facts_line(summary, n_buildings),
        "trackingNames": list(site.get("tracking_names") or []),
        "tabCounts": {"evidence": len(evidence_records(conn, slug)),
                      "opposition": len(opposition_tab(conn, slug)["items"]),
                      "events": len(site_event_log(conn, slug))},
        "debtExposed": debt_exposed(conn, instruments),
        "materialEvents": material,
    }


def status_event(key: str, doc: Doc) -> Doc:
    """A changed site page is announced as the site's status event (types.ts StreamEvent 'status'):
    the app refetches the site on it."""
    return {"type": "status", "siteSlug": key, "chain": doc["chain"]}


@builder("site_detail", keys=site_slugs, event=status_event)
def build_site_detail(conn: Conn, slug: str) -> Doc | None:
    return site_detail(conn, slug)


# ---------------------------------------------------------------- schedule
def cushion_history(conn: Conn, slug: str) -> list[Doc]:
    """Weekly step chart since Jan 1. The cushion model's own read-side helper when the models package
    is installed (weekly points with what moved the cushion), else one point per change of its months."""
    try:
        from marden.models.cushion_output import cushion_history as model_history
    except ImportError:
        model_history = None
    if model_history is not None:
        return model_history(conn, slug, day())
    out: list[Doc] = []
    for row in output_history(conn, "cushion", "site", slug, date(day().year, 1, 1)):
        months = ((output_field(row, "projection") or {}).get("cushionMonths") or {}).get("value")
        if not isinstance(months, int | float) or (out and out[-1]["months"] == months):
            continue
        point: Doc = {"date": row["as_of"].isoformat(), "months": months}
        if isinstance(event := output_field(row, "event"), str):
            point["event"] = event
        out.append(point)
    return out


def latest_documents(conn: Conn, slug: str, n: int = 4) -> list[Doc]:
    """The n latest records on the site, filed records (F) first."""
    rows = [e for e in site_events(conn, slug) if e["kind"] != "catalyst"]
    filed = [e for e in rows if e["tag"] == "F"][:n]
    rest = [e for e in rows if e["tag"] != "F"][: n - len(filed)]
    return [event_evidence(e) for e in sorted(filed + rest, key=lambda e: e["event_date"], reverse=True)]


def site_schedule(conn: Conn, slug: str) -> Doc | None:
    if site_row(conn, slug) is None:
        return None
    cushion_out = latest_output(conn, "cushion", "site", slug)
    projection = output_field(cushion_out, "projection")
    milestones = output_field(cushion_out, "milestones")
    return {
        "projection": projection if isinstance(projection, dict) and projection.get("p50") else None,
        "milestones": milestones if isinstance(milestones, list) else [],
        "cushionHistory": cushion_history(conn, slug),
        "imagery": imagery(conn, slug)[-3:],
        "documents": latest_documents(conn, slug),
    }


@builder("site_schedule", keys=site_slugs)
def build_site_schedule(conn: Conn, slug: str) -> Doc | None:
    return site_schedule(conn, slug)
