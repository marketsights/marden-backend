"""PJM interconnection queues -> queue_project facts (tag F, source PJM). See iso_queue for the model.

Two public exports, both stored raw before parsing:

- Serial queues (A through AJ, plus long-term firm transmission and upgrade requests): the XML
  linked as "Export: XML" on https://www.pjm.com/planning/service-requests/serial-service-request-status
  (/pjmfiles/media/planning/queues-data/PlanningQueues.xml, regenerated daily, ~23 MB).
- Transition Cycles 1-2 and Cycle 1 (the reformed process): the "XML / All" export of
  https://www.pjm.com/planning/m/cycle-service-request-status, a form POST to
  /m/ProjectTransition/GenerateXMLTransitionProjectsAll (no account or key), ~4 MB.

PJM marks serial projects that moved into the transition cycles as "AE1-070 - moved to TC1" and says
their serial data is "no longer current"; such rows are stored under their base id from the cycle
export, and the serial row is used only when the cycle export does not list the project.

PJM Data Miner 2 (api.pjm.com) needs a free subscription key (apiportal.pjm.com) and has no
interconnection-queue feed; nothing here uses it or the key embedded in PJM's web pages.
Cadence: PJM regenerates the exports daily; collected weekly (Tuesday). `python -m marden collect pjm_queue`.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import time
from datetime import UTC, date
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from lxml import etree

from marden import cli
from marden.collectors.iso_queue import QueueEntry, QueueStore, month_or_date, number, text, us_date
from marden.collectors.rates_common import ET, ParseError, RunStats, collect, print_stats
from marden.core import db, documents, http, sources
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "pjm_queue"
PRODUCER = "collector:pjm_queue"
HOST = "www.pjm.com"
SERIAL_PAGE = f"https://{HOST}/planning/service-requests/serial-service-request-status"
CYCLE_PAGE = f"https://{HOST}/planning/m/cycle-service-request-status"
SERIAL_URL = f"https://{HOST}/pjmfiles/media/planning/queues-data/PlanningQueues.xml"
CYCLE_URL = f"https://{HOST}/m/ProjectTransition/GenerateXMLTransitionProjectsAll"
# The request model the page's "XML / All" button sends (PJM.Website.Feature.Planning.ProjectTransition.js).
CYCLE_MODEL = {
    "GridName": "ProjectTransition", "ItemType": 0, "Items": [],
    "Paginator": {"ItemType": 7, "CurrentItmsPerPageValue": "100", "CurrentPageIndex": "1"},
    "Sort": "QueueNumber", "SortDirection": "asc", "RelatedGridsFilters": "", "PrimaryTab": "SISReport",
}
CRON_UTC = "23 11 * * 2"  # Tuesdays 11:23 UTC (06:23/07:23 ET)
TERMS_URL = "https://www.pjm.com/about-pjm/legal.aspx"
TERMS_NOTE = (
    f"Checked 2026-10-21. PJM Legal & Privacy ({TERMS_URL}): \"Transmitting information to PJM member companies, "
    "partners, the electric industry, and the public is an important function of PJM.\" and \"Access to this "
    "website does not confer any license or ownership interest in either the form or content of the website\"; "
    "no clause on automated access. robots.txt (https://www.pjm.com/robots.txt) disallows only /xsl/, /temp/, "
    "/sitecore and admin paths; /pjmfiles/ and /m/ are allowed. Both exports are the public download buttons on "
    "the queue pages (no login). Data Miner 2 needs a free key (apiportal.pjm.com) and is not used. "
    "One weekly run, 2 requests, PJM's own XML."
)
SOURCE = declare_source(SOURCE_ID, "PJM", "portal", f"https://{HOST}", 7 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 0.5)

_MOVED = re.compile(r"\s+-\s+moved to .*$", re.I)
_SERIAL_STAGES = (
    ("Feasibility Study", "FeasibilityStudyStatus"),
    ("System Impact Study", "SystemImpactStudyStatus"),
    ("Facilities Study", "FacilitiesStudyStatus"),
    ("Interconnection Service Agreement", "Interim-InterconnectionService-GenerationInterconnectionAgreement-Status"),
    ("Construction Service Agreement", "ConstructionServiceAgreementStatus"),
)
_NOT_STARTED = frozenset({"not started", "not required", "n/a"})


# ---------------------------------------------------------------- fetch
def fetch_cycle(fetcher: Fetcher | None = None, *, attempts: int = 3) -> DocumentRow:
    """POST the cycle page's "XML / All" export and store the response raw (fetch_document is GET-only)."""
    sources.require_collectable(SOURCE_ID)
    fetcher = fetcher or http.default_fetcher()
    _, bucket = http.bucket_for(HOST)
    headers = {"User-Agent": http.user_agent_for(HOST, fetcher.settings), "Referer": CYCLE_PAGE}
    form = {"jsonModel": json.dumps(CYCLE_MODEL, separators=(",", ":"))}
    for attempt in range(attempts):
        bucket.acquire()
        try:
            resp = fetcher.client.post(CYCLE_URL, data=form, headers=headers)
        except httpx.TransportError as exc:
            if attempt == attempts - 1:
                raise FetchError(CYCLE_URL, None, f"{type(exc).__name__}: {exc}") from exc
            time.sleep(2.0 * 2**attempt)
            continue
        if resp.status_code in http.RETRY_STATUSES and attempt < attempts - 1:
            time.sleep(2.0 * 2**attempt)
            continue
        break
    if not 200 <= resp.status_code < 300:
        raise FetchError(CYCLE_URL, resp.status_code, f"HTTP {resp.status_code}: {resp.content[:200]!r}")
    return documents.record_document(
        SOURCE_ID, CYCLE_URL, resp.content, content_type=resp.headers.get("content-type"), doc_type="queue_xml",
        title="PJM cycle projects (all)", external_id="cycle",
        meta={"http": {"status": resp.status_code, "method": "POST", "form": form}, "page": CYCLE_PAGE},
    )


def fetch_serial(fetcher: Fetcher | None = None) -> DocumentRow:
    return fetch_document(SOURCE_ID, SERIAL_URL, doc_type="queue_xml", title="PJM serial queues (PlanningQueues.xml)",
                          external_id="serial", fetcher=fetcher, meta={"page": SERIAL_PAGE})


def report_date(doc: DocumentRow) -> date:
    """The file's Last-Modified day when PJM sends one, else the day it was fetched (Eastern)."""
    modified = (doc.meta.get("http") or {}).get("last_modified")
    if modified:
        try:
            return parsedate_to_datetime(modified).astimezone(UTC).date()
        except (TypeError, ValueError):
            pass
    return doc.fetched_at.astimezone(ET).date()


# ---------------------------------------------------------------- parse
def _projects(data: bytes) -> list[dict[str, str]]:
    try:
        root = etree.parse(io.BytesIO(data), etree.XMLParser(huge_tree=True, resolve_entities=False)).getroot()
    except etree.XMLSyntaxError as exc:
        raise ParseError(f"PJM queue export is not XML: {exc}") from None
    if root.tag != "Projects":
        raise ParseError(f"PJM queue export: root <{root.tag}>, expected <Projects>")
    rows = [{c.tag: (c.text or "").strip() for c in p} for p in root if p.tag == "Project"]
    if rows and "ProjectNumber" not in rows[0]:
        raise ParseError("PJM queue export: <Project> without <ProjectNumber>")
    return rows


def _base(fields: dict[str, Any], p: dict[str, str]) -> dict[str, Any]:
    status = text(p.get("Status"))
    return {
        "name": text(p.get("CommercialName")) or text(p.get("Name")),
        "poi": text(p.get("Name")),
        "state": text(p.get("State")),
        "county": text(p.get("County")),
        "transmission_owner": text(p.get("TransmissionOwner")),
        "project_type": text(p.get("ProjectType")),
        "fuel": text(p.get("Fuel")),
        "status": status,
        "queue_date": us_date(p.get("SubmittedDate")),
        "actual_cod": us_date(p.get("ActualInServiceDate")),
        **fields,
    }


def parse_serial(data: bytes) -> list[QueueEntry]:
    out = []
    for p in _projects(data):
        qid = _MOVED.sub("", " ".join(p["ProjectNumber"].split()))
        if not qid:
            continue
        stage = next(
            (f"{label}: {p[key]}" for label, key in reversed(_SERIAL_STAGES)
             if text(p.get(key)) and p[key].lower() not in _NOT_STARTED),
            None,
        )
        out.append(QueueEntry(qid, _base({
            "mw_requested": number(p.get("MaximumFacilityOutput")),
            "proposed_cod": month_or_date(p.get("ProjectedInServiceDate")),
            "withdrawn_date": us_date(p.get("WithdrawalDate")),
            "study_phase": stage,
        }, p)))
    return out


def parse_cycle(data: bytes) -> list[QueueEntry]:
    out = []
    for p in _projects(data):
        qid = " ".join(p["ProjectNumber"].split())
        if not qid:
            continue
        out.append(QueueEntry(qid, _base({
            "developer": text(p.get("Developer")),
            "mw_requested": number(p.get("MWO")),
            "proposed_cod": month_or_date(p.get("RequestedInServiceDate")),
            "withdrawn_date": us_date(p.get("WithdrawnDate")),
            "study_phase": text(p.get("Stage")),
            "cycle": text(p.get("Cycle")),
        }, p), updated=us_date(p.get("LastUpdated"))))
    return out


# ---------------------------------------------------------------- run
def load(serial: DocumentRow, cycle: DocumentRow, stats: RunStats) -> None:
    """Parse both stored exports and write them as one report (cycle rows win over serial rows)."""
    cycle_entries = parse_cycle(cycle.read())
    serial_entries = parse_serial(serial.read())
    if len(cycle_entries) < 100 or len(serial_entries) < 1000:
        raise ParseError(f"PJM exports look truncated: {len(cycle_entries)} cycle, {len(serial_entries)} serial rows")
    in_cycle = {e.queue_id for e in cycle_entries}
    day = max(report_date(serial), report_date(cycle))
    with db.transaction() as conn:
        store = QueueStore(conn, "pjm", "PJM", PRODUCER, day, stats)
        for e in cycle_entries:
            store.write(e, source_url=CYCLE_PAGE, document_id=cycle.id)
        for e in serial_entries:
            if e.queue_id not in in_cycle:
                store.write(e, source_url=SERIAL_URL, document_id=serial.id)
        store.finish(source_url=CYCLE_PAGE, document_id=cycle.id)
    stats.extra["pjm_rows"] = {"cycle": len(cycle_entries), "serial": len(serial_entries),
                               "serial_superseded_by_cycle": len(in_cycle & {e.queue_id for e in serial_entries})}


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        serial = fetch_serial(fetcher)
        stats.document(serial)
        cycle = fetch_cycle(fetcher)
        stats.document(cycle)
        load(serial, cycle, stats)

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="weekly_tue")
@app.task(name="collect.pjm_queue", queue="collect", queueing_lock="collect.pjm_queue", lock="collect.pjm_queue")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "pjm_queue", help="PJM interconnection queues (serial + transition/cycle exports)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
