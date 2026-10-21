"""oati_oasis: the pinned OATI root, the provider registry, the public paths, the evidence for the gated
document tree (real captures), the run recording the gap, the schedule and a live run (network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from marden import cli
from marden.collectors import lbnl_queued_up as lbnl
from marden.collectors import oati_oasis as oati
from marden.core.http import FetchResponse
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "oati_oasis"


def test_root_is_the_ccadb_certificate():
    assert "BEGIN CERTIFICATE" in oati.root_pem()
    ctx = oati.ssl_context()
    assert any(c.get("subject") and ("commonName", "OATI WebCARES Root CA") in [x[0] for x in c["subject"]]
               for c in ctx.get_ca_certs())


def test_registry_and_paths():
    assert set(oati.PROVIDERS) == {"SOCO", "DUK", "TVA", "SC", "SCEG", "PPW", "AZPS", "SRP", "TEPC", "NEVP", "PNM",
                                   "EPE", "PSCO", "IPCO", "PGE", "PSEI"}
    assert {p.lbnl_entity for p in oati.PROVIDERS.values()} <= set(lbnl.OPERATORS) | {
        "SOCO", "Duke", "TVA", "S-C", "Dominion", "PacifiCorp", "APS", "SRP", "TEP", "NVE", "PNM", "EPE", "PSCo",
        "IP", "PGE", "PSE"}
    assert oati.home_url("EPE") == "https://www.oasis.oati.com/EPE/index.html"
    assert oati.doc_url("EPE", "q.pdf") == "https://www.oasis.oati.com/woa/docs/EPE/EPEdocs/q.pdf"


def test_captures_show_the_public_page_and_the_signed_tree():
    assert "webSmartOASIS/HomePage?ProviderName=EPE" in (DATA / "EPE_index.html").read_text()
    assert "Session Terminated" in (DATA / "getdata_unsigned_response.html").read_text()
    script = (DATA / "checksum_injector_excerpt.js").read_text()
    assert 'setRequestHeader("rcpt"' in script and 'setRequestHeader("rcpi"' in script
    assert "rcpt/rcpi" in oati.TERMS_NOTE
    assert "white papers, press releases, datasheets and FAQs" in (DATA / "oati_terms_excerpt.txt").read_text()


class _Pages:
    """A Fetcher stand-in serving the captured provider page for every provider."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url, **kw):
        self.urls.append(url)
        body = (DATA / "EPE_index.html").read_bytes().replace(b"EPE", url.split("/")[3].encode())
        return FetchResponse(url, url, 200, body, {"content-type": "text/html"})


def test_run_stores_the_pages_and_records_the_gap(committed):
    pages = _Pages()
    out = oati.run(fetcher=pages)  # type: ignore[arg-type]
    assert not out["ok"] and "DocumentsGated" in out["error"] and "16 of 16" in out["error"]
    assert len(pages.urls) == 16 and all(s["reachable"] for s in out["oasis"].values())
    n = committed.execute("SELECT count(*) AS n FROM document WHERE source_id = 'oati_oasis'").fetchone()["n"]
    assert n == 16
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0
    src = committed.execute("SELECT last_error FROM source WHERE id = 'oati_oasis'").fetchone()
    assert "rcpt/rcpi" in src["last_error"]


def test_schedule_and_cli():
    assert app.tasks["collect.oati_oasis"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.oati_oasis"] == oati.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "oati_oasis"])._handler is oati.collect_cli


@pytest.mark.network
def test_live_run_reaches_every_provider_and_records_the_gap(committed):
    out = oati.run()
    assert not out["ok"] and "DocumentsGated" in out["error"]
    assert all(s["reachable"] for s in out["oasis"].values()), out["oasis"]
