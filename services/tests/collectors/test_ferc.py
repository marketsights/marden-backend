"""ferc: eLibrary search responses and files (real captures), the extraction queue rules, the terms note,
the watch list from seed-shaped rows, an end-to-end run with a rerun, a stop at bot protection, the
schedule and a live run (network)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import ferc
from marden.collectors import ferc_api as api
from marden.collectors.ferc_parse import FileRef, check_docket, parse_search
from marden.collectors.rates_common import ParseError
from marden.core.facts import FactWriter
from marden.core.http import Fetcher, FetchResponse, TokenBucket
from marden.core.queue import app
from marden.core.records import EventWriter
from marden.core.sources import upsert_spec

DATA = Path(__file__).parent / "data" / "ferc"


def _filings(name: str) -> dict[str, object]:
    total, filings = parse_search((DATA / name).read_bytes())
    assert total == len(filings)
    return {f.accession: f for f in filings}


def test_parses_a_docket_search():
    rows = _filings("search_cp26_80.json")
    assert len(rows) == 18
    oracle = rows["20261013-5316"]
    assert oracle.title == ("Renewed Request of Oracle Corporation for Expedited Issuance of Order re the Green Chile "
                            "Project under CP26-80.")
    assert (oracle.category, oracle.filed, oracle.day, oracle.dockets, oracle.filers) == (
        "Submittal", date(2026, 10, 13), date(2026, 10, 13), ("CP26-80-000",), ("Oracle Corporation",))
    assert oracle.filelist_url == "https://elibrary.ferc.gov/eLibrary/filelist?accession_number=20261013-5316"
    main, why = oracle.main_file()
    assert why is None and main.file_type == "PDF" and main.size == 17539
    assert main.url == "https://elibrary.ferc.gov/eLibrary/filedownload?fileid=0A9BFD62-6D58-CCFE-998F-A1368BB00000"
    assert oracle.queue_reasons() == []
    # filed 10/13 for a document dated 10/12: a submittal's own date is its filed date
    supplement = rows["20261013-5142"]
    assert (supplement.filed, supplement.issued, supplement.day) == (date(2026, 10, 13), date(2026, 10, 12),
                                                                     date(2026, 10, 13))
    assert supplement.queue_reasons() == ["certificate_filing"]  # Prior Notice Request under Blanket Certificate
    assert rows["20261013-5143"].main_file() == (None, "not_public")  # availability N: never fetched
    assert rows["20261005-5204"].main_file()[0].file_type == "DOCX"  # no PDF: the first file
    sierra = rows["20261005-5158"]
    assert len(sierra.files) == 12 and sierra.main_file()[0].name.startswith("Sierra Club et al, Comments")


def test_queues_orders_deficiency_letters_and_certificates():
    order = _filings("search_el26_101.json")["20260924-3089"]
    assert (order.category, order.day, order.queue_reasons()) == ("Issuance", date(2026, 9, 24), ["order"])
    letter = _filings("search_er26_3531.json")["20261015-3058"]
    assert letter.queue_reasons() == ["deficiency_letter"] and letter.doc_type == "ferc_issuance"
    pjm = _filings("search_el26_108.json")
    assert pjm["20260929-3099"].queue_reasons() == ["order"]  # order with a commissioner's concurrence
    assert pjm["20261016-3047"].queue_reasons() == []  # notice establishing an answer period
    docless = pjm["20261020-5064"]
    assert docless.main_file()[0].file_type == "TXT" and docless.queue_reasons() == []
    assert pjm["20261006-5069"].main_file() == (None, "not_public")
    withheld = FileRef("X", "(PRIV) Attachment C - Cultural.pdf", "PDF", 10, None)
    assert withheld.withheld


def test_refuses_failed_or_garbled_responses():
    with pytest.raises(ParseError, match="Object reference not set"):
        parse_search((DATA / "search_failed.json").read_bytes())
    with pytest.raises(ParseError, match="not JSON"):
        parse_search(b"<html>maintenance</html>")
    assert parse_search((DATA / "search_er26_1323_empty.json").read_bytes()) == (0, [])


def test_docket_numbers_and_window():
    assert check_docket(" cp26-80") == "CP26-80"
    with pytest.raises(ValueError):
        check_docket("CP26-80-000; DROP")
    today = date(2026, 10, 21)
    assert ferc.window_start(today, None) == date(2026, 9, 21)
    assert ferc.window_start(today, date(2026, 10, 21)) == date(2026, 9, 21)  # fixed within a day: reruns match
    assert ferc.window_start(today, date(2026, 8, 1)) == date(2026, 7, 25)  # after an outage: a week before it


def _flat(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", text).split())


def test_terms_note_quotes_the_saved_sources():
    note = ferc.TERMS_NOTE
    cfr = _flat((DATA / "ecfr_18_388_106.xml").read_text())
    quotes = re.findall(r'388\.106\((?:a|b)\)[^"]*"([^"]+)"', note)
    assert len(quotes) == 2
    for quote in quotes:
        for part in quote.split(" ... "):
            assert part.rstrip(";") in cfr, part
    pia = _flat((DATA / "pia_elibrary_excerpt.txt").read_text())
    for part in ("The public and FERC staff have access to eLibrary", "printed or downloaded at one’s computer",  # noqa: RUF001
                 "End Users: All end users have read access to the public data in the eLibrary repository."):
        assert part in pia and part in note
    robots = (DATA / "robots_www_ferc_gov.txt").read_text()
    assert "Disallow: /search/" in robots and "elibrary" not in robots.lower()
    page = (DATA / "challenge_www_ferc_gov.html").read_bytes()
    challenge = FetchResponse("u", "u", 403, page, {"content-type": "text/html; charset=UTF-8"})
    assert api.interstitial(challenge) and not ferc.SOURCE.blocked


def _seed_rows(committed, writer_day=date(2026, 10, 20)) -> None:
    """Rows shaped like the seed load's: docket entities, linked_sites, a plant's dockets, a docket-sheet event."""
    committed.execute("INSERT INTO site (slug, name) VALUES ('jupiter-t', 'Jupiter test'), ('lighthouse-t', 'LH')")
    committed.execute("INSERT INTO power_plant (id, name) VALUES ('oklo-t', 'Oklo test')")
    committed.execute("INSERT INTO campus (id, name) VALUES ('lh-campus-t', 'LH campus')")
    for number in ("CP26-80", "ER26-3531", "EL26-108", "AD24-11"):
        committed.execute("INSERT INTO entity (id, name, kind, meta) VALUES (%s, %s, 'docket', %s)",
                          (f"docket-ferc-{number.lower()}", f"Docket FERC {number}",
                           json.dumps({"proceeding_key": f"FERC_{number}"})))
    facts = FactWriter(committed)
    common = {"tag": "R", "source_name": "test", "valid_from": writer_day, "producer": "seed:test",
              "status": "published"}
    facts.add("entity", "docket-ferc-cp26-80", "linked_sites", ["jupiter-t", "no-such-site"], **common)
    facts.add("entity", "docket-ferc-er26-3531", "linked_sites", ["lighthouse-t"], **common)
    facts.add("entity", "docket-ferc-el26-108", "linked_sites", ["jupiter-t"], **common)
    facts.add("plant", "oklo-t", "dockets", [{"agency": "FERC", "number": "EL26-101"}, {"agency": "NRC"}], **common)
    EventWriter(committed).add(
        "campus", "lh-campus-t", writer_day, "docket", "FERC ER26-3531: Letter", tag="F", source_name="FERC eLibrary",
        source_url="https://elibrary.ferc.gov/eLibrary/docketsheet?DOCKET=ER26-3531-000", status="published")


def test_watch_list_comes_from_seed_rows(committed):
    _seed_rows(committed)
    watched, unlinked = ferc.watched_dockets(committed)
    got = {w.docket: w.subject_keys() for w in watched}
    assert got == {"CP26-80": ["site:jupiter-t"], "EL26-101": ["plant:oklo-t"], "EL26-108": ["site:jupiter-t"],
                   "ER26-3531": ["campus:lh-campus-t", "site:lighthouse-t"]}
    assert unlinked == ["AD24-11"]


SEARCHES = {"CP26-80": "trimmed_cp26_80.json", "EL26-108": "trimmed_el26_108.json",
            "ER26-3531": "trimmed_er26_3531.json", "EL26-101": "search_el26_101.json"}
FILES = {"0A9BFD62-6D58-CCFE-998F-A1368BB00000": "file_20261013-5316.pdf",
         "DDBEF936-7E11-C918-9295-A140AAA00000": "file_20261015-3058.docx",
         "15376640-9C1A-CA10-97FB-A0D53B400000": "file_20260924-3089.docx"}


def _client(*, challenge: bool = False) -> api.ElibraryClient:
    el108 = json.loads((DATA / "trimmed_el26_108.json").read_text())
    files = {**FILES, el108["searchHits"][0]["transmittals"][0]["fileId"]: "file_20261020-5064.txt"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.headers["user-agent"].startswith("MardenBot/")
        if challenge:
            return httpx.Response(403, content=(DATA / "challenge_www_ferc_gov.html").read_bytes(),
                                  headers={"content-type": "text/html; charset=UTF-8"})
        body = json.loads(request.content)
        if str(request.url) == api.SEARCH_URL:
            name = SEARCHES[body["docketSearches"][0]["docketNumber"]]
            return httpx.Response(200, content=(DATA / name).read_bytes(), headers={"content-type": "application/json"})
        assert str(request.url) == api.DOWNLOAD_URL
        return httpx.Response(200, content=(DATA / files[body["fileidLst"][0]]).read_bytes(),
                              headers={"content-type": "application/octet-stream"})

    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(handler)))
    return api.ElibraryClient(ferc.SOURCE_ID, fetcher=fetcher, bucket=TokenBucket(1000, 1000), sleep=lambda s: None)


def test_run_records_files_events_and_reruns_clean(committed):
    _seed_rows(committed)
    first = ferc.run(today=date(2026, 10, 21), client=_client())
    assert first["ok"], first
    assert first["window"] == {"posted_from": "2026-09-21", "posted_to": "2026-10-21"}
    assert (first["dockets_watched"], first["dockets_unlinked"]) == (4, ["AD24-11"])
    assert (first["files_fetched"], first["files_not_fetched"], first["queued_for_extraction"]) == (
        4, {"not_public": 2}, 2)
    assert first["documents_new"] == 8 and first["requests"] == 8  # 4 search pages + 4 main files
    docs = {r["external_id"]: r for r in committed.execute(
        "SELECT * FROM document WHERE source_id = 'ferc' AND doc_type <> 'ferc_search'").fetchall()}
    assert {k: (v["doc_type"], v["extract_state"], v["content_type"]) for k, v in docs.items()} == {
        "20261013-5316": ("ferc_submittal", "none", "application/pdf"),
        "20261020-5064": ("ferc_submittal", "none", "text/plain"),
        "20261015-3058": ("ferc_issuance", "queued",
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "20260924-3089": ("ferc_issuance", "queued",
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    letter = docs["20261015-3058"]
    assert letter["url"] == "https://elibrary.ferc.gov/eLibrary/filedownload?fileid=DDBEF936-7E11-C918-9295-A140AAA00000"
    assert letter["meta"]["queue_reasons"] == ["deficiency_letter"] and letter["meta"]["http"]["method"] == "POST"
    assert letter["meta"]["filers"] == ["OFFICE OF ENERGY MARKET REGULATION"]  # AUTHOR affiliations only
    assert letter["doc_date"] == date(2026, 10, 15) and letter["bytes"] == 82725
    events = committed.execute(
        """SELECT subject_type || ':' || subject_id AS s, event_date, status, material, tag, source_url, document_id
             FROM event WHERE source_name = 'FERC eLibrary' AND source_url LIKE '%%filelist%%'
            ORDER BY 1, 2""").fetchall()
    assert len(events) == first["events_new"] == 7  # 2 + 2 filings on jupiter-t, 1 letter x 2 subjects, 1 order
    assert {e["tag"] for e in events} == {"F"}
    candidates = [(e["s"], e["event_date"]) for e in events if e["status"] == "candidate"]
    assert sorted(candidates) == [("campus:lh-campus-t", date(2026, 10, 15)), ("plant:oklo-t", date(2026, 9, 24)),
                                  ("site:lighthouse-t", date(2026, 10, 15))]  # 'filing is deficient', 'order denying'
    assert first["reviews_opened"] == 3 and committed.execute(
        "SELECT count(*) AS n FROM review_item WHERE kind = 'event' AND status = 'open'").fetchone()["n"] == 3
    nonpublic = committed.execute("SELECT document_id FROM event WHERE source_url LIKE '%%20261013-5143'").fetchone()
    assert committed.execute("SELECT doc_type FROM document WHERE id = %s",
                             (nonpublic["document_id"],)).fetchone()["doc_type"] == "ferc_search"
    assert committed.execute(
        "SELECT count(*) AS n FROM read_model_dirty WHERE view = 'site_detail' AND key = 'jupiter-t'").fetchone()["n"]

    second = ferc.run(today=date(2026, 10, 21), client=_client())
    assert second["ok"], second
    assert (second["documents_new"], second["events_new"], second["files_fetched"], second["reviews_opened"]) == (
        0, 0, 0, 0)
    assert second["requests"] == 4 and second["events_seen"] == first["events_seen"]
    runs = committed.execute("SELECT ok FROM source_run WHERE source_id = 'ferc' ORDER BY id").fetchall()
    assert [r["ok"] for r in runs] == [True, True]


def test_listed_file_size_is_not_the_byte_count(committed):
    """eComment text files list fileSize 2 (20261005-5003 sent 2,440 bytes): the file is still stored."""
    upsert_spec(ferc.SOURCE)
    filing = _filings("search_cp26_80.json")["20261013-5316"]
    main = replace(filing.main_file()[0], size=2)
    doc = _client().download(filing, main, meta=filing.as_meta(), extract_state="none")
    assert doc.was_new and doc.bytes == 17539 and doc.meta["main_file"]["size"] == 2


def test_bot_protection_stops_the_run(committed):
    _seed_rows(committed)
    out = ferc.run(today=date(2026, 10, 21), client=_client(challenge=True))
    assert not out["ok"] and "Challenged" in out["error"] and "no evasion" in out["error"]
    assert out["requests"] == 1
    src = committed.execute("SELECT last_error FROM source WHERE id = 'ferc'").fetchone()
    assert "403" in src["last_error"]
    written = committed.execute("SELECT count(*) AS n FROM event WHERE source_url LIKE '%%filelist%%'").fetchone()
    assert written["n"] == 0
    with pytest.raises(ValueError, match="not watched"):
        ferc.run(dockets=["AD24-11"], client=_client())


def test_schedule_and_cli():
    assert app.tasks["collect.ferc"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.ferc"] == ferc.CRON_UTC == "23 * * * *"
    assert ferc.SOURCE.cadence_minutes == 60
    assert cli.build_parser().parse_args(["collect", "ferc", "--list"])._handler is ferc.collect_cli


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    committed.execute("INSERT INTO site (slug, name) VALUES ('jupiter-live', 'Jupiter live test')")
    committed.execute("INSERT INTO entity (id, name, kind, meta) VALUES ('docket-ferc-cp26-80', 'Docket FERC CP26-80', "
                      "'docket', '{\"proceeding_key\": \"FERC_CP26-80\"}')")
    FactWriter(committed).add("entity", "docket-ferc-cp26-80", "linked_sites", ["jupiter-live"], tag="R",
                              source_name="test", valid_from=date(2026, 10, 20), producer="seed:test",
                              status="published")
    first = ferc.run(window_days=7)
    assert first["ok"], first
    assert first["dockets"]["CP26-80"]["pages"] == 1 and first["filings_seen"] == first["dockets"]["CP26-80"]["hits"]
    second = ferc.run(window_days=7)
    assert second["ok"], second
    assert (second["documents_new"], second["events_new"], second["files_fetched"]) == (0, 0, 0)
