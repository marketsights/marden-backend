"""PJM Reliability Pricing Model (RPM) auction results -> capacity series facts (tag F, source PJM). See capacity_common.

Auctions: every Base Residual Auction (BRA) and Incremental Auction (1st/2nd/3rd IA) whose results workbook the
RPM page (https://www.pjm.com/markets-and-operations/rpm) links, delivery year 2020/2021 onward (pjm_rpm_parse).
Each run stores the page, then each auction's results workbook (.xlsx) and its report (.pdf), raw first; the
workbook gives the numbers, the report the publication date (valid_from) and, where stated, the date the
auction window opened (auction_date). Subjects 'capacity:pjm:<lda or zone>:<period>' (period '2027-2028' for a
BRA, '2026-2027-3ia' for an IA):
- each LDA (RTO, MAAC, EMAAC, SWMAAC, PS, PSNORTH, DPLSOUTH, PEPCO, ATSI, ATSI-CLEVELAND, COMED, BGE, PL, DAYTON,
  DEOK, DOM, JCPL as modeled): clearing_price = Resource Clearing Price (USD/MW-day), cleared_mw (BRA: total
  annual equivalent resources cleared; IA: sell offers cleared) and, for IAs, buy_bids_cleared_mw;
- each transmission zone (AE, AEP, APS, ... RECO): zonal_capacity_price (the adjusted zonal capacity price,
  USD/MW-day, what load in the zone pays per MW of obligation), zonal_ucap_obligation_mw, lda (the zone's most
  specific LDA, whose clearing price applies to resources in it);
- every subject: auction_name, delivery_start (June 1), delivery_end (May 31), auction_date when stated.
A zone code that is also an LDA (DOM, COMED, BGE ...) carries both sets of fields.
After the load: capacity_zones assigns sites, campuses and plants to zones, and each V1 site in a PJM zone gets a
`capacity_auction` event for the zone's latest auction (capacity_common.emit_auction_events).

A workbook or report that fails to fetch or parse fails the run (recorded on the source) after the other auctions
are loaded; nothing is written for it. Cadence: monthly (2nd, 13:53 UTC); BRAs are annual and IAs a few times a
year, and the results post on PJM's published schedule. `python -m marden collect pjm_rpm [--year 2027 ...]`.
"""

from __future__ import annotations

import argparse
from datetime import date
from typing import Any

from marden import cli
from marden.collectors import capacity_zones
from marden.collectors.capacity_common import Auction, CapacityWriter, ZoneResult, delivery_year, emit_auction_events
from marden.collectors.pjm_queue import HOST, TERMS_URL
from marden.collectors.pjm_rpm_parse import PAGE_URL, Listed, find_auctions, parse_results, report_dates, zone_results
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats
from marden.core import db, http
from marden.core.http import Fetcher, FetchError, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "pjm_rpm"
SOURCE_NAME = "PJM"
PRODUCER = f"collector:{SOURCE_ID}"
CRON_UTC = "53 13 2 * *"  # the 2nd of each month, 13:53 UTC
EVENT_MARKETS = {"pjm": "PJM"}
TERMS_NOTE = (
    f"Checked 2026-10-21. PJM Legal & Privacy ({TERMS_URL}): \"Transmitting information to PJM member companies, "
    "partners, the electric industry, and the public is an important function of PJM.\"; \"Access to this website "
    "does not confer any license or ownership interest in either the form or content of the website ... PJM "
    "hereby expressly reserves such rights\"; \"use of PJM Trademarks and Logos by third parties is not generally "
    "permitted except through express written PJM authorization\". No clause on automated access. robots.txt "
    "(https://www.pjm.com/robots.txt) disallows only /xsl/, /temp/, /sitecore and admin paths; /markets-and-"
    "operations/ and /-/media/ are allowed. Use: the auction results (prices, MW) as facts with citation to PJM; "
    "PJM's documents are stored for audit, not redistributed; no PJM logos. Access: the public RPM page and the "
    "result workbooks/reports it links (no login; Data Miner 2 is not used), once a month, 0.5 requests/second."
)
SOURCE = declare_source(SOURCE_ID, "PJM capacity auctions (RPM)", "portal", PAGE_URL, 31 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 0.5)  # the same limit pjm_queue declares for www.pjm.com


def _name(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def load_auction(listed: Listed, stats: RunStats, *, fetcher: Fetcher | None = None) -> Auction:
    """Fetch (raw first) and parse one auction, write its facts in one transaction. Returns the auction."""
    if listed.report_url is None:
        raise ParseError(f"{listed.name}: the RPM page links no report next to {_name(listed.results_url)}")
    workbook = fetch_document(
        SOURCE_ID, listed.results_url, doc_type="rpm_results_xlsx", title=f"{listed.name} Results",
        external_id=listed.period, meta={"period": listed.period, "auction": listed.auction}, fetcher=fetcher,
    )
    stats.document(workbook)
    report = fetch_document(
        SOURCE_ID, listed.report_url, doc_type="rpm_results_report_pdf", title=f"{listed.name} Report",
        external_id=listed.period, meta={"period": listed.period, "auction": listed.auction}, fetcher=fetcher,
    )
    stats.document(report)
    dates = report_dates(report.read(), _name(listed.report_url))
    start, end = delivery_year(listed.start_year)
    if not date(listed.start_year - 5, 1, 1) <= dates.published <= end:
        raise ParseError(f"{listed.name}: publication date {dates.published} is implausible ({dates.published_loc})")
    results = parse_results(workbook.read(), listed, _name(listed.results_url))
    auction = Auction(
        iso="pjm", period=listed.period, name=listed.name, delivery_start=start, delivery_end=end,
        published=dates.published, published_loc=dates.published_loc, source_url=listed.results_url,
        document_id=workbook.id, auction_date=dates.opened,
        auction_date_loc=f"auction window opened: {dates.opened_loc}" if dates.opened_loc else None,
        zones=[ZoneResult(code, vals) for code, vals in sorted(zone_results(results).items())],
    )
    with db.transaction() as conn:
        CapacityWriter(conn, stats, source_name=SOURCE_NAME, producer=PRODUCER).write(auction)
    return auction


def run(*, years: list[int] | None = None, fetcher: Fetcher | None = None) -> dict[str, Any]:
    """One collection run (see module docstring). `years`: only these delivery years (start year)."""

    def body(stats: RunStats) -> None:
        page = fetch_document(SOURCE_ID, PAGE_URL, doc_type="rpm_page_html", title="PJM Capacity Market (RPM)",
                              fetcher=fetcher)
        stats.document(page)
        listed, skipped = find_auctions(page.read())
        if years:
            listed = [x for x in listed if x.start_year in set(years)]
        stats.extra.update(auctions_listed=len(listed), skipped_files=skipped, loaded=[], failed={})
        for item in listed:
            try:
                auction = load_auction(item, stats, fetcher=fetcher)
            except (FetchError, ParseError) as exc:
                stats.extra["failed"][item.period] = f"{type(exc).__name__}: {exc}"[:500]
                continue
            stats.extra["loaded"].append(
                {"period": auction.period, "name": auction.name, "published": auction.published.isoformat(),
                 "rto_clearing_price": _rto_price(auction)})
        with db.transaction() as conn:
            capacity_zones.assign(conn, stats)
            emit_auction_events(conn, EVENT_MARKETS, stats)
        if stats.extra["failed"]:
            raise ParseError(f"{len(stats.extra['failed'])} auction(s) not loaded: {sorted(stats.extra['failed'])}")

    return collect(SOURCE, body)


def _rto_price(auction: Auction) -> float | None:
    rto = next((z for z in auction.zones if z.zone == "RTO"), None)
    value = rto.values.get("clearing_price") if rto else None
    return float(value.value) if value else None


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="monthly_2nd_1353_utc")
@app.task(name="collect.pjm_rpm", queue="collect", queueing_lock="collect.pjm_rpm", lock="collect.pjm_rpm")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--year", type=int, action="append", dest="years",
                   help="only this delivery year, by its first year (repeatable; default: every listed year)")


@cli.action("collect", SOURCE_ID, help="PJM RPM capacity auction results (BRA and IAs) -> capacity series",
            configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(years=args.years))
