"""MISO generator interconnection queue -> queue_project facts (tag F, source MISO). See iso_queue.

MISO publishes its queue only through the GI Interactive Queue on www.misoenergy.org, whose data is
the JSON at https://www.misoenergy.org/api/giqueue/getprojects. On 2026-10-21 every www.misoenergy.org
URL, robots.txt and the legal pages included, answered Marden's declared crawler with HTTP 403 and a
Cloudflare managed challenge ('cf-mitigated: challenge', "Just a moment..."), which only a scripted
browser can pass. Marden does not get around bot challenges, so a run records the failure on the
source and writes nothing. MISO's free-key API portal (data-exchange.misoenergy.org, public-api.misoenergy.org)
offers load, generation, interchange and pricing data but no interconnection queue, and MISO's
large-load information is posted on the same challenged site.

When the endpoint answers JSON, `parse_projects` reads it: a list of project objects keyed
projectNumber, applicationStatus, county, state, transmissionOwner, poiName, fuelType, summerNetMW,
winterNetMW, queueDate, inService, withdrawnDate, studyPhase, svcType (the field names of the
interactive queue's JSON as used by public tools; not seen by Marden yet, so a payload without
projectNumber and applicationStatus is refused rather than guessed at).

Cadence: MISO updates the queue continuously; tried weekly (Monday). `python -m marden collect miso_queue`.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from marden import cli
from marden.collectors.iso_queue import QueueEntry, QueueStore, month_or_date, number, text, us_date
from marden.collectors.rates_common import ET, ParseError, RunStats, collect, print_stats
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "miso_queue"
PRODUCER = "collector:miso_queue"
HOST = "www.misoenergy.org"
QUEUE_URL = f"https://{HOST}/api/giqueue/getprojects"
PAGE_URL = f"https://{HOST}/planning/resource-utilization/GI_Queue/gi-interactive-queue/"
CRON_UTC = "13 11 * * 1"  # Mondays 11:13 UTC
TERMS_NOTE = (
    "Checked 2026-10-21. Terms could not be read: www.misoenergy.org (including /robots.txt and the legal pages) "
    "answers automated clients with HTTP 403 'cf-mitigated: challenge' (Cloudflare managed challenge, 'Just a "
    "moment... Enable JavaScript and cookies to continue'). A bot challenge on every page, robots.txt included, "
    "is treated as no permission to collect automatically; Marden does not solve or route around it, so each "
    "run records the 403 and writes nothing. MISO's API portals (data-exchange.misoenergy.org: Load, Generation "
    "and Interchange API and Pricing API, free subscription key; public-api.misoenergy.org: real-time displays) "
    "have no interconnection-queue or large-load data. Unblock: MISO allow-lists the crawler or publishes the "
    "queue on an API."
)
SOURCE = declare_source(SOURCE_ID, "MISO", "portal", f"https://{HOST}", 7 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 0.2)

REQUIRED = ("projectNumber", "applicationStatus")


class BotChallenge(FetchError):
    """The site answered with a bot challenge instead of data."""


def is_challenge(exc: FetchError) -> bool:
    return exc.status == 403 and ("Just a moment" in str(exc) or "challenge" in str(exc).lower())


def parse_projects(data: bytes) -> list[QueueEntry]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ParseError(f"MISO queue response is not JSON: {exc}") from None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ParseError("MISO queue response is not a non-empty list of projects")
    missing = [k for k in REQUIRED if k not in payload[0]]
    if missing:
        raise ParseError(f"MISO queue projects lack {missing}; keys: {sorted(payload[0])[:30]}")
    out = []
    for p in payload:
        qid = text(p.get("projectNumber"))
        if not qid:
            continue
        mw = number(p.get("summerNetMW"))
        if mw is None:
            mw = number(p.get("winterNetMW"))
        out.append(QueueEntry(qid, {
            "transmission_owner": text(p.get("transmissionOwner")),
            "county": text(p.get("county")),
            "state": text(p.get("state")),
            "poi": text(p.get("poiName")),
            "fuel": text(p.get("fuelType")),
            "project_type": text(p.get("svcType")),
            "mw_requested": mw,
            "status": text(p.get("applicationStatus")),
            "queue_date": us_date(p.get("queueDate")),
            "proposed_cod": month_or_date(p.get("inService")),
            "withdrawn_date": us_date(p.get("withdrawnDate")),
            "study_phase": text(p.get("studyPhase")),
        }))
    return out


def load(doc: DocumentRow, stats: RunStats) -> None:
    entries = parse_projects(doc.read())
    with db.transaction() as conn:
        store = QueueStore(conn, "miso", "MISO", PRODUCER, doc.fetched_at.astimezone(ET).date(), stats)
        for e in entries:
            store.write(e, source_url=PAGE_URL, document_id=doc.id)
        store.finish(source_url=PAGE_URL, document_id=doc.id)


def fetch(fetcher: Fetcher | None = None) -> DocumentRow:
    try:
        return fetch_document(SOURCE_ID, QUEUE_URL, doc_type="queue_json", title="MISO GI queue projects",
                              fetcher=fetcher)
    except FetchError as exc:
        if is_challenge(exc):
            raise BotChallenge(QUEUE_URL, exc.status, "Cloudflare bot challenge (HTTP 403, cf-mitigated: "
                               "challenge); not bypassed, nothing written") from None
        raise


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        doc = fetch(fetcher)
        stats.document(doc)
        load(doc, stats)

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="weekly_mon")
@app.task(name="collect.miso_queue", queue="collect", queueing_lock="collect.miso_queue", lock="collect.miso_queue")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "miso_queue", help="MISO GI queue (currently refused by a Cloudflare bot challenge)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
