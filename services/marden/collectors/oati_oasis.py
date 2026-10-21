"""OATI webOASIS: the shared platform of the non-RTO transmission providers' OASIS sites, and a collector
that checks them (source `oati_oasis`).

Under FERC Order 2023 (and the reciprocity tariffs of TVA, SRP and Santee Cooper) a transmission provider
outside an RTO posts its interconnection queue on its OASIS. Every provider Marden needs in the Southeast and
the West uses OATI's webOASIS (www.oasis.oati.com/<CODE>/): PROVIDERS below, with the EIA-930 balancing
authorities they run and the entity code LBNL's Queued Up uses for the same queue (lbnl_queued_up loads
those queues as of each year-end, tag R).

What this module gives other collectors:

- `oati_fetcher()`: a Fetcher that trusts OATI's own root. www.oasis.oati.com presents a chain to "OATI
  WebCARES Root CA" (OATI's NAESB-accredited CA), which Microsoft's root program includes (CCADB, Server
  Authentication) but Mozilla's, and so the system bundle, does not. The root shipped in
  certs/oati_webcares_root_ca.pem is the CCADB copy; its SHA-256 is checked against ROOT_SHA256 before use.
  Verification stays on: the system bundle plus this one root, for OATI hosts only.
- `home_url(code)`, `doc_url(code, file_name)`: the public provider page and the public document path
  (/woa/docs/<CODE>/<CODE>docs/<file>), the path webSmartOASIS itself opens a public document from.

Why no queue is parsed yet: the folder tree that lists each provider's documents (and so the queue file
names) is served only by webSmartOASIS AJAX calls (POST /webSmartOASIS/HomePage/GetData) that the browser
client signs with a per-request checksum header (rcpt/rcpi, "ChecksumInjector"); unsigned calls answer
"Session Terminated" or HTTP 403. Marden does not emulate a site's client-side request signing, so the
collector stores each provider's public OASIS page (proof the node is up), then records the gap on the
source and writes nothing. Unblock: a provider or OATI gives a stable public URL for its queue file (then
add it to QUEUE_DOCUMENTS with a parser), or OATI offers an API.

Cadence: monthly (12th). `python -m marden collect oati_oasis`.
"""

from __future__ import annotations

import argparse
import hashlib
import ssl
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from marden import cli
from marden.collectors.rates_common import RunStats, collect, print_stats
from marden.core import http
from marden.core.http import Fetcher, FetchError, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "oati_oasis"
HOST = "www.oasis.oati.com"
CRON_UTC = "19 12 12 * *"  # the 12th of each month, 12:19 UTC
ROOT_PEM = Path(__file__).parent / "certs" / "oati_webcares_root_ca.pem"
ROOT_SHA256 = "7A77C6C61EEEB9AA65C4EA410D65D895B26A81123283009DB104B48DE80B2479"  # CCADB, Microsoft-included
TREE_URL = f"https://{HOST}/webSmartOASIS/HomePage/GetData"


@dataclass(frozen=True)
class Provider:
    name: str
    balancing_authorities: tuple[str, ...]  # EIA-930 codes
    states: tuple[str, ...]
    lbnl_entity: str | None  # LBNL Queued Up 'entity'


PROVIDERS: dict[str, Provider] = {
    # Southeast
    "SOCO": Provider("Southern Company Services (Georgia Power, Alabama Power, Mississippi Power)", ("SOCO",),
                     ("GA", "AL", "MS", "FL"), "SOCO"),
    "DUK": Provider("Duke Energy Carolinas and Duke Energy Progress", ("DUK", "CPLE", "CPLW"), ("NC", "SC"),
                    "Duke"),
    "TVA": Provider("Tennessee Valley Authority", ("TVA",), ("TN", "AL", "MS", "KY", "GA", "NC", "VA"), "TVA"),
    "SC": Provider("Santee Cooper (South Carolina Public Service Authority)", ("SC",), ("SC",), "S-C"),
    "SCEG": Provider("Dominion Energy South Carolina", ("SCEG",), ("SC",), "Dominion"),
    # West
    "PPW": Provider("PacifiCorp", ("PACE", "PACW"), ("UT", "WY", "ID", "OR", "WA", "CA"), "PacifiCorp"),
    "AZPS": Provider("Arizona Public Service", ("AZPS",), ("AZ",), "APS"),
    "SRP": Provider("Salt River Project", ("SRP",), ("AZ",), "SRP"),
    "TEPC": Provider("Tucson Electric Power", ("TEPC",), ("AZ",), "TEP"),
    "NEVP": Provider("NV Energy (Nevada Power, Sierra Pacific Power)", ("NEVP",), ("NV",), "NVE"),
    "PNM": Provider("Public Service Company of New Mexico", ("PNM",), ("NM",), "PNM"),
    "EPE": Provider("El Paso Electric", ("EPE",), ("TX", "NM"), "EPE"),
    "PSCO": Provider("Public Service Company of Colorado (Xcel Energy)", ("PSCO",), ("CO",), "PSCo"),
    "IPCO": Provider("Idaho Power", ("IPCO",), ("ID", "OR"), "IP"),
    "PGE": Provider("Portland General Electric", ("PGE",), ("OR",), "PGE"),
    "PSEI": Provider("Puget Sound Energy", ("PSEI",), ("WA",), "PSE"),
}
QUEUE_DOCUMENTS: dict[str, str] = {}  # code -> public queue file name, once one is known (none yet)

TERMS_NOTE = (
    "Checked 2026-10-21. www.oasis.oati.com serves no robots.txt (HTTP 404) and the public webSmartOASIS pages "
    "link no terms of use; OATI's site terms (https://www.oati.com/terms-and-conditions/) cover OATI's own "
    "documents (\"white papers, press releases, datasheets and FAQs\"), not the transmission providers' OASIS "
    "postings, which FERC requires them to make public. TLS: the hosts chain to \"OATI WebCARES Root CA\" "
    f"(SHA-256 {ROOT_SHA256}), included in Microsoft's root program (CCADB) but not in the system bundle; Marden "
    "trusts that one root for OATI hosts, verification stays on. Access: the document tree (POST "
    "/webSmartOASIS/HomePage/GetData) is answered only to requests carrying the browser client's checksum "
    "headers (rcpt/rcpi from its ChecksumInjector script); without them it answers \"Session Terminated\" or "
    "HTTP 403. Marden does not emulate client-side request signing, so queue documents cannot be listed and "
    "nothing is collected; each run fetches the 16 public provider pages (/<CODE>/index.html, ~0.5 KB each) "
    "once a month to show the nodes are up and records the gap."
)
SOURCE = declare_source(SOURCE_ID, "OATI webOASIS (non-RTO transmission providers)", "portal", f"https://{HOST}",
                        31 * 24 * 60, TERMS_NOTE)
http.set_rate_limit("oasis.oati.com", 0.5)


class DocumentsGated(FetchError):
    """The provider's document list needs the browser client's signed AJAX calls."""


def root_pem() -> str:
    pem = ROOT_PEM.read_text()
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    if digest != ROOT_SHA256:
        raise RuntimeError(f"{ROOT_PEM} is not the CCADB OATI WebCARES Root CA (sha256 {digest})")
    return pem


def ssl_context() -> ssl.SSLContext:
    """The system / proxy CA bundle (SSL_CERT_FILE etc., as httpx would use) plus the OATI root."""
    ctx = httpx.create_ssl_context(trust_env=True)
    ctx.load_verify_locations(cadata=root_pem())
    return ctx


@lru_cache(maxsize=1)
def oati_fetcher() -> Fetcher:
    client = httpx.Client(verify=ssl_context(), timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)
    return Fetcher(client=client)


def home_url(code: str) -> str:
    return f"https://{HOST}/{code}/index.html"


def doc_url(code: str, file_name: str) -> str:
    return f"https://{HOST}/woa/docs/{code}/{code}docs/{file_name}"


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        f = fetcher or oati_fetcher()
        status: dict[str, Any] = {}
        for code, provider in PROVIDERS.items():
            try:
                doc = fetch_document(SOURCE_ID, home_url(code), doc_type="index_html", title=f"{provider.name} OASIS",
                                     fetcher=f)
            except FetchError as exc:
                status[code] = {"reachable": False, "error": str(exc)[:200]}
                continue
            stats.document(doc)
            status[code] = {"reachable": True, "document": doc.id, "queue_document": QUEUE_DOCUMENTS.get(code)}
        stats.extra["oasis"] = status
        up = sum(1 for s in status.values() if s["reachable"])
        raise DocumentsGated(TREE_URL, None, f"{up} of {len(PROVIDERS)} provider OASIS pages reachable, but their "
                             "document folders (queue files) are listed only to webSmartOASIS calls signed by the "
                             "browser client (rcpt/rcpi checksum); not emulated, nothing written")

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="monthly_12th")
@app.task(name="collect.oati_oasis", queue="collect", queueing_lock="collect.oati_oasis", lock="collect.oati_oasis")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "oati_oasis", help="OATI webOASIS queue postings of non-RTO providers (gated; records the gap)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
