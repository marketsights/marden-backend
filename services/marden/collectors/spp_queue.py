"""SPP generator interconnection queue -> queue_project facts (tag F, source SPP). See iso_queue for the model.

SPP's OpsPortal lists every generation interconnection request on "Generator Interconnection Request
Summary" (https://opsportal.spp.org/Studies/GISummary: active, in service, withdrawn and terminated;
the GIActive page is the active subset). Its "Download data as CSV" button,
https://opsportal.spp.org/Studies/GenerateSummaryCSV, is stored raw and parsed in code. The first line is
`"Last Updated On",MM/DD/YYYY` (the report date), the second the header. Ids are SPP's Generation
Interconnection Numbers (GEN-2024-379, ERAS-2025-036, TI-18-0827 ...), so subjects are `spp:<number>`.

What SPP publishes and what it does not:
- No project name and no interconnection customer, so queue-match@0.1 (which needs a name) cannot link
  an SPP entry to our sites, campuses or plants; every entry stays unmatched.
- " Nearest Town or County" is written as `county` in SPP's own words (a town for some rows, e.g.
  'Amarillo'); `poi` is "Substation or Line"; `transmission_owner` is "TO at POI".
- `fuel` is "Generation Type", with "Fuel Type" after a colon when SPP gives one ('Thermal: Gas').
- MW: "Capacity", else "MAX Summer MW", else "Nameplate Capacity".
- Dates: "Commercial Operation Date" (else "In-Service Date") is `actual_cod` for entries in commercial
  operation and `proposed_cod` otherwise; "Request Received" is `queue_date`; "Date Withdrawn".
- `status` is SPP's status ('IA FULLY EXECUTED/ON SCHEDULE', 'DISIS STAGE', 'WITHDRAWN', 'TERMINATED');
  `study_phase` repeats it for entries still in study and adds "Cause of Delay"; `cycle` is "Current
  Cluster", `zone` is "Cluster Group".
- No large loads: SPP's High Impact Large Load (HILL) process publishes meeting material, not requests.

Cadence: SPP regenerates the list daily ("Last Updated"); collected weekly (Friday).
`python -m marden collect spp_queue`.
"""

from __future__ import annotations

import argparse
import csv
import io
from datetime import date
from typing import Any

from marden import cli
from marden.collectors.iso_queue import QueueEntry, QueueStore, month_or_date, number, status_class, text, us_date
from marden.collectors.rates_common import ET, ParseError, RunStats, collect, print_stats
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "spp_queue"
SOURCE_NAME = "SPP"  # the name seed facts from spp.org carry (seed.common.SOURCE_NAMES)
PRODUCER = "collector:spp_queue"
HOST = "opsportal.spp.org"
PAGE_URL = f"https://{HOST}/Studies/GISummary"
CSV_URL = f"https://{HOST}/Studies/GenerateSummaryCSV"
CRON_UTC = "53 11 * * 5"  # Fridays 11:53 UTC
TERMS_URL = "https://www.spp.org/terms-conditions/"
AUP_URL = "https://www.spp.org/Documents/22819/SPP_External_Systems_Acceptable_Use_Policy%20Final%20Approved.pdf"
TERMS_NOTE = (
    f"Checked 2026-10-21. SPP Terms & Conditions ({TERMS_URL}), Copyright: \"Permission is implicitly granted "
    "to copy and distribute (via computer network or printed form) in whole or in part (with appropriate "
    "citation) EXCEPT when such materials will be used, in whole or in part, within a commercial publication "
    "(printed or otherwise) ... Any commercial use of these materials requires prior, express written "
    "authorization from the author(s) or a duly authorized officer of SPP.\" Marden has no such authorization, "
    "so SPP rows are flagged 'unlicensed-commercial' (marden.core.licensing) until SPP grants it. Automated "
    f"access is not forbidden: SPP External Systems Acceptable Use Policy ({AUP_URL}) covers \"programmatic "
    "interfaces\" and \"public data\" and asks for approval only for use \"inconsistent with this Policy (e.g., "
    "polling of systems at a frequency higher than necessary for basic reconciliation of data)\". "
    "opsportal.spp.org and www.spp.org serve no robots.txt (404). The CSV is the public 'Download data as CSV' "
    "button on GISummary (no login). One request a week."
)
SOURCE = declare_source(SOURCE_ID, "SPP", "portal", f"https://{HOST}", 7 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 0.5)

HEADER = (
    "Generation Interconnection Number", "IFS Queue Number", "Current Cluster", "Cluster Group",
    "Nearest Town or County", "State", "TO at POI", "In-Service Date", "Commercial Operation Date",
    "Cessation Date", "Original Generator Commercial Op Date", "Capacity", "MAX Summer MW", "MAX Winter MW",
    "Service Type", "Requested Maximum Injection Capability (MW)", "Requested Network Resource Deliverability (MW)",
    "Nameplate Capacity", "Generation Type", "Fuel Type", "Substation or Line", "Request Received",
    "Date Withdrawn", "Status", "JTIQ Participant", "JTIQ Commitment", "Cause of Delay",
)
COL = {name: i for i, name in enumerate(HEADER)}
MIN_ENTRIES = 1000  # SPP lists ~3,000 requests; fewer means a truncated or changed file


def _fuel(gen_type: str | None, fuel_type: str | None) -> str | None:
    if gen_type and fuel_type and fuel_type.lower() != gen_type.lower():
        return f"{gen_type}: {fuel_type}"
    return gen_type or fuel_type


def _mw(v: list[str | None]) -> int | float | None:
    for name in ("Capacity", "MAX Summer MW", "Nameplate Capacity"):
        mw = number(v[COL[name]])
        if mw:
            return mw
    return None


def parse_csv(data: bytes) -> tuple[date, list[QueueEntry]]:
    """(the 'Last Updated On' date, entries)."""
    try:
        body = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ParseError("SPP queue file is not UTF-8 text") from None
    rows = list(csv.reader(io.StringIO(body, newline="")))
    if len(rows) < 2 or not rows[0] or rows[0][0].strip() != "Last Updated On":
        raise ParseError("SPP queue CSV does not start with 'Last Updated On' (error page or layout change)")
    as_of = us_date(rows[0][1] if len(rows[0]) > 1 else None)
    if as_of is None:
        raise ParseError(f"SPP queue CSV has no report date: {rows[0]!r}")
    header = tuple(" ".join(h.split()) for h in rows[1])
    if header != HEADER:
        raise ParseError(f"SPP queue columns changed: {header}")
    out: list[QueueEntry] = []
    seen: set[str] = set()
    for row in rows[2:]:
        if not any(c.strip() for c in row):
            continue
        if len(row) != len(HEADER):
            raise ParseError(f"SPP queue row with {len(row)} cells, expected {len(HEADER)}: {row[:2]}")
        v = [text(c) for c in row]
        qid = v[COL["Generation Interconnection Number"]]
        if not qid:
            continue
        if qid in seen:
            raise ParseError(f"SPP queue number {qid} listed twice")
        seen.add(qid)
        status = v[COL["Status"]]
        cls = "suspended" if status and "SUSPENSION" in status.upper() else status_class(status)
        cod = month_or_date(v[COL["Commercial Operation Date"]]) or month_or_date(v[COL["In-Service Date"]])
        delay = v[COL["Cause of Delay"]]
        phase = "; ".join(p for p in (status if cls == "active" else None,
                                      f"Cause of delay: {delay}" if delay else None) if p)
        service = v[COL["Service Type"]]
        out.append(QueueEntry(qid, {
            "transmission_owner": v[COL["TO at POI"]],
            "county": (v[COL["Nearest Town or County"]] or "").rstrip(", ") or None,
            "state": (v[COL["State"]] or "").upper() or None,
            "poi": v[COL["Substation or Line"]],
            "fuel": _fuel(v[COL["Generation Type"]], v[COL["Fuel Type"]]),
            "project_type": f"Generator ({service} service)" if service else "Generator",
            "mw_requested": _mw(v),
            "status": status,
            "status_class": cls,
            "queue_date": us_date(v[COL["Request Received"]]),
            "proposed_cod": None if cls == "in_service" else cod,
            "actual_cod": cod if cls == "in_service" else None,
            "withdrawn_date": us_date(v[COL["Date Withdrawn"]]),
            "study_phase": phase or None,
            "cycle": v[COL["Current Cluster"]],
            "zone": v[COL["Cluster Group"]],
        }))
    return as_of, out


def load(doc: DocumentRow, stats: RunStats, *, min_entries: int = MIN_ENTRIES) -> None:
    as_of, entries = parse_csv(doc.read())
    if len(entries) < min_entries:
        raise ParseError(f"SPP queue CSV looks truncated: {len(entries)} rows")
    day = min(as_of, doc.fetched_at.astimezone(ET).date())
    with db.transaction() as conn:
        store = QueueStore(conn, "spp", SOURCE_NAME, PRODUCER, day, stats)
        for e in entries:
            store.write(e, source_url=CSV_URL, document_id=doc.id)
        store.finish(source_url=CSV_URL, document_id=doc.id)
    stats.extra["spp_csv"] = {"url": CSV_URL, "page": PAGE_URL, "last_updated": as_of.isoformat()}


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        doc = fetch_document(SOURCE_ID, CSV_URL, doc_type="queue_csv", title="SPP GI request summary (CSV)",
                             fetcher=fetcher)
        stats.document(doc)
        load(doc, stats)

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="weekly_fri")
@app.task(name="collect.spp_queue", queue="collect", queueing_lock="collect.spp_queue", lock="collect.spp_queue")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "spp_queue", help="SPP generator interconnection requests (OpsPortal GI summary CSV)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
