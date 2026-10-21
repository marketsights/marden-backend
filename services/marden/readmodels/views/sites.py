"""Site list views: `sites` (GET /api/sites, SiteSummary[]) and `completion` (GET /api/completion,
CompletionResponse), specs 03 and 05.

One SiteSummary per site is shared with `site_detail`, so status and cushion are the same value on
every screen (spec 05: one source). Chain links and status come from the `status` model, cushion,
projection, rent start and driver from the `cushion` model; until those models have run for a site
the values are n/d (status null, projection null, chain links grey with reason 'n/d'). Debt is the
sum of the site's instruments' sizes (F/R/M as filed); lead lenders the lenders link's label.
Non-US sites are left out of both lists (V1 is US only); `completion` also drops 'No build' sites.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.registry import builder
from marden.readmodels.views.sites_common import (
    Doc,
    chain_links,
    chain_parties,
    chain_status,
    day,
    debt_value,
    gw_value,
    is_us,
    known_facts,
    last_evidence,
    latest_output,
    output_field,
    party_label,
    site_county,
    site_events,
    site_instruments,
    site_rows,
)

SUMMARY_FACTS = ("gross_mw", "critical_it_mw")
BANDS = ("<0", "0-3", "3-6", "6-12", "12+")
DRIVERS = ("Power", "Build", "Delivery", "Permits", "Funding")


def cushion_months(projection: Any) -> float | None:
    months = projection.get("cushionMonths") if isinstance(projection, dict) else None
    value = months.get("value") if isinstance(months, dict) else None
    return value if isinstance(value, int | float) else None


def projection_of(cushion_out: Row | None) -> Doc | None:
    projection = output_field(cushion_out, "projection")
    return projection if isinstance(projection, dict) and projection.get("p50") else None


def rent_start_p50(cushion_out: Row | None, projection: Doc | None) -> str | None:
    explicit = output_field(cushion_out, "rentStartP50")
    if isinstance(explicit, str):
        return explicit
    return projection.get("p50") if projection else None


def site_summary(conn: Conn, site: Row, *, instruments: list[Doc] | None = None) -> Doc:
    """SiteSummary (types.ts) for one site, as known on the build date."""
    slug = site["slug"]
    facts = known_facts(conn, "site", slug, SUMMARY_FACTS)
    parties = chain_parties(conn, slug)
    status_out = latest_output(conn, "status", "site", slug)
    cushion_out = latest_output(conn, "cushion", "site", slug)
    projection = projection_of(cushion_out)
    months = projection.get("cushionMonths") if projection else None
    instruments = site_instruments(conn, slug) if instruments is None else instruments
    return {
        "slug": slug,
        "name": site["name"],
        "county": site_county(site),
        "state": site.get("state") or "n/d",
        "endUser": party_label(parties, "end_user") or "n/d",
        "tenant": party_label(parties, "tenant"),
        "developer": party_label(parties, "developer") or "n/d",
        "gw": gw_value(facts),
        "chain": chain_links(parties, status_out),
        "status": chain_status(status_out),
        "cushionMonths": months if isinstance(months, dict) else None,
        "rentStartP50": rent_start_p50(cushion_out, projection),
        "debtB": debt_value(instruments),
        "leadLenders": party_label(parties, "lenders") or "n/d",
    }


def _sort_key(summary: Doc) -> tuple:
    months = (summary.get("cushionMonths") or {}).get("value")
    no_build = summary.get("status") == "No build"
    debt = (summary.get("debtB") or {}).get("value") or 0
    return (no_build, months is None, months if months is not None else 0, -debt, summary["name"])


@builder("sites", many=True)
def build_sites(conn: Conn):
    """Every US site, cushion ascending; no-lease (no cushion) and no-build sites last."""
    rows = [site_summary(conn, s) for s in site_rows(conn) if is_us(s)]
    yield "all", sorted(rows, key=_sort_key)


# ---------------------------------------------------------------- completion
def band_of(months: float) -> str:
    if months < 0:
        return "<0"
    if months < 3:
        return "0-3"
    if months < 6:
        return "3-6"
    if months < 12:
        return "6-12"
    return "12+"


def completion_row(conn: Conn, site: Row, summary: Doc) -> Doc:
    cushion_out = latest_output(conn, "cushion", "site", site["slug"])
    driver = output_field(cushion_out, "driver")
    events = site_events(conn, site["slug"])
    return {
        "slug": site["slug"],
        "name": site["name"],
        "state": summary["state"],
        "chain": summary["chain"],
        "projection": projection_of(cushion_out),
        "driver": driver if driver in DRIVERS else None,
        "lastEvidence": last_evidence(conn, site["slug"], events),
        "debtB": summary["debtB"],
        "leadLenders": summary["leadLenders"],
    }


def completion_summary(rows: list[Doc]) -> Doc:
    """KPI cards, counted from the rows' model values (no cushion -> not counted)."""
    def debt(row: Doc) -> float:
        return (row["debtB"] or {}).get("value") or 0.0

    with_cushion = [(r, m) for r in rows if (m := cushion_months(r["projection"])) is not None]
    past = [r for r, m in with_cushion if m < 0]
    under3 = [r for r, m in with_cushion if 0 <= m < 3]
    slipped = []
    for r in rows:
        delta = ((r["projection"] or {}).get("deltaWeek") or {}).get("value")
        if isinstance(delta, int | float) and delta < 0:
            slipped.append((r, delta))
    slipped.sort(key=lambda pair: pair[1])
    by_band: dict[str, float] = {}
    for r, m in with_cushion:
        by_band[band_of(m)] = by_band.get(band_of(m), 0.0) + debt(r)
    week: Doc = {"count": len(slipped), "months": sum(d for _, d in slipped)}
    if slipped and slipped[0][0]["lastEvidence"]:
        week["record"] = slipped[0][0]["lastEvidence"]
    return {
        "pastOutside": _kpi(past, debt),
        "under3": _kpi(under3, debt),
        "slippedThisWeek": week,
        "debtByBand": [{"band": b, "debtB": round(by_band[b], 3)} for b in BANDS if b in by_band],
    }


def _kpi(rows: list[Doc], debt) -> Doc:
    return {"count": len(rows), "debtB": round(sum(map(debt, rows)), 3), "names": [r["name"] for r in rows]}


def _completion_key(row: Doc) -> tuple:
    months = cushion_months(row["projection"])
    debt = (row["debtB"] or {}).get("value") or 0
    return (months is None, months if months is not None else 0, -debt, row["name"])


@builder("completion", many=True)
def build_completion(conn: Conn):
    """Every US site with a build, tightest cushion first; sites with no lease date last (spec 03)."""
    rows = []
    for site in site_rows(conn):
        if not is_us(site):
            continue
        summary = site_summary(conn, site)
        if summary["status"] == "No build":
            continue
        rows.append(completion_row(conn, site, summary))
    rows.sort(key=_completion_key)
    yield "all", {"asOf": day().isoformat(), "summary": completion_summary(rows), "rows": rows}
