"""ISO-NE interconnection request queue -> queue_project facts (tag F, source ISO-NE). See iso_queue.

The public queue is the IRTT "Generator Interconnection Queue / Public Queue" page,
https://irtt.iso-ne.com/reports/external (HTML table #publicqueue, "As of" date in the page). It is
stored raw and parsed in code. Its "Export to Excel" (/reports/exportpublicqueue) needs an IRTT login,
so it is not used. Types: G (generator), ETU (elective transmission upgrade), TS (transmission service);
ISO-NE publishes no large-load requests in this queue. Status A / C / W = Active / Commercial / Withdrawn
(the page's own queue-status filter). Fuel codes are EIA energy-source codes.

Cadence: the page is live (updated as requests change); collected weekly (Thursday).
`python -m marden collect isone_queue`.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import date
from typing import Any

from lxml import etree
from lxml import html as lhtml

from marden import cli
from marden.collectors.iso_queue import QueueEntry, QueueStore, month_or_date, number, text, us_date
from marden.collectors.rates_common import ET, ParseError, RunStats, collect, print_stats
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source
from marden.seed.common import slugify

SOURCE_ID = "isone_queue"
PRODUCER = "collector:isone_queue"
HOST = "irtt.iso-ne.com"
QUEUE_URL = f"https://{HOST}/reports/external"
CRON_UTC = "47 11 * * 4"  # Thursdays 11:47 UTC
TERMS_URL = "https://www.iso-ne.com/legal-privacy"
TERMS_NOTE = (
    f"Checked 2026-10-21. ISO New England Terms and Conditions ({TERMS_URL}): \"You may not use the website in "
    "any manner that could damage, disable, overburden or impair any ISO New England server(s) ... You may not "
    "obtain or attempt to obtain any materials or information through any means not intentionally made "
    "available through this Web site.\" The IRTT public queue page is intentionally public (no login); its "
    "Excel export requires an IRTT account and is not used. irtt.iso-ne.com serves no robots.txt (the path "
    "returns the app page). One page a week."
)
SOURCE = declare_source(SOURCE_ID, "ISO New England", "portal", f"https://{HOST}", 7 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 0.5)

HEADER = (
    "Cluster", "QP", "Updated", "Type", "Requested", "Alternative Name", "Unit", "Fuel Type", "Net MW",
    "Summer MW", "Winter MW", "County", "ST", "Op Date", "Sync Date", "W/D Date", "POI", "Serv", "SIS", "I39",
    "TO Report", "Dev", "Zone", "FS", "SIS", "OS", "FAC", "IA", "Project Status", "Status", "Jurisdiction",
)
COL = {name: i for i, name in reversed(list(enumerate(HEADER)))}  # first occurrence of a repeated name
STUDY_COLS = (("FS", 23), ("SIS", 24), ("OS", 25), ("FAC", 26), ("IA", 27))
TYPES = {"G": "Generator", "ETU": "Elective Transmission Upgrade", "TS": "Transmission Service"}
STATUS = {"A": "Active", "C": "Commercial", "W": "Withdrawn"}
FUEL = {  # EIA energy source codes used in the queue
    "SUN": "Solar", "BAT": "Battery", "WND": "Wind", "NG": "Natural Gas", "DFO": "Distillate Fuel Oil",
    "WAT": "Water", "WDS": "Wood", "LFG": "Landfill Gas", "NUC": "Nuclear", "BIT": "Bituminous Coal",
    "RFO": "Residual Fuel Oil", "JF": "Jet Fuel", "KER": "Kerosene", "MSW": "Municipal Solid Waste",
    "OBG": "Other Biomass Gas", "H2": "Hydrogen", "OTH": "Other", "MWH": "Energy Storage", "SUB": "Subbituminous Coal",
}
_AS_OF = re.compile(r"As of:\s*(\d{1,2}/\d{1,2}/\d{4})")


def _fuel(codes: str | None) -> str | None:
    if not codes:
        return None
    parts = codes.split()
    return "; ".join(FUEL.get(p.upper(), p) for p in parts)


def _study(cells: list[Any]) -> str | None:
    stage = None
    for label, i in STUDY_COLS:
        titles = [t for t in cells[i].xpath(".//img/@title") if t.strip()]
        if titles:
            stage = f"{label}: {titles[0].strip()}"
    return stage


def parse_page(data: bytes) -> tuple[date | None, list[QueueEntry]]:
    """(the page's as-of date, entries)."""
    try:
        doc = lhtml.fromstring(data)
    except (etree.ParserError, ValueError) as exc:
        raise ParseError(f"IRTT page is not HTML: {exc}") from None
    tables = doc.xpath('//table[@id="publicqueue"]')
    if not tables:
        raise ParseError("IRTT page has no #publicqueue table (login page or layout change)")
    table = tables[0]
    header = tuple(th.text_content().strip() for th in table.xpath("./thead/tr/th"))
    if header != HEADER:
        raise ParseError(f"IRTT queue columns changed: {header}")
    m = _AS_OF.search(doc.text_content())
    as_of = us_date(m[1]) if m else None
    out = []
    for tr in table.xpath("./tbody/tr"):
        cells = tr.xpath("./td")
        if len(cells) != len(HEADER):
            raise ParseError(f"IRTT queue row with {len(cells)} cells, expected {len(HEADER)}")
        v = [text(c.text_content()) for c in cells]
        qid = v[COL["QP"]]
        if not qid:
            continue
        status_code = (v[COL["Status"]] or "").upper()
        status = STATUS.get(status_code, status_code or None)
        op_date = us_date(v[COL["Op Date"]])
        mw = next((x for x in (number(v[COL["Net MW"]]), number(v[COL["Summer MW"]]), number(v[COL["Winter MW"]]))
                   if x), None)
        out.append(QueueEntry(qid, {
            "name": v[COL["Alternative Name"]],
            "county": v[COL["County"]],
            "state": v[COL["ST"]],
            "poi": v[COL["POI"]],
            "fuel": _fuel(v[COL["Fuel Type"]]),
            "project_type": TYPES.get(v[COL["Type"]] or "", v[COL["Type"]]),
            "mw_requested": mw,
            "status": status,
            "queue_date": us_date(v[COL["Requested"]]),
            "proposed_cod": None if status_code == "C" else month_or_date(v[COL["Op Date"]]),
            "actual_cod": op_date if status_code == "C" else None,
            "withdrawn_date": us_date(v[COL["W/D Date"]]),
            "study_phase": _study(cells),
            "cycle": v[COL["Cluster"]],
            "zone": v[COL["Zone"]],
        }, updated=us_date(v[COL["Updated"]])))
    return as_of, unique_ids(out)


def unique_ids(entries: list[QueueEntry]) -> list[QueueEntry]:
    """Old queue positions list several units on one QP ('8': ANP Bellingham Unit 1, Unit 2 ...): those
    rows get '<QP>/<name slug>' ids so each unit is its own entry; a single-row QP keeps its number."""
    counts = Counter(e.queue_id for e in entries)
    used: set[str] = set()
    for e in entries:
        if counts[e.queue_id] > 1:
            base = f"{e.queue_id}/{slugify(e.values.get('name') or 'unnamed')[:60]}"
            qid, n = base, 2
            while qid in used:
                qid, n = f"{base}-{n}", n + 1
            e.queue_id = qid
        used.add(e.queue_id)
    return entries


MIN_ENTRIES = 500  # fewer means a truncated page or a layout change


def load(doc: DocumentRow, stats: RunStats, *, min_entries: int = MIN_ENTRIES) -> None:
    as_of, entries = parse_page(doc.read())
    if len(entries) < min_entries:
        raise ParseError(f"IRTT queue looks truncated: {len(entries)} rows")
    fetched = doc.fetched_at.astimezone(ET).date()
    day = min(as_of or fetched, fetched)
    with db.transaction() as conn:
        store = QueueStore(conn, "isone", "ISO New England", PRODUCER, day, stats)
        for e in entries:
            store.write(e, source_url=QUEUE_URL, document_id=doc.id)
        store.finish(source_url=QUEUE_URL, document_id=doc.id)


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        doc = fetch_document(SOURCE_ID, QUEUE_URL, doc_type="queue_html", title="ISO-NE IRTT public queue",
                             fetcher=fetcher)
        stats.document(doc)
        load(doc, stats)

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="weekly_thu")
@app.task(name="collect.isone_queue", queue="collect", queueing_lock="collect.isone_queue",
          lock="collect.isone_queue")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "isone_queue", help="ISO-NE interconnection request queue (IRTT public queue)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
