"""ercot_gis: inert without the ERCOT API key (records 'needs key', no request), the robots and terms findings
(real captured files), the GIS workbook parser, the API flow (sign-in, archive listing, one download per
report), the schedule, and a live check that the key wall and robots rules still hold (network).

No GIS Report has been captured: robots.txt disallows the MIS download paths on www.ercot.com and the API
needs a key nobody has registered yet. The workbook and API answers below are therefore built in the test,
in the layout ERCOT publishes (sheet and column names as in the posted reports); the first keyed run is the
check against a live file, and the parser refuses (ParseError, nothing written) a layout it does not know.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import openpyxl
import pytest

from marden import cli
from marden.collectors import ercot_gis as ercot
from marden.collectors import ercot_gis_parse as gis
from marden.collectors.rates_common import ParseError
from marden.core import config, documents, licensing, sources
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "ercot_gis"
LARGE_HEADER = ["INR", "Project Name", "Interconnecting Entity", "POI Location", "County", "CDR Reporting Zone",
                "Projected COD", "Fuel", "Technology", "Capacity (MW)", "GIM Study Phase", "Screening Study Started",
                "Screening Study Complete", "FIS Requested", "FIS Approved", "Economic Study Required", "IA Signed",
                "Air Permit", "GHG Permit", "Water Availability", "Meets Planning", "Meets All Planning",
                "Approved for Energization", "Approved for Synchronization", "Comment"]


def gis_workbook() -> bytes:
    wb = openpyxl.Workbook()
    wb.active.title = "Disclaimer and References"
    large = wb.create_sheet(gis.LARGE)
    for _ in range(3):
        large.append(["Generation Interconnection Status Report"])
    large.append(LARGE_HEADER)
    large.append([None] * 11 + ["(date)"] * 6)  # sub-header row: no INR
    large.append(["24INR0459", "Test Peaker", "Example Power LLC", "Example 138kV", "Taylor", "WEST",
                  datetime(2027, 6, 30), "GAS", "GT", 360.5, "SS Completed, FIS Completed, IA",
                  datetime(2024, 9, 2), datetime(2024, 11, 1), None, None, None, datetime(2025, 3, 3)]
                 + [None] * 5 + [datetime(2026, 8, 14), None, None])
    large.append(["21INR0012", "Solar Field", "Sun Co LLC", "Sub 345kV", "Pecos", "WEST", datetime(2026, 12, 1),
                  "SOL", "PV", 250, "SS Completed, FIS Started", datetime(2021, 2, 1)] + [None] * 13)
    large.append(["Note: projects are listed until commercial operation."])
    small = wb.create_sheet(gis.SMALL)
    small.append(["INR", "Project Name", "Interconnecting Entity", "POI Location", "County", "CDR Reporting Zone",
                  "Projected COD", "Fuel", "Technology", "Capacity (MW)", "IA Signed", "Approved for Energization",
                  "Approved for Synchronization"])
    small.append(["25INR0101", "Small Battery", "Store LLC", "Feeder 12", "Bexar", "SOUTH", datetime(2026, 5, 1),
                  "OTH", "BA", 9.9, None, None, None])
    inactive = wb.create_sheet(gis.INACTIVE)
    inactive.append(["INR", "Project Name", "County", "Fuel", "MW **", "Size Category", "Inactive Date"])
    inactive.append(["22INR0300", "Paused Wind", "Nolan", "WIN", 200, "Large", datetime(2025, 7, 1)])
    cancelled = wb.create_sheet(gis.CANCELLED)
    cancelled.append(["INR", "Project Name", "County", "Fuel", "MW **", "Size Category", "Cancel Date"])
    cancelled.append(["21INR0012", "Solar Field", "Pecos", "SOL", 250, "Large", datetime(2026, 9, 15)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parses_the_gis_sheets():
    rows = {e.queue_id: e.values for e in gis.parse_workbook(gis_workbook())}
    assert set(rows) == {"24INR0459", "21INR0012", "25INR0101", "22INR0300"}
    gas = rows["24INR0459"]
    assert (gas["name"], gas["developer"], gas["county"], gas["state"], gas["poi"], gas["zone"]) == (
        "Test Peaker", "Example Power LLC", "Taylor", "TX", "Example 138kV", "WEST")
    assert (gas["fuel"], gas["mw_requested"], gas["proposed_cod"], gas["queue_date"]) == (
        "Gas", 360.5, "2027-06-30", date(2024, 9, 2))
    assert gas["project_type"] == "Large Gen (Combustion (gas) Turbine, but not part of a Combined-Cycle)"
    assert (gas["status"], gas["status_class"]) == ("Approved for Energization", "under_construction")
    assert gas["study_phase"] == ("SS Completed, FIS Completed, IA; IA signed 2025-03-03; "
                                  "approved for energization 2026-08-14")
    assert rows["21INR0012"]["status"] == "Cancelled" and rows["21INR0012"]["withdrawn_date"] == date(2026, 9, 15)
    assert rows["25INR0101"]["project_type"] == "Small Gen (Battery Energy Storage)"
    assert rows["25INR0101"]["status"] == "In study"
    paused = rows["22INR0300"]
    assert (paused["status"], paused["status_class"], paused["mw_requested"]) == ("Inactive", "suspended", 200)
    assert gis.report_month("GIS_Report_September2026") == date(2026, 9, 1)
    assert gis.report_month("Co-located_Battery_Identification_Report_September_2026") is None


def test_refuses_unknown_layouts():
    with pytest.raises(ParseError, match="not an xlsx"):
        gis.parse_workbook(b"<html>Access denied</html>")
    wb = openpyxl.Workbook()
    wb.active.title = "Summary"
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ParseError, match="lacks sheets"):
        gis.parse_workbook(buf.getvalue())


def test_robots_and_terms_findings():
    robots = (DATA / "robots.txt").read_text()
    assert "User-agent: *" in robots and "Disallow: /misapp" in robots and "Disallow: /misdownload" in robots
    product = (DATA / "data_product_PG7-200-ER_excerpt.txt").read_text()
    assert "EMIL ID pg7-200-er Report Type ID 15933" in product and "Chron - Monthly" in product
    denied = json.loads((DATA / "archive_without_key_401.json").read_text())
    assert denied["statusCode"] == 401 and "missing subscription key" in denied["message"]
    terms = " ".join((DATA / "terms_of_use_excerpt.txt").read_text().split())
    reuse = ("raw data provided in public portions of this website may be used, reproduced, and redistributed in "
             "compilations, charts, and analyses without maintaining such notices.")
    assert reuse in terms and reuse in ercot.TERMS_NOTE
    api_terms = " ".join((DATA / "api_terms_excerpt.txt").read_text().split())
    for clause in ('Accounts registered by "bots" or other automated methods are not permitted',
                   "You may not download the same report from the Data Access Portal more than three times within "
                   "a 12-month period"):
        assert clause in api_terms and clause in ercot.TERMS_NOTE
    assert licensing.licence_for(ercot.SOURCE_NAME)["status"] == "public" and not ercot.SOURCE.blocked


def _never(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request may be made without the key: {request.url}")


def test_without_the_key_records_needs_key_and_requests_nothing(committed, monkeypatch):
    for name in ("ERCOT_API_KEY", "ERCOT_API_USERNAME", "ERCOT_API_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    config.reload_settings()
    out = ercot.run(fetcher=Fetcher(client=httpx.Client(transport=httpx.MockTransport(_never))))
    assert not out["ok"] and "needs key" in out["error"]
    run = committed.execute("SELECT ok, error FROM source_run WHERE source_id = 'ercot_gis'").fetchone()
    assert run["ok"] is False and "ERCOT_API_KEY" in run["error"]
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("ERCOT_API_KEY", "sub-key-test")
    monkeypatch.setenv("ERCOT_API_USERNAME", "person@example.com")
    monkeypatch.setenv("ERCOT_API_PASSWORD", "pw-test")
    config.reload_settings()
    yield
    monkeypatch.undo()
    config.reload_settings()


def test_api_flow_downloads_each_report_once(committed, keyed, monkeypatch):
    monkeypatch.setattr(ercot, "MIN_ENTRIES", 1)
    calls: list[str] = []
    listing = {"_meta": {"totalRecords": 2}, "archives": [
        {"docId": 1283680517, "friendlyName": "Co-located_Battery_Identification_Report_September_2026",
         "postDatetime": "2026-10-07T15:55:23"},
        {"docId": 1281327706, "friendlyName": "GIS_Report_September2026", "postDatetime": "2026-10-01T16:08:28",
         "_links": {"endpoint": {"href": f"{ercot.ARCHIVE_URL}?download=1281327706"}}}]}
    book = gis_workbook()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.host}{request.url.path}?{request.url.query.decode()}")
        if request.url.host == ercot.TOKEN_HOST:
            assert b"password=pw-test" in request.content and "pw-test" not in str(request.url)
            return httpx.Response(200, json={"id_token": "tok", "token_type": "Bearer"})
        assert request.headers["Ocp-Apim-Subscription-Key"] == "sub-key-test"
        assert request.headers["Authorization"] == "Bearer tok"
        if "download=1281327706" in str(request.url):
            return httpx.Response(200, content=book, headers={"content-type": "application/octet-stream"})
        return httpx.Response(200, json=listing)

    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(handler)))
    first = ercot.run(fetcher=fetcher)
    assert first["ok"], first
    assert first["iso"]["ercot"]["entries"] == 4 and first["facts_new"] > 0
    assert first["ercot_gis"]["loaded"] == [{"doc_id": "1281327706", "name": "GIS_Report_September2026",
                                             "posted": "2026-10-01"}]
    assert sum("download=" in c for c in calls) == 1
    doc = documents.latest_document(ercot.SOURCE_ID, f"{ercot.ARCHIVE_URL}?download=1281327706")
    assert doc is not None and doc.external_id == "1281327706" and doc.doc_date == date(2026, 10, 1)
    assert "sub-key-test" not in json.dumps(doc.meta) and "tok" not in doc.url
    fact = committed.execute("SELECT value, source_name, valid_from, tag FROM fact_current "
                             "WHERE subject_id = 'ercot:24INR0459' AND field = 'status'").fetchone()
    assert (fact["value"], fact["source_name"], fact["valid_from"], fact["tag"]) == (
        "Approved for Energization", "ERCOT", date(2026, 10, 1), "F")
    second = ercot.run(fetcher=fetcher)
    assert second["ok"], second
    assert second["facts_new"] == 0 and second["ercot_gis"]["loaded"] == []
    assert sum("download=" in c for c in calls) == 1  # never downloaded twice


def test_schedule_and_cli():
    assert app.tasks["collect.ercot_gis"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.ercot_gis"] == ercot.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "ercot_gis"])._handler is ercot.collect_cli


@pytest.mark.network
def test_live_key_wall_and_robots_still_hold(committed):
    """Without a key the collector stays inert; the API still refuses keyless calls and robots.txt still
    disallows the MIS download path, so there is no other automated route to collect."""
    sources.upsert_spec(ercot.SOURCE)
    with httpx.Client(timeout=30) as client:
        robots = client.get("https://www.ercot.com/robots.txt").text
        denied = client.get(ercot.ARCHIVE_URL)
    assert "Disallow: /misdownload" in robots and "Disallow: /misapp" in robots
    assert denied.status_code == 401 and "subscription key" in denied.text
    if not config.get_settings().has_ercot_api():
        assert "needs key" in ercot.run()["error"]
        return
    first = ercot.run()
    assert first["ok"], first
    second = ercot.run()
    assert second["ok"] and second["facts_new"] == 0
