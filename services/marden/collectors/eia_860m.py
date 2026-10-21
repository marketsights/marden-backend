"""EIA-860M Preliminary Monthly Electric Generator Inventory -> planned-vs-actual history of generators.

Source: https://www.eia.gov/electricity/data/eia860m/ lists one workbook per month (`<month>_generator<year>.xlsx`;
the newest under /xls/, earlier ones under /archive/xls/). Each run reads robots.txt and the release page, then
fetches the newest BACKFILL_MONTHS files that robots.txt allows (raw first, conditional GET) and loads them oldest
first through `eia_860m_store` (generator history on subject_type 'eia_generator', facts on matched power_plant
rows, slip counts in run meta). A backfill file already loaded is not parsed again; the newest file always is, so
new plant matches are picked up. Links in HTML comments (placeholders for months not released) are ignored.

TERMS (checked 2026-10-21): see TERMS_NOTE. EIA's robots.txt disallows `/*archive/`, so today only the newest
file is fetched automatically; the earlier months are recorded as `robots_disallowed` in run meta. A person may
download them in a browser and load them with `--import-dir` (stored raw with their EIA URL; meta says so).
Cadence: weekly (EIA releases about the 24th of each month; spec 22 "Weekly: grid queue reports").
`python -m marden collect eia_860m [--import-dir DIR]`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from marden import cli
from marden.collectors.eia_860m_parse import ReportFile, list_files, parse_workbook, robots_allowed, robots_rules
from marden.collectors.eia_860m_store import report_date, write_report
from marden.collectors.rates_common import RunStats, collect, document_loaded, print_stats
from marden.core import db
from marden.core.config import get_settings
from marden.core.documents import DocumentRow, record_document
from marden.core.http import Fetcher, fetch_document, marden_user_agent
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source
from marden.seed.common import tag_for_url

SOURCE_ID = "eia_860m"
PAGE_URL = "https://www.eia.gov/electricity/data/eia860m/"
ROBOTS_URL = "https://www.eia.gov/robots.txt"
REUSE_URL = "https://www.eia.gov/about/copyrights_reuse.php"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BACKFILL_MONTHS = 24
CRON_UTC = "37 13 * * 5"  # Fridays 13:37 UTC: a release (about the 24th) is loaded within a week
TERMS_NOTE = (
    "Checked 2026-10-21. EIA Copyrights and Reuse (" + REUSE_URL + "): \"U.S. government publications are in the "
    "public domain and are not subject to copyright protection. You may use and/or distribute any of our data, "
    "files, databases, reports, graphs, charts, and other information products that are on our website or that "
    "you receive through our email distribution service. However, if you use or reproduce any of our information "
    "products, you should use an acknowledgment, which includes the publication date, such as: \"Source: U.S. "
    "Energy Information Administration (Oct 2008).\"\" robots.txt (" + ROBOTS_URL + "): \"User-agent: * Allow: /\" "
    "but \"Disallow: /*archive/\": the newest workbook (/electricity/data/eia860m/xls/) may be fetched; earlier "
    "months live under /electricity/data/eia860m/archive/xls/ and are not fetched automatically (robots.txt is "
    "re-read every run). Weekly, conditional GETs, 2 req/s host limit."
)
SOURCE = declare_source(SOURCE_ID, "EIA-860M monthly generator inventory", "feed", PAGE_URL, 7 * 24 * 60,
                        TERMS_NOTE)


def _agent_token() -> str:
    return marden_user_agent(get_settings()).split("/", 1)[0]


def _xlsx_document(source_id: str, f: ReportFile, fetcher: Fetcher | None) -> DocumentRow:
    return fetch_document(source_id, f.url, doc_type="eia860m_xlsx", title=f"EIA-860M {f.label}",
                          doc_date=report_date(f.year, f.month), external_id=f.name, fetcher=fetcher)


def _imported(source_id: str, files: list[ReportFile], import_dir: Path, stats: RunStats) -> dict[str, DocumentRow]:
    """Workbooks a person downloaded from the release page, stored raw under their EIA URL."""
    by_name = {f.name: f for f in files}
    out: dict[str, DocumentRow] = {}
    for path in sorted(import_dir.glob("*_generator*.xlsx")):
        f = by_name.get(path.name.lower())
        if f is None:
            stats.extra.setdefault("import_unlisted", []).append(path.name)
            continue
        doc = record_document(
            source_id, f.url, path.read_bytes(), content_type=XLSX, doc_type="eia860m_xlsx",
            title=f"EIA-860M {f.label}", doc_date=report_date(f.year, f.month), external_id=f.name,
            meta={"obtained": "downloaded by a person from the release page (robots.txt disallows /*archive/ "
                              "for automated fetches)", "file": path.name},
        )
        stats.document(doc)
        out[f.label] = doc
    return out


def run(*, source: SourceSpec = SOURCE, fetcher: Fetcher | None = None, months: int = BACKFILL_MONTHS,
        import_dir: Path | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        robots = fetch_document(source.id, ROBOTS_URL, doc_type="robots_txt", fetcher=fetcher)
        stats.document(robots)
        rules = robots_rules(robots.read().decode("utf-8", errors="replace"), _agent_token())
        page = fetch_document(source.id, PAGE_URL, doc_type="eia860m_release_page", title="EIA-860M release page",
                              fetcher=fetcher)
        stats.document(page)
        files, other_links = list_files(page.read().decode("utf-8", errors="replace"), PAGE_URL)
        window = files[-months:]
        newest = window[-1]
        backfill: dict[str, Any] = {"window": [window[0].label, newest.label], "listed": len(files),
                                    "outside_window": len(files) - len(window), "other_links_skipped": other_links,
                                    "robots_disallowed": [], "already_loaded": []}
        stats.extra["backfill"] = backfill
        docs = _imported(source.id, window, import_dir, stats) if import_dir else {}
        backfill["imported"] = sorted(docs)
        for f in window:
            if not robots_allowed(rules, urlsplit(f.url).path):
                if f.label not in docs:
                    backfill["robots_disallowed"].append(f.label)
                continue
            docs[f.label] = _xlsx_document(source.id, f, fetcher)
            stats.document(docs[f.label])
        if newest.label not in docs:
            return  # nothing loadable; the run meta says why
        reports: list[dict[str, Any]] = []
        stats.extra["reports"] = reports
        for f in window:
            doc = docs.get(f.label)
            if doc is None:
                continue
            with db.transaction() as conn:
                if f != newest and not doc.was_new and document_loaded(conn, doc.id):
                    backfill["already_loaded"].append(f.label)
                    continue
            report = parse_workbook(doc.read(), (f.year, f.month))
            with db.transaction() as conn:
                reports.append(write_report(conn, stats, report, doc, tag=tag_for_url(f.url)))

    return collect(source, body)


@app.periodic(cron=CRON_UTC, periodic_id="weekly")
@app.task(name="collect.eia_860m", queue="collect", queueing_lock="collect.eia_860m", lock="collect.eia_860m")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--import-dir", type=Path, default=None,
                        help="also load <month>_generator<year>.xlsx files a person downloaded from the release page")


@cli.action("collect", "eia_860m", help="EIA-860M generator inventory -> eia_generator history, plant facts",
            configure=_configure)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(import_dir=args.import_dir))
