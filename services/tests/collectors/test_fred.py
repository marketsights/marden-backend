"""fred: parsers on real FRED responses, the ICE licence block, API-key hygiene, facts and reruns, live run.

The ICE BofA series are licence-restricted ("Copyrighted: Pre-Approval Required"), so no ICE values
are stored as fixtures or fetched by tests: the write path and the live run use the public-domain
series DGS10 (same fredgraph.csv format) under a test source.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import fred
from marden.collectors.rates_common import ParseError, RunStats, due_et
from marden.core import documents, http, sources
from marden.core.http import Fetcher, FetchError, fetch_document
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "fred"
PUBLIC = fred.FredSeries("DGS10", "test_fred_dgs10", "Board of Governors of the Federal Reserve System via FRED",
                         tag="F", display_allowed=False, notice="test notice")
SECRET = "k3y-0f-32-chars-abcdefghijklmnop"


def _public_source() -> sources.SourceSpec:
    spec = sources.declare_source(
        "test_fred_public", "FRED public-domain series (test)", "api", "https://fred.stlouisfed.org", 1440,
        "test: DGS10 is Public Domain: Citation Requested",
    )
    sources.upsert_spec(spec)
    return spec


def test_parses_fredgraph_csv():
    rows, blanks = fred.parse_csv((DATA / "fredgraph_DGS10.csv").read_bytes(), "DGS10")
    assert rows[0] == (date(2026, 8, 3), Decimal("4.70")) and rows[-1] == (date(2026, 10, 19), Decimal("5.15"))
    assert blanks == 2  # Labor Day and Columbus Day: no observation, nothing written
    assert date(2026, 9, 7) not in dict(rows)
    with pytest.raises(ParseError, match="header"):
        fred.parse_csv((DATA / "fredgraph_DGS10.csv").read_bytes(), "BAMLH0A0HYM2")


def test_parses_api_observations_and_errors():
    rows, blanks = fred.parse_api((DATA / "api_observations_docs_example.json").read_bytes(), "GNPCA")
    assert rows[0] == (date(1929, 1, 1), Decimal("1065.9")) and len(rows) == 4 and blanks == 0
    with pytest.raises(ParseError, match="api_key is not set"):
        fred.parse_api((DATA / "api_error_no_key.json").read_bytes(), "DGS10")
    rows, blanks = fred.parse_api(b'{"observations": [{"date": "2026-10-12", "value": "."}]}', "DGS10")
    assert rows == [] and blanks == 1


def test_ice_terms_are_quoted_from_the_series_page_and_fred_legal():
    notes = (DATA / "series_notes_BAMLH0A0HYM2_excerpt.txt").read_text()
    legal = (DATA / "legal_excerpt.txt").read_text()
    assert "Tag: Copyrighted: Pre-Approval Required" in notes
    assert fred.ICE_NOTICE in notes
    assert "these series may only be used for non-commercial educational or personal use" in legal
    assert "Public Domain: Citation requested and Copyrighted: Citation required—may be used for internal" in legal
    assert {s.fred_id for s in fred.ICE_SERIES} == {
        "BAMLH0A0HYM2", "BAMLH0A1HYBB", "BAMLH0A2HYB", "BAMLH0A3HYC", "BAMLC0A4CBBB", "BAMLC0A0CM"}
    assert all(s.display_allowed is False and s.notice == fred.ICE_NOTICE for s in fred.ICE_SERIES)


def test_blocked_source_makes_no_request_and_writes_nothing(committed):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    out = fred.run(fetcher=Fetcher(client=httpx.Client(transport=httpx.MockTransport(handler))))
    assert out["ok"] is False and out["error"].startswith("SourceBlocked")
    assert calls == []
    src = committed.execute(
        "SELECT blocked, blocked_reason, last_error FROM source WHERE id = 'fred_ice_bofa'").fetchone()
    assert src["blocked"] is True and "Pre-Approval Required" in src["blocked_reason"]
    assert "SourceBlocked" in src["last_error"]
    assert committed.execute("SELECT ok FROM source_run WHERE source_id = 'fred_ice_bofa'").fetchone()["ok"] is False
    assert committed.execute("SELECT count(*) AS n FROM document").fetchone()["n"] == 0
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


def test_load_writes_values_and_licence_facts_once(committed):
    spec = _public_source()
    doc = documents.record_document(spec.id, fred.csv_url("DGS10"), (DATA / "fredgraph_DGS10.csv").read_bytes(),
                                    content_type="application/csv", doc_type="fredgraph_csv", external_id="DGS10")
    first = RunStats()
    assert fred.load_document(doc, PUBLIC, first) == 54
    assert first.facts_new == 54 + 2 and first.blank_cells == 2
    again = RunStats()
    fred.load_document(doc, PUBLIC, again)
    assert again.facts_new == 0
    cur = {r["field"]: r for r in committed.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'series' AND subject_id = 'test_fred_dgs10'").fetchall()}
    assert cur["value"]["value"] == 5.15 and cur["value"]["valid_from"] == date(2026, 10, 19)
    assert cur["display_allowed"]["value"] is False and cur["copyright_notice"]["value"] == "test notice"
    assert cur["display_allowed"]["source_url"] == "https://fred.stlouisfed.org/series/DGS10"


class _ApiHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        ok = self.path.startswith("/ok")
        body = (DATA / ("api_observations_docs_example.json" if ok else "api_error_no_key.json")).read_bytes()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def api_server() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    http.set_rate_limit("127.0.0.1", 200)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_api_key_is_sent_but_never_stored_or_shown(committed, api_server):
    spec = _public_source()
    fetcher = Fetcher(max_retries=0)
    doc = fetch_document(spec.id, f"{api_server}/ok?series_id=GNPCA&file_type=json", fetcher=fetcher,
                         secret_params={"api_key": SECRET})
    assert doc.was_new and SECRET not in doc.url and SECRET not in str(doc.meta)
    stored = committed.execute("SELECT url, meta::text AS meta FROM document WHERE id = %s", (doc.id,)).fetchone()
    assert SECRET not in stored["url"] and SECRET not in stored["meta"]
    with pytest.raises(FetchError) as err:
        fetch_document(spec.id, f"{api_server}/err?series_id=DGS10", fetcher=fetcher, secret_params={"api_key": SECRET})
    assert err.value.status == 400 and SECRET not in str(err.value) and err.value.url.endswith("/err?series_id=DGS10")


def test_scheduled_daily_at_1800_eastern():
    assert app.tasks["collect.fred"].queue == "collect"
    crons = {pid: pt.cron for (name, pid), pt in app.periodic_registry.periodic_tasks.items() if name == "collect.fred"}
    assert crons == {"daily_1800_et": "0 22,23 * * *"}
    for day in (date(2026, 7, 1), date(2026, 12, 1)):
        slots = [int(datetime(day.year, day.month, day.day, h, 0, tzinfo=UTC).timestamp()) for h in (22, 23)]
        assert [due_et(ts, 18, 0) for ts in slots].count(True) == 1
    assert fred.collect_task(timestamp=int(datetime(2026, 7, 1, 23, 0, tzinfo=UTC).timestamp())) == {
        "skipped": "not 18:00 ET"}
    assert "fred" in cli._groups["collect"]


@pytest.mark.network
def test_live_public_series_run_twice_writes_nothing_new(committed):
    spec = _public_source()
    first = fred.run(series=(PUBLIC,), source=spec)
    assert first["ok"], first
    assert first["access"] == "fredgraph.csv" and first["documents_new"] == 1 and first["facts_new"] > 100
    second = fred.run(series=(PUBLIC,), source=spec)
    assert second["ok"], second
    assert second["documents_new"] == 0 and second["facts_new"] == 0
