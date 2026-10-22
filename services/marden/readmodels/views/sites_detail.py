"""Site detail header and Schedule tab: `site_detail` (GET /api/sites/:slug, SiteDetail) and
`site_schedule` (GET /api/sites/:slug/schedule, SiteSchedule), specs 04 and 16.

SiteDetail is the site's SiteSummary (views/sites.py: same chain, status, cushion and debt as the
lists) plus `buildings` (built / total on the linked campuses, as the lists count them), the
facts line ("2.46 GW · $18.0B debt · 4 buildings"), tracking names, tab counts (the same counts the
Evidence, Opposition and Material events tabs list), the main instrument that needs the site
finished (size, dealer quote R, Marden price and recovery from the price / recovery models, filed
holders) and the material events, one per development: reports of the same subject and development a
few days apart are one event with all its sources (views/sites_stories.py).
The Schedule tab's projection, milestones and cushion history come from the `cushion` model;
imagery from Sentinel-2 facts; documents are the latest filed records (F first).
"""

from __future__ import annotations

from datetime import date

from marden.core.db import Conn
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.instrument_detail import recovery as instrument_recovery
from marden.readmodels.views.instrument_inputs import latest_output as instrument_output
from marden.readmodels.views.instruments import marden_price as instrument_price
from marden.readmodels.views.instruments import marden_prices
from marden.readmodels.views.sites import building_count, site_summary
from marden.readmodels.views.sites_common import (
    Doc,
    chain_parties,
    day,
    dealer_quote,
    event_evidence,
    holders,
    holders_note,
    latest_output,
    main_instrument,
    nd,
    output_field,
    output_history,
    party_label,
    price_sourced,
    site_events,
    site_instruments,
    site_row,
    site_slugs,
    with_value,
)
from marden.readmodels.views.sites_debt_basis import debt_basis
from marden.readmodels.views.sites_links import imagery
from marden.readmodels.views.sites_milestones import with_dates
from marden.readmodels.views.sites_stories import material_rows
from marden.readmodels.views.sites_tabs import evidence_records, opposition_tab, site_event_log, story_key

RECOVERY_FACTS = ("recovery_waterfall", "recovery_project", "recovery_gpu")


def facts_line(summary: Doc, buildings: Doc | None) -> str:
    """'2.46 GW · $18.0B debt · 4 buildings' (spec 16: the header names the linked campus's buildings,
    counted as the Sites and Completion lists count them: views/sites.building_count)."""
    parts = []
    if summary["gw"]["value"]:
        parts.append(summary["gw"]["value"])
    debt = summary["debtB"]["value"]
    if isinstance(debt, int | float):
        parts.append(f"${debt:,.1f}B debt")
    total = ((buildings or {}).get("value") or {}).get("total")
    if isinstance(total, int) and total > 0:
        parts.append(f"{total} building{'s' if total != 1 else ''}")
    return " · ".join(parts) or "n/d"


_story_key = story_key  # one key per story (views/sites_tabs.story_key)


def marden_price(conn: Conn, instrument_id: str) -> Doc:
    """The instrument's Marden price exactly as its own page shows it (views/instruments.py): the
    `price` model's output, else the model's daily `marden` price series; n/d (M) before either."""
    price_out = instrument_output(conn, "price", instrument_id, "instrument")
    return instrument_price(price_out, marden_prices(conn, instrument_id))


def recovery_value(conn: Conn, instrument_id: str) -> Doc:
    """Recovery if the tenant walks, as the instrument page shows it (views/instrument_detail.py):
    the recovery model, else the price model's recovery, else the filed / research recovery ranges."""
    facts = known_facts(conn, "instrument", instrument_id, RECOVERY_FACTS)
    return instrument_recovery(conn, instrument_id, instrument_output(conn, "price", instrument_id, "instrument"),
                               facts)


def debt_exposed(conn: Conn, instruments: list[Doc]) -> Doc | None:
    inst = main_instrument(instruments)
    if inst is None:
        return None
    amount = inst["facts"].get("amount_usd")
    size = with_value(amount, round(amount["value"] / 1e9, 3)) if amount and isinstance(
        amount["value"], int | float) else nd("R")
    doc: Doc = {
        "instrumentId": inst["id"], "name": inst["name"], "sizeB": size,
        "mardenPrice": marden_price(conn, inst["id"]),
        "recovery": recovery_value(conn, inst["id"]),
        "holdersNote": holders_note(holders(conn, [inst["id"]])),
    }
    quote_row = dealer_quote(conn, inst["id"])
    quote = price_sourced(quote_row, as_text=True)
    if quote is not None:
        doc["dealerQuote"] = quote
    slug = inst.get("site_slug") or ""
    doc["basis"] = debt_basis(conn, slug, inst, doc["mardenPrice"], quote_row, doc["recovery"],
                              party_label(chain_parties(conn, slug), "tenant"))
    return doc


def site_detail(conn: Conn, slug: str) -> Doc | None:
    site = site_row(conn, slug)
    if site is None:
        return None
    instruments = site_instruments(conn, slug)
    summary = site_summary(conn, site, instruments=instruments)
    buildings = building_count(conn, slug)
    material = material_rows(conn, site, site_events(conn, slug))  # one row per development
    return {
        **summary,
        "buildings": buildings,
        "facts": facts_line(summary, buildings),
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
    dated, model = with_dates(conn, slug, cushion_out, milestones if isinstance(milestones, list) else [])
    doc: Doc = {
        "projection": projection if isinstance(projection, dict) and projection.get("p50") else None,
        "milestones": dated,
        "cushionHistory": cushion_history(conn, slug),
        "imagery": imagery(conn, slug)[-3:],
        "documents": latest_documents(conn, slug),
    }
    if model is not None:
        doc["cushionModel"] = model  # run day, simulations, method (sites_milestones)
    return doc


@builder("site_schedule", keys=site_slugs)
def build_site_schedule(conn: Conn, slug: str) -> Doc | None:
    return site_schedule(conn, slug)
