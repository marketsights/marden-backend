"""El Paso Electric generator interconnection queue -> queue_project facts (tag F, source El Paso Electric).
Blocked by EPE's site terms until EPE gives written consent; see TERMS_NOTE. See iso_queue for the model.

EPE (EIA-930 BA EPE; west Texas and southern New Mexico, outside ERCOT and any RTO, the utility territory of
Project Jupiter in Doña Ana County and Meta El Paso) posts its active queue as a PDF printed from Excel,
"EPE GENERATOR INTERCONNECTION QUEUE - ACTIVE" (epe-interconnection-queue_active.pdf, linked from
https://www.epelectric.com/company/interconnection/generator-interconnection). Its OASIS (OATI, code EPE)
holds the same postings behind webSmartOASIS's signed folder calls (see oati_oasis). No large-load queue is
posted.

The page is stored raw, the PDF link is read from it, and the PDF is stored raw; `parse_pdf` reads its one
table with PyMuPDF: SGIP/LGIP, QUEUE #, APPLICATION DATE, PROJECT NAME, AFFILIATE, PROJECTED IN-SERVICE
DATE, MAX MW, LOCATION (County, State), LOCATION (Interconnection Point), TYPE OF SERVICE (NR/ER), TYPE OF
FACILITY, AVAILABLE STUDIES, STATUS, COMMENTS (comments are rotated text and not read). The as-of is the
PDF's modification date. Queue numbers are normalized to hyphens ('LG 22-3-6' -> 'LG-22-3-6').

Cadence: EPE re-posts the PDF irregularly (last 2026-07-07); checked monthly (6th) once unblocked.
`python -m marden collect epe_queue`.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

import pymupdf

from marden import cli
from marden.collectors.iso_queue import QueueEntry, QueueStore, month_or_date, text, us_date
from marden.collectors.rates_common import ET, ParseError, RunStats, collect, print_stats
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "epe_queue"
SOURCE_NAME = "El Paso Electric"
PRODUCER = f"collector:{SOURCE_ID}"
HOST = "www.epelectric.com"
INDEX_URL = f"https://{HOST}/company/interconnection/generator-interconnection"
CRON_UTC = "29 12 6 * *"  # the 6th of each month, 12:29 UTC
TERMS_URL = f"https://{HOST}/company/terms-of-use"
TERMS_NOTE = (
    f"Checked 2026-10-21. EPE Terms of Use ({TERMS_URL}): \"Personal Use of Contents The content of the Site is "
    "for your personal, noncommercial use. Content copied, downloaded or printed must retain all copyright, "
    "trademark and other proprietary notices. For commercial use of any portion of the contents you must secure "
    "the written consent from the appropriate content provider.\" and \"Your use of the Site is at the sole "
    "discretion of EPE, which may deny you further use of the Site at any time\". No clause on automated access; "
    "www.epelectric.com/robots.txt answers 404. Marden's use is commercial, so the source is blocked until EPE "
    "consents in writing (interconnections@epelectric.com is the queue contact on the page). The same queue on "
    "EPE's OATI OASIS cannot be listed without the browser client's signed calls (source oati_oasis); LBNL "
    "Queued Up carries EPE's queue as of each year-end (tag R)."
)
BLOCKED_REASON = (
    "EPE's Terms of Use limit site content to \"personal, noncommercial use\" and require \"written consent\" "
    "for \"commercial use of any portion of the contents\". Unblock once EPE consents in writing (or the business "
    "owner records a decision to collect, as for FINRA TRACE); the parser is tested and ready."
)
SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "portal", f"https://{HOST}", 31 * 24 * 60, TERMS_NOTE,
                        blocked=True, blocked_reason=BLOCKED_REASON)
http.set_rate_limit(HOST, 0.5)

_LINK = re.compile(r'href="([^"]*epe-interconnection-queue_active\.pdf[^"]*)"', re.I)
TITLE = "EPE GENERATOR INTERCONNECTION QUEUE - ACTIVE"
HEADER = {0: "SGIP OR LGIP", 1: "QUEUE #", 2: "APPLICATION DATE", 3: "PROJECT NAME", 4: "AFFILIATE",
          5: "PROJECTED IN", 6: "MAX MW", 7: "LOCATION (COUNTY", 8: "LOCATION (INTERCONNECTION",
          9: "TYPE OF SERVICE", 10: "TYPE OF FACILITY", 11: "AVAILABLE STUDIES", 12: "STATUS", 13: "COMMENTS"}
COLUMNS = 14
SERVICE = {"NR": "NRIS", "ER": "ERIS", "NR/ER": "NRIS/ERIS", "ER/NR": "NRIS/ERIS"}
_MW = re.compile(r"\d+(?:\.\d+)?")
_PDF_DATE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")


def find_pdf(page: bytes) -> str:
    m = _LINK.search(page.decode("utf-8", errors="replace"))
    if not m:
        raise ParseError(f"no epe-interconnection-queue_active.pdf link on {INDEX_URL}")
    return urljoin(INDEX_URL, m[1].replace("&amp;", "&"))


def _qid(value: str | None) -> str | None:
    s = text(value)
    if s is None:
        return None
    return re.sub(r"\s*-\s*|\s+", "-", s.upper())


def _location(value: str | None) -> tuple[str | None, str | None]:
    s = text(value)
    if not s or "," not in s:
        return s, None
    county, state = (p.strip() for p in s.rsplit(",", 1))
    return county or None, state.upper() if re.fullmatch(r"[A-Za-z]{2}", state) else None


def _entry(cells: list[str | None]) -> QueueEntry | None:
    kind = (text(cells[0]) or "").upper()
    qid = _qid(cells[1])
    if kind not in {"LGIP", "SGIP"} or qid is None:
        return None
    mw = _MW.search((cells[6] or "").replace(",", ""))
    county, state = _location(cells[7])
    affiliate = (text(cells[4]) or "").lower() == "yes"
    service = (text(cells[9]) or "").upper().replace(" ", "")
    return QueueEntry(qid, {
        "project_type": f"{kind}, EPE affiliate" if affiliate else kind,
        "queue_date": us_date(text(cells[2])),
        "name": text(cells[3]),
        "proposed_cod": month_or_date(text(cells[5])),
        "mw_requested": float(mw[0]) if mw and "." in mw[0] else (int(mw[0]) if mw else None),
        "county": county,
        "state": state,
        "poi": text(cells[8]),
        "service": SERVICE.get(service, service or None),
        "fuel": text(cells[10]),
        "study_phase": text(cells[11]),
        "status": text(cells[12]),
        "transmission_owner": "El Paso Electric Company",
        "balancing_authority": "EPE",
    })


def parse_pdf(data: bytes) -> tuple[date | None, list[QueueEntry]]:
    """(the PDF's modification date, entries)."""
    if not data.startswith(b"%PDF-"):
        raise ParseError(f"EPE queue file is not a PDF: starts {data[:20]!r}")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises its own FileDataError / RuntimeError for non-PDFs
        raise ParseError(f"EPE queue file is not a PDF: {type(exc).__name__}: {exc}") from None
    m = _PDF_DATE.match(doc.metadata.get("modDate") or doc.metadata.get("creationDate") or "")
    as_of = date(int(m[1]), int(m[2]), int(m[3])) if m else None
    out: dict[str, QueueEntry] = {}
    header_seen = False
    for page in doc:
        if TITLE not in page.get_text():
            raise ParseError(f"EPE queue PDF changed: no {TITLE!r} title")
        for table in page.find_tables().tables:
            for row in table.rows:
                # each cell's own text box (the table's extract() interleaves wrapped lines)
                cells = [" ".join(page.get_textbox(pymupdf.Rect(c)).split()) if c else None for c in row.cells]
                if len(cells) != COLUMNS:
                    raise ParseError(f"EPE queue table has {len(cells)} columns, expected {COLUMNS}")
                if (cells[1] or "").upper() == "QUEUE #":
                    found = tuple(c or "" for c in cells)
                    if any(not found[i].upper().startswith(label) for i, label in HEADER.items()):
                        raise ParseError(f"EPE queue columns changed: {found}")
                    header_seen = True
                    continue
                e = _entry(cells)
                if e is not None:
                    out.setdefault(e.queue_id, e)
    if not header_seen:
        raise ParseError("EPE queue PDF has no 'QUEUE #' header row")
    return as_of, list(out.values())


MIN_ENTRIES = 20  # the Jul 2026 PDF lists 50


def load(pdf: DocumentRow, stats: RunStats, *, min_entries: int = MIN_ENTRIES) -> date:
    as_of, entries = parse_pdf(pdf.read())
    if len(entries) < min_entries:
        raise ParseError(f"EPE queue PDF looks truncated: {len(entries)} entries")
    fetched = pdf.fetched_at.astimezone(ET).date()
    day = min(as_of or fetched, fetched)
    with db.transaction() as conn:
        store = QueueStore(conn, "epe", SOURCE_NAME, PRODUCER, day, stats, iso_label="")
        for e in entries:
            store.write(e, source_url=pdf.url, document_id=pdf.id)
        store.finish(source_url=pdf.url, document_id=pdf.id)
    return day


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        index = fetch_document(SOURCE_ID, INDEX_URL, doc_type="index_html", title="EPE Generator Interconnection",
                               fetcher=fetcher)
        stats.document(index)
        pdf = fetch_document(SOURCE_ID, find_pdf(index.read()), doc_type="queue_pdf", title="EPE GI queue (active)",
                             fetcher=fetcher)
        stats.document(pdf)
        stats.extra["epe_pdf"] = {"url": pdf.url, "as_of": load(pdf, stats).isoformat()}

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="monthly_6th")
@app.task(name="collect.epe_queue", queue="collect", queueing_lock="collect.epe_queue", lock="collect.epe_queue")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "epe_queue", help="El Paso Electric GI queue PDF (blocked: EPE terms need written consent)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
