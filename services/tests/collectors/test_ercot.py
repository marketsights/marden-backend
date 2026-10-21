"""ercot: the terms and robots findings, the LLWG page parsers, the status-update parser (real decks captured
2026-10-21, trimmed: pptx without pictures, pdf pages 1, 3, 6 and 7 with every picture made one pixel), the
flow over captured pages (raw first, robots, one download per deck, extract queue, a second run writes nothing),
a bot challenge stopping the run, the schedule, and a live check of the page structure (network).

ERCOT publishes no per-project large-load list, so there is nothing per project to test: no campus matching,
no queue rows, no status-change events (see marden.collectors.ercot).
"""

from __future__ import annotations

import gzip
import itertools
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import ercot
from marden.collectors import ercot_parse as parse
from marden.collectors.rates_common import ParseError
from marden.core import documents, http, sources
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "ercot"
JUNE = DATA / "June-19-LLWG-Report.slides.pptx"
APRIL = DATA / "April-24-LLWG-Report_042426.slides.pptx"
FEBRUARY = DATA / "February-TAC-Report.pages-1-3-6-7.pdf"
CHAIR = DATA / "TAC-LLWG-Aug-2026.slides.pptx"  # the LLWG chair's report to TAC: not a status update
JUNE_URL = "https://www.ercot.com/files/docs/2026/06/18/June-19-LLWG-Report.pptx"
FEB_URL = "https://www.ercot.com/files/docs/2026/03/05/February-TAC-Report.pdf"
APRIL_URL = "https://www.ercot.com/files/docs/2026/04/24/April-24-LLWG-Report_042426.pptx"
A2E, NONSIM, SIM = (series for series, _ in parse.FIGURES)


def _html(name: str) -> bytes:
    return gzip.decompress((DATA / f"{name}.html.gz").read_bytes())


def test_terms_and_robots_allow_the_pages_read():
    terms = (DATA / "terms_of_use_excerpt.txt").read_text()
    for clause in ("raw data provided in public portions of this website may be used, reproduced, and redistributed "
                   "in compilations, charts, and analyses without maintaining such notices.",
                   "Use of this website in a manner that negatively affects the performance of this website or other "
                   "ERCOT systems is prohibited."):
        assert clause in terms and clause in " ".join(ercot.TERMS_NOTE.split())
    api = (DATA / "api_terms_excerpt.txt").read_text()
    assert "Automated screen scraping programs that access ERCOT URL(s)" in api
    assert "Prevent overloading of ERCOT computing resources" in api
    assert not ercot.SOURCE.blocked and ercot.SOURCE.cadence_minutes == 7 * 24 * 60
    from urllib.robotparser import RobotFileParser

    robots = RobotFileParser()
    robots.parse((DATA / "robots.txt").read_text().splitlines())
    for url in (ercot.LLWG_URL, "https://www.ercot.com/calendar/06192026-LLWG-Meeting", JUNE_URL):
        assert robots.can_fetch(ercot.ROBOTS_AGENT, url)
    assert not robots.can_fetch(ercot.ROBOTS_AGENT, "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1")
    assert not robots.can_fetch(ercot.ROBOTS_AGENT, "https://www.ercot.com/meetings/x")


def test_meetings_on_the_llwg_page():
    found = parse.meetings(_html("committees_tac_llwg"), ercot.LLWG_URL)
    assert len(found) == 14 and found == sorted(found)
    assert (date(2026, 6, 19), "https://www.ercot.com/calendar/06192026-LLWG-Meeting") in found
    assert (date(2026, 8, 21), "https://www.ercot.com/calendar/08212026-LLWG-Meeting-_-Webex") in found
    assert all("-LLWG-" in u for _, u in found)  # TAC, ROS, PRS, Board and Batch Zero meetings are not LLWG pages


def test_status_update_links_on_meeting_pages():
    base = "https://www.ercot.com/calendar/x"
    assert parse.report_links(_html("calendar_06192026-LLWG-Meeting"), base) == [
        parse.Link(JUNE_URL, "June 19 LLWG Report")]
    assert parse.report_links(_html("calendar_02192026-LLWG-Meeting"), base) == [
        parse.Link(FEB_URL, "February TAC Report")]
    april = parse.report_links(_html("calendar_04242026-LLWG-Meeting"), base)  # nine documents on the page
    assert april == [parse.Link(APRIL_URL, "April 24 LLWG Report_042426")]  # 'Report' runs into '_042426'
    march = parse.report_links(_html("calendar_03132026-LLWG-Meeting"), base)
    assert [lk.text for lk in march] == ["March TAC Report", "March TAC Report Updated_03262026"]
    assert parse.report_links(_html("calendar_07232026-LLWG-Meeting"), base) == []
    assert parse.posted_date(JUNE_URL) == date(2026, 6, 18) and parse.posted_date(ercot.LLWG_URL) is None


def test_parses_the_june_deck():
    report = parse.parse_status_update(JUNE.read_bytes())
    assert report is not None
    assert (report.report_date, report.as_of, report.as_of_stated) == (date(2026, 6, 19), date(2026, 6, 18), True)
    figs = {f.series_id: f for f in report.figures}
    assert {k: f.value for k, f in figs.items()} == {A2E: 8927, NONSIM: 3900, SIM: 3675}
    assert figs[A2E].loc == "slide 8" and figs[SIM].loc == "slide 9"
    assert figs[SIM].quote == ("ERCOT has observed a simultaneous monthly peak consumption of 3675 MW in June 2026 "
                               "which is a slight decrease since May 2026.")


def test_as_of_without_a_year_takes_the_decks_year():
    report = parse.parse_status_update(APRIL.read_bytes())
    assert report is not None and (report.report_date, report.as_of) == (date(2026, 4, 24), date(2026, 4, 22))
    assert {f.series_id: f.value for f in report.figures} == {A2E: 9042, NONSIM: 4008, SIM: 3701}


def test_parses_the_february_pdf():
    report = parse.parse_status_update(FEBRUARY.read_bytes())
    assert report is not None
    assert (report.report_date, report.as_of, report.as_of_stated) == (date(2026, 2, 25), date(2026, 2, 25), False)
    figs = {f.series_id: f for f in report.figures}
    assert {k: f.value for k, f in figs.items()} == {A2E: 8876, NONSIM: 3998, SIM: 3846}
    assert "in February 2025" in figs[NONSIM].quote  # ERCOT's year typo stays in the quote, not in a value
    # page 2 (slide 3) says "non- simultaneous peak load consumption": not read as the simultaneous peak
    assert figs[SIM].loc == "page 4"


def test_other_decks_and_files():
    assert parse.parse_status_update(CHAIR.read_bytes()) is None
    with pytest.raises(ParseError):
        parse.parse_status_update(b"<html>not a deck</html>")
    assert parse.is_challenge((DATA / "incapsula_403.html").read_bytes())
    assert not parse.is_challenge(_html("committees_tac_llwg"))  # normal pages load an Incapsula script too


@pytest.fixture
def fast_rate() -> Iterator[None]:
    http.set_rate_limit(ercot.HOST, 1000.0)
    yield
    http.set_rate_limit(ercot.HOST, ercot.PER_SECOND)


def _site(calls: list[str], challenge_on: str | None = None) -> Fetcher:
    """ERCOT as captured. HTML gets a fresh token per response, as ERCOT's does; meeting pages that were not
    captured are served the captured July page, which lists no documents."""
    token = itertools.count()
    pages = {ercot.LLWG_URL: _html("committees_tac_llwg"),
             **{f"https://www.ercot.com/calendar/{d}-LLWG-Meeting": _html(f"calendar_{d}-LLWG-Meeting")
                for d in ("02192026", "04242026", "06192026", "07232026")}}
    files = {JUNE_URL: JUNE.read_bytes(), FEB_URL: FEBRUARY.read_bytes(), APRIL_URL: APRIL.read_bytes()}

    def handler(request: httpx.Request) -> httpx.Response:
        url = f"https://{request.url.host}{request.url.path}"
        calls.append(url)
        if challenge_on and challenge_on in url:
            return httpx.Response(403, content=(DATA / "incapsula_403.html").read_bytes())
        if url == ercot.ROBOTS_URL:
            return httpx.Response(200, content=(DATA / "robots.txt").read_bytes())
        if url in files:
            return httpx.Response(200, content=files[url], headers={"etag": '"x"'})
        if url.startswith("https://www.ercot.com/calendar/") or url == ercot.LLWG_URL:
            body = pages.get(url, pages["https://www.ercot.com/calendar/07232026-LLWG-Meeting"])
            return httpx.Response(200, content=body + f"<!-- rid={next(token)} -->".encode(),
                                  headers={"content-type": "text/html"})
        return httpx.Response(404)

    return Fetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)


def test_run_twice_over_captured_pages(committed, fast_rate):
    calls: list[str] = []
    files_ = (JUNE_URL, FEB_URL, APRIL_URL)
    fetcher = _site(calls)
    first = ercot.run(fetcher=fetcher, today=date(2026, 7, 1))
    assert first["ok"], first
    info = first["ercot"]
    assert info["meetings_listed"] == 14 and info["meeting_pages_read"] == 8  # Jan 22 .. Jun 19
    assert [r["url"] for r in info["reports"]] == [FEB_URL, APRIL_URL, JUNE_URL]
    assert info["per_project_rows"] == 0 and info["robots_disallowed"] == []
    assert first["facts_new"] == 9 and first["documents_new"] == 1 + 1 + 8 + 3
    row = committed.execute(
        "SELECT value, tag, source_name, source_url, valid_from, unit, quote_loc, producer, status FROM fact_current "
        "WHERE subject_type = 'series' AND subject_id = %s AND field = 'value'", (A2E,)).fetchone()
    assert (row["value"], row["tag"], row["source_name"], row["source_url"], row["valid_from"], row["unit"],
            row["quote_loc"], row["producer"], row["status"]) == (
        8927, "F", "ERCOT", JUNE_URL, date(2026, 6, 18), "MW", "slide 8", "collector:ercot", "published")
    deck = documents.latest_document(ercot.SOURCE_ID, JUNE_URL)
    assert deck is not None and deck.extract_state == "queued" and deck.doc_date == date(2026, 6, 18)
    assert deck.doc_type == "llwg_report_pptx" and deck.meta["meeting"].endswith("06192026-LLWG-Meeting")

    downloads = sum(u in files_ for u in calls)
    second = ercot.run(fetcher=fetcher, today=date(2026, 7, 1))
    assert second["ok"], second
    assert second["facts_new"] == 0 and second["documents_new"] == 0
    assert second["ercot"]["meeting_pages_read"] == 4  # meetings in the last RECENT_DAYS (Apr 23 .. Jun 19)
    assert downloads == 3 and sum(u in files_ for u in calls) == downloads  # each deck downloaded once
    n = committed.execute("SELECT count(*) AS n FROM fact WHERE producer = 'collector:ercot'").fetchone()["n"]
    assert n == 9


def test_a_bot_challenge_stops_the_run(committed, fast_rate):
    calls: list[str] = []
    out = ercot.run(fetcher=_site(calls, challenge_on="02192026"), today=date(2026, 7, 1))
    assert not out["ok"] and "BotChallenge" in out["error"] and "bot protection" in out["error"]
    assert not any("06192026" in u for u in calls)  # nothing after the challenge
    src = sources.get_source(ercot.SOURCE_ID)
    assert src is not None and "bot protection" in src["last_error"]
    assert committed.execute("SELECT count(*) AS n FROM fact WHERE producer = 'collector:ercot'").fetchone()["n"] == 0


def test_schedule_and_cli():
    assert app.tasks["collect.ercot"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.ercot"] == ercot.CRON_UTC
    assert cli.build_parser().parse_args(["collect", "ercot"])._handler is ercot.collect_cli


@pytest.mark.network
def test_live_pages_still_have_the_structure():
    """The LLWG page still lists meeting pages and the June 19 meeting page still links the status update."""
    sources.upsert_spec(ercot.SOURCE)
    fetcher = Fetcher()
    listed = parse.meetings(ercot._get(ercot.LLWG_URL, fetcher).content, ercot.LLWG_URL)
    assert len(listed) >= 10
    june_page = "https://www.ercot.com/calendar/06192026-LLWG-Meeting"
    june = ercot._get(june_page, fetcher)
    assert JUNE_URL in [lk.url for lk in parse.report_links(june.content, june_page)]
