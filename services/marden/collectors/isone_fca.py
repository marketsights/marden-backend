"""ISO-NE Forward Capacity Auction (FCA) results -> capacity series facts (tag F, source ISO New England).

Documents (all public static files, stored raw before parsing; see isone_fca_parse):
- the FCA results report (fca-results-report.pdf, one section per FCA): each capacity zone's payment rate in
  $/kW-month, the Capacity Commitment Period and the day the auction ran;
- the FCA capacity obligations workbook (fca_obligations.xlsx, one sheet per FCA): every resource's Capacity
  Supply Obligation by capacity zone and load zone;
- the key stats markets page (links each FCA's results press release; its "Total Capacity Acquired" checks
  the obligation sums) and each press release (the day the results were announced = valid_from).
Subjects 'capacity:isone:<zone>:<period>' for FCA FIRST_FCA onward, zone ROP (Rest-of-Pool), SENE (Southeast
New England), NNE (Northern New England), MAINE (nested in NNE) as modeled in that FCA; period is the
commitment period ('2027-2028' for FCA 18). Fields: clearing_price (USD/MW-day = payment rate x 1000 / 30.4167,
the conversion in quote_loc), clearing_price_native (USD/kW-month, the report's sentence as quote), cleared_mw
(sum of the zone's CSOs in the period's first month, from the obligations workbook), load_zones (the load zones
of the resources in the zone; capacity_zones uses them to place sites by state), auction_name ('FCA 18'),
auction_date, delivery_start, delivery_end. A zone priced by "The system closed ..." gets that rate.
ISO-NE has announced a prompt/seasonal capacity auction to replace the FCA (first commitment period 2028/2029);
its results are not posted yet, and a report section this parser does not know is listed in the run meta.
The ISO Express FCA report tree is not used (its bulk download sits behind a CAPTCHA).

After the load: capacity_zones runs, and V1 sites in an ISO-NE zone get a `capacity_auction` event for the zone's
latest FCA. Cadence: monthly (4th, 14:53 UTC). `python -m marden collect isone_fca`.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from typing import Any

from marden import cli
from marden.collectors import capacity_zones
from marden.collectors.capacity_common import (
    MW,
    USD_KW_MONTH,
    USD_MW_DAY,
    Auction,
    CapacityWriter,
    Value,
    ZoneResult,
    conversion_note,
    emit_auction_events,
    kw_month_to_mw_day,
    period_key,
)
from marden.collectors.isone_fca_parse import (
    KEYSTATS_URL,
    OBLIGATIONS_URL,
    REPORT_URL,
    Obligations,
    Section,
    keystats_totals,
    parse_obligations,
    parse_report,
    press_releases,
    release_date,
    unknown_sections,
)
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "isone_fca"
SOURCE_NAME = "ISO New England"
PRODUCER = f"collector:{SOURCE_ID}"
HOST = "www.iso-ne.com"
FIRST_FCA = 15
CRON_UTC = "53 14 4 * *"  # the 4th of each month, 14:53 UTC
EVENT_MARKETS = {"isone": "ISO-NE"}
MW_CHECK_TOLERANCE = Decimal(1)  # MW: the key stats totals are rounded to whole MW
TERMS_URL = "https://www.iso-ne.com/legal-privacy"
TERMS_NOTE = (
    f"Checked 2026-10-21. ISO New England Terms and Conditions ({TERMS_URL}): \"The posting of documents on this "
    "website is done for the convenience of ISO New England's market participants and other interested visitors "
    "to the website.\"; \"You are also hereby put on notice that the Content is protected by copyright under "
    "United States laws. Any duplication of the Content or non-personal use may violate copyright, trademark, "
    "and other laws.\"; \"You may not use the website in any manner that could damage, disable, overburden or "
    "impair any ISO New England server(s) ... You may not obtain or attempt to obtain any materials or "
    "information through any means not intentionally made available through this Web site.\"; excessive "
    "bandwidth or concurrent requests are throttled by IP; \"To request permission to use a graph, chart, or "
    "other image on this website, please email info@iso-ne.com.\" No clause forbids automated access; "
    "www.iso-ne.com serves no robots.txt (404). Use: auction results (prices, MW) as facts with citation to "
    "ISO-NE; the documents are stored for audit, never redistributed, and no ISO-NE graph or image is shown "
    "(the copyright notice covers the documents, not the auction facts; a person should review before the "
    "documents themselves are shown). Access: four static files and the press releases the key stats page "
    "links, once a month, 0.5 requests/second; not the ISO Express tree, whose bulk download is CAPTCHA-gated."
)
SOURCE = declare_source(SOURCE_ID, "ISO New England capacity auctions (FCA)", "portal", f"https://{HOST}",
                        31 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 0.5)


def _doc(url: str, doc_type: str, title: str, stats: RunStats, fetcher: Fetcher | None) -> DocumentRow:
    doc = fetch_document(SOURCE_ID, url, doc_type=doc_type, title=title, fetcher=fetcher)
    stats.document(doc)
    return doc


def build_auction(section: Section, obligations: Obligations | None, report: DocumentRow,
                  obl_doc: DocumentRow | None, published: tuple[Any, str]) -> Auction:
    """One FCA's zones: price from the report, MW and load zones from the obligations workbook."""
    fca = section.fca
    zones = sorted(set(section.prices) | set(obligations.zones if obligations else ()))
    if not zones and section.system_price is None:
        raise ParseError(f"FCA {fca}: no capacity zones")
    auction = Auction(
        iso="isone", period=period_key(section.ccp_start.year), name=f"FCA {fca}",
        delivery_start=section.ccp_start, delivery_end=section.ccp_end, published=published[0],
        published_loc=published[1], source_url=report.url, document_id=report.id,
        auction_date=section.auction_date,
        auction_date_loc=f"fca-results-report.pdf p.{section.page}: \"{section.auction_date_quote}\"",
    )
    for code in zones:
        priced = section.prices.get(code) or section.system_price
        if priced is None:
            raise ParseError(f"FCA {fca}: zone {code} holds obligations but the report gives it no payment rate")
        rate, sentence = priced
        loc = f"fca-results-report.pdf p.{section.page}ff (FCA {fca} section)"
        values = {
            "clearing_price": Value(kw_month_to_mw_day(rate), USD_MW_DAY, f"{conversion_note(rate)}; {loc}", sentence),
            "clearing_price_native": Value(rate, USD_KW_MONTH, loc, sentence),
        }
        zone = obligations.zones.get(code) if obligations else None
        if zone is not None and obl_doc is not None:
            first = obligations.month.isoformat()
            values["cleared_mw"] = Value(
                zone.mw, MW, f"fca_obligations.xlsx '{obligations.sheet}': sum of the {first} CSO column over "
                f"{zone.resources} resources in Capacity Zone '{zone.name}' (summed by Marden)",
                source_url=obl_doc.url, document_id=obl_doc.id)
            values["load_zones"] = Value(
                zone.load_zones, None, f"fca_obligations.xlsx '{obligations.sheet}': Load Zone of the resources in "
                f"Capacity Zone '{zone.name}'", source_url=obl_doc.url, document_id=obl_doc.id)
        auction.zones.append(ZoneResult(code, values))
    return auction


def check_totals(obligations: dict[int, Obligations], totals: dict[int, Decimal], stats: RunStats) -> None:
    """The obligation sums must equal ISO-NE's published Total Capacity Acquired (key stats page)."""
    checked = {}
    for fca, obl in obligations.items():
        if fca not in totals:
            continue
        ours = sum((z.mw for z in obl.zones.values()), Decimal(0))
        if abs(ours - totals[fca]) > MW_CHECK_TOLERANCE:
            raise ParseError(f"FCA {fca}: obligations sum {ours} MW differs from ISO-NE's total {totals[fca]} MW")
        checked[str(fca)] = [float(ours), float(totals[fca])]
    stats.extra["total_mw_check"] = checked


def run(*, fetcher: Fetcher | None = None, first_fca: int = FIRST_FCA) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        keystats = _doc(KEYSTATS_URL, "isone_keystats_html", "ISO-NE Key Grid and Market Stats: Markets", stats,
                        fetcher)
        report = _doc(REPORT_URL, "fca_results_report_pdf", "Forward Capacity Auction Results report", stats, fetcher)
        obl_doc = _doc(OBLIGATIONS_URL, "fca_obligations_xlsx", "Forward Capacity Auction Capacity Obligations",
                       stats, fetcher)
        page = keystats.read()
        sections = parse_report(report.read(), first_fca=first_fca)
        obligations = parse_obligations(obl_doc.read(), first_fca=first_fca)
        check_totals(obligations, keystats_totals(page), stats)
        releases = press_releases(page)
        stats.extra.update(fcas=sorted(sections), loaded=[], published_fallback=[],
                           obligations_without_report=sorted(set(obligations) - set(sections)))
        stats.extra["unknown_section_pages"] = unknown_sections(report.read())  # a new auction design
        for fca, section in sorted(sections.items()):
            if fca in releases:
                pr = _doc(releases[fca], "isone_press_release_pdf", f"FCA {fca} results press release", stats,
                          fetcher)
                published = release_date(pr.read(), releases[fca].rsplit("/", 1)[-1])
            elif section.auction_date is not None:
                published = (section.auction_date, f"no results press release linked for FCA {fca}; the auction "
                             f"date from the report (p.{section.page}) is used")
                stats.extra["published_fallback"].append(fca)
            else:
                raise ParseError(f"FCA {fca}: no press release and no auction date for the publication date")
            auction = build_auction(section, obligations.get(fca), report, obl_doc, published)
            with db.transaction() as conn:
                CapacityWriter(conn, stats, source_name=SOURCE_NAME, producer=PRODUCER).write(auction)
            stats.extra["loaded"].append({
                "fca": fca, "period": auction.period, "published": auction.published.isoformat(),
                "zones": {z.zone: float(z.values["clearing_price"].value) for z in auction.zones}})
        with db.transaction() as conn:
            capacity_zones.assign(conn, stats)
            emit_auction_events(conn, EVENT_MARKETS, stats)

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="monthly_4th_1453_utc")
@app.task(name="collect.isone_fca", queue="collect", queueing_lock="collect.isone_fca", lock="collect.isone_fca")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", SOURCE_ID, help="ISO-NE Forward Capacity Auction results (FCA 15 on) -> capacity series")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
