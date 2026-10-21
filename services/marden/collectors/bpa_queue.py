"""BPA interconnection request queue -> queue_project facts (tag F, source BPA). See iso_queue for the model.

Bonneville Power Administration (EIA-930 BA BPAT) is a federal transmission provider outside any RTO. It
posts one workbook, the "Bonneville Power Administration Interconnection Request Queue"
(InterconnectionQueueOutput.xlsx, linked from https://www.bpa.gov/energy-and-services/transmission/interconnection),
regenerated daily (the timestamp in its title row is the as-of). The page is stored raw, the workbook link is
read from it, and the workbook is stored raw before parsing. It lists both kinds of request BPA studies:

- GI, generator interconnection requests (LGIP/SGIP; ids G...): fuel = the published fuel sources, MW = max
  summer interconnection service, hybrids also get mw_by_type from the nameplate columns;
- LL, line and load interconnection requests (ids L...): new or larger loads, mostly utilities' substations
  (PUDs and co-ops serving data centers, mines, ...), with fuel LOAD like NYISO's Load Projects, so a match to
  one of our sites or campuses also lands there as `large_load_queue_bpa`.

Status is BPA's word (WITHDRAWN, STUDY, ENERGIZED ...); status_class follows STATUS_CLASS below, not the
generic rules (ENERGIZED means in service). proposed_cod is the "Agreed To" in-service date, else the
originally requested one. There is no ISO: `iso` is not written; balancing_authority BPAT and
transmission_owner Bonneville Power Administration are.

Cadence: BPA regenerates the workbook daily; collected weekly (Friday). `python -m marden collect bpa_queue`.
"""

from __future__ import annotations

import argparse
import io
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

import openpyxl

from marden import cli
from marden.collectors.iso_queue import LOAD, QueueEntry, QueueStore, month_or_date, number, text, us_date
from marden.collectors.rates_common import ET, ParseError, RunStats, collect, print_stats
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "bpa_queue"
SOURCE_NAME = "Bonneville Power Administration"
PRODUCER = f"collector:{SOURCE_ID}"
HOST = "www.bpa.gov"
INDEX_URL = f"https://{HOST}/energy-and-services/transmission/interconnection"
CRON_UTC = "41 11 * * 5"  # Fridays 11:41 UTC
BA = "BPAT"
TERMS_NOTE = (
    "Checked 2026-10-21. BPA is a federal power marketing administration (U.S. Department of Energy); its "
    "website content is a U.S. Government work (17 U.S.C. 105, no copyright). robots.txt "
    "(https://www.bpa.gov/robots.txt): \"User-agent: * Disallow: /customerportal/ Allow: /\". Privacy policy "
    "(https://www.bpa.gov/about/who-we-are/privacy): \"When you visit our website to read pages or download "
    "information, we automatically collect and store the following information only: The name of the domain "
    "from which you entered our website ...\"; no clause limits automated or commercial use. The workbook is the "
    "public queue download on the interconnection page (no login); study reports themselves are only "
    "\"available upon request to studyrequest@bpa.gov\" and are not collected. One page and one workbook a week."
)
SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "portal", f"https://{HOST}", 7 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 0.5)

_LINK = re.compile(r'href="([^"]*/InterconnectionQueueOutput\.xlsx[^"]*)"', re.I)
TITLE = "interconnection request queue"
CONNECTION = {"GI": "Generator Interconnection", "LL": "Line and Load Interconnection"}
# BPA's status words -> status_class (Marden's reading; BPA publishes no key).
STATUS_CLASS = {
    "WITHDRAWN": "withdrawn", "ENERGIZED": "in_service", "DECOMMISSIONED": "retired", "RECEIVED": "active",
    "STUDY": "active", "STUDY COMPLETED": "active", "IA EXECUTED": "active", "E&P EXECUTED": "active",
    "CONST AGRMT EXE": "active", "BPA COMPLETED": "active",
}
STUDIES = ("feasibility study", "phase 1 cluster study", "system impact study", "phase 2 cluster study",
           "facilities study")
FLAGS = ("surplus", "repower", "replacement")
REQUIRED = ("request number", "request date", "project name", "requestor", "connection type", "state", "county",
            "status", "max summer interconnection service mw", "point of interconnection")
MIN_ENTRIES = 500  # fewer means a truncated or changed workbook


def _norm(value: Any) -> str:
    return " ".join(str(value).split()).lower() if value is not None else ""


def find_workbook(page: bytes) -> str:
    m = _LINK.search(page.decode("utf-8", errors="replace"))
    if not m:
        raise ParseError(f"no InterconnectionQueueOutput.xlsx link on {INDEX_URL}")
    return urljoin(INDEX_URL, m[1].replace("&amp;", "&"))


def _flag(value: Any) -> bool:
    return value is True or _norm(value) in {"true", "yes", "y", "x"}


def _entry(row: tuple[Any, ...], col: dict[str, int]) -> QueueEntry | None:
    def cell(name: str) -> Any:
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else None

    qid = text(cell("request number"))
    if qid is None:
        return None
    kind = (text(cell("connection type")) or "").upper()
    is_load = kind == "LL"
    status = text(cell("status"))
    parts = [(text(cell(f"fuel source {n}")), number(cell(f"nameplate mw{n}"))) for n in (1, 2, 3)]
    parts = [(f, mw) for f, mw in parts if f]
    fuels = list(dict.fromkeys(f for f, _ in parts))
    mw = number(cell("max summer interconnection service mw"))
    if mw is None:
        mw = number(cell("max winter interconnection service mw"))
    if mw is None and not is_load:
        mw = number(cell("nameplate mw total"))
    by_type = None
    if not is_load and len(fuels) > 1 and all(m is not None for _, m in parts):
        by_type = {}
        for f, m in parts:
            by_type[f] = by_type.get(f, 0) + m
    flags = [f for f in FLAGS if _flag(cell(f))]
    base = CONNECTION.get(kind, kind or None)
    studies = [s.title() for s in STUDIES if _norm(cell(s)) == "available"]
    return QueueEntry(qid, {
        "name": text(cell("project name")),
        "developer": text(cell("requestor")),
        "transmission_owner": SOURCE_NAME,
        "balancing_authority": BA,
        "county": text(cell("county")),
        "state": text(cell("state")),
        "poi": text(cell("point of interconnection")),
        "fuel": LOAD if is_load else ("; ".join(fuels) or None),
        "project_type": f"{base} ({', '.join(flags)})" if base and flags else base,
        "service": None if is_load else text(cell("interconnection type")),
        "mw_requested": mw,
        "mw_by_type": by_type,
        "status": status,
        "status_class": STATUS_CLASS.get((status or "").upper(), "active") if status else None,
        "queue_date": us_date(cell("request date")),
        "proposed_cod": month_or_date(cell("agreed to: (blank=tbd)")) or month_or_date(
            cell("original requested in-service date")),
        "study_phase": f"Studies available: {', '.join(studies)}" if studies else None,
        "cycle": text(cell("cluster")),
    })


def parse_workbook(data: bytes) -> tuple[date, list[QueueEntry]]:
    """(the as-of date in the title row, entries)."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises zipfile/KeyError/InvalidFileException for non-workbooks
        raise ParseError(f"BPA queue file is not an xlsx workbook: {type(exc).__name__}: {exc}") from None
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    as_of = None
    for row in rows[:5]:
        if any(TITLE in _norm(c) for c in row):
            as_of = next((c.date() for c in row if isinstance(c, datetime)), None)
            break
    if as_of is None:
        raise ParseError("BPA workbook has no 'Interconnection Request Queue' title row with a timestamp")
    start = next((i for i, r in enumerate(rows[:12]) if r and _norm(r[0]) == "request number"), None)
    if start is None:
        raise ParseError("BPA workbook has no 'Request Number' header row")
    col: dict[str, int] = {}
    for i, name in enumerate(rows[start]):
        key = _norm(name)
        if key and key not in col:
            col[key] = i
    missing = [c for c in REQUIRED if c not in col]
    if missing:
        raise ParseError(f"BPA queue columns changed: missing {missing}")
    entries: dict[str, QueueEntry] = {}
    for row in rows[start + 1:]:
        e = _entry(row, col)
        if e is not None:
            entries.setdefault(e.queue_id, e)
    return as_of, list(entries.values())


def load(workbook: DocumentRow, stats: RunStats, *, min_entries: int = MIN_ENTRIES) -> date:
    as_of, entries = parse_workbook(workbook.read())
    if len(entries) < min_entries:
        raise ParseError(f"BPA workbook looks truncated: {len(entries)} entries")
    day = min(as_of, workbook.fetched_at.astimezone(ET).date())
    with db.transaction() as conn:
        store = QueueStore(conn, "bpa", SOURCE_NAME, PRODUCER, day, stats, iso_label="")
        for e in entries:
            store.write(e, source_url=workbook.url, document_id=workbook.id)
        store.finish(source_url=workbook.url, document_id=workbook.id)
    return day


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        index = fetch_document(SOURCE_ID, INDEX_URL, doc_type="index_html", title="BPA Interconnection",
                               fetcher=fetcher)
        stats.document(index)
        url = find_workbook(index.read())
        workbook = fetch_document(SOURCE_ID, url, doc_type="queue_xlsx", title="BPA Interconnection Request Queue",
                                  fetcher=fetcher)
        stats.document(workbook)
        day = load(workbook, stats)
        stats.extra["bpa_workbook"] = {"url": url, "as_of": day.isoformat(), "index_document": index.id}

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="weekly_fri")
@app.task(name="collect.bpa_queue", queue="collect", queueing_lock="collect.bpa_queue", lock="collect.bpa_queue")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "bpa_queue", help="BPA interconnection request queue (generators and line/load requests)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
