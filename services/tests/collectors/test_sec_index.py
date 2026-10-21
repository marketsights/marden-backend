"""sec_index: parsers on real captured EDGAR responses, a full run on the test database (twice), and
one live check against sec.gov.

Captured 2026-10-21 with SEC_USER_AGENT (trimmed to the records the tests need):
- form.20261020.idx: 99 real lines of that day's daily form index (header kept).
- form.20261021.idx.403.xml: what sec.gov answers for today's index before the night build.
- CIK0001769628.json: CoreWeave submissions, `recent` cut to filings since 2026-08-05.
- CIK0001999261.json, CIK0002089208.json: TEG Overseas and FFC Depositor submissions (whole files).
- CIK0009999999.json.404.xml: data.sec.gov's answer for a CIK that does not exist.
- three `-index.htm` filing index pages and the documents of the two Oct 20 filings.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import sec_index
from marden.collectors import sec_index_parse as p
from marden.collectors import sec_index_store as store
from marden.core.config import get_settings
from marden.core.http import Fetcher, FetchError
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "sec_index"
ARCH = "https://www.sec.gov/Archives/edgar"
SUB = "https://data.sec.gov/submissions"


def _read(name: str) -> bytes:
    return (DATA / name).read_bytes()


# ---------------------------------------------------------------- parsers
def test_parse_daily_form_index():
    lines, unparsed = p.parse_form_index(_read("form.20261020.idx").decode())
    assert len(lines) == 99 and unparsed == 0
    assert lines[0] == p.IndexLine("1-A", "Masterworks Vault 3, LLC", "1983146", date(2026, 10, 20),
                                   "edgar/data/1983146/0001493152-26-047712.txt", "0001493152-26-047712")
    by_form = {line.form for line in lines}
    assert {"SCHEDULE 13D", "SCHEDULE 13D/A", "8-K/A", "424B2", "ABS-15G", "10-D", "NPORT-P"} <= by_form
    teg = [line for line in lines if line.cik == "1999261"]
    assert [t.form for t in teg] == ["3", "3", "3", "SCHEDULE 13D"]
    assert [(t.form, t.accession) for t in teg if p.is_relevant_form(t.form)] == [
        ("SCHEDULE 13D", "0001213900-26-111068")]
    assert sum(1 for line in lines if line.cik == "2089208") == 2  # the index lists FFC twice (two roles)
    assert next(line for line in lines if line.cik == "930245").company == "Agassi Sports Entertainment Corp."
    with pytest.raises(ValueError):
        p.parse_form_index("<Error><Code>AccessDenied</Code></Error>")


def test_parse_submissions_and_older_pages():
    page = p.parse_submissions(_read("CIK0001769628.json"))
    assert (page.cik, page.name, len(page.filings)) == ("1769628", "CoreWeave, Inc.", 143)
    relevant = [f for f in page.filings if p.is_relevant_form(f.form)]
    assert [(f.accession, f.form, f.filing_date.isoformat()) for f in relevant] == [
        ("0001769628-26-000432", "8-K", "2026-09-22"),
        ("0001769628-26-000429", "8-K", "2026-09-17"),
        ("0001628280-26-062362", "424B5", "2026-09-17"),
        ("0001769628-26-000366", "10-Q", "2026-08-12"),
        ("0001769628-26-000362", "8-K", "2026-08-11"),
        ("0001769628-26-000357", "8-K", "2026-08-10"),
    ]
    assert relevant[0].items == ("1.01", "3.02", "8.01", "9.01")
    assert relevant[0].report_date == date(2026, 9, 17) and relevant[2].report_date is None
    assert p.pages_needed(page, date(2026, 9, 1)) == []
    assert p.pages_needed(page, date(2025, 3, 1)) == ["CIK0001769628-submissions-001.json"]
    assert p.pages_needed(page, date(2025, 9, 1)) == []  # page 001 ends 2025-08-26, before the window
    # a page file holds the bare arrays
    arrays = json.loads(_read("CIK0001769628.json"))["filings"]["recent"]
    assert len(p.parse_submissions(json.dumps(arrays), "1769628").filings) == 143


def test_parse_8k_filing_index():
    url = f"{ARCH}/data/1769628/000176962826000432/0001769628-26-000432-index.htm"
    fi = p.parse_filing_index(_read("0001769628-26-000432-index.htm"), url)
    assert (fi.form, fi.form_description, fi.filing_date, fi.period) == (
        "8-K", "Current report", date(2026, 9, 22), date(2026, 9, 17))
    assert fi.accepted == "2026-09-22 16:17:14"
    assert [code for code, _ in fi.items] == ["1.01", "3.02", "8.01", "9.01"]
    assert fi.parties == (p.Party("CoreWeave, Inc.", "1769628", "Filer"),)
    docs, skipped = p.relevant_documents(fi)
    assert [(d.doc_type, primary) for d, primary in docs] == [
        ("8-K", True), ("EX-4.1", False), ("EX-10.1", False), ("EX-10.2", False), ("EX-99.1", False)]
    assert docs[0][0].url == f"{ARCH}/data/1769628/000176962826000432/crwv-20260917.htm"  # not the /ix viewer
    assert skipped == 1  # the complete submission text file
    assert {p.extract_state_for(fi.form, d.doc_type) for d, _ in docs} == {"queued"}
    assert p.event_title(fi, "1769628") == (
        "8-K: Entry into a Material Definitive Agreement; Unregistered Sales of Equity Securities; Other Events")


def test_parse_13d_and_abs15g_filing_indexes():
    fi = p.parse_filing_index(_read("0001213900-26-111068-index.htm"), f"{ARCH}/data/1999261/x/y-index.htm")
    assert fi.form == "SCHEDULE 13D" and fi.items == () and fi.period is None
    assert {(x.cik, x.role) for x in fi.parties} == {("1999261", "Subject"), ("2160471", "Filed by")}
    docs, skipped = p.relevant_documents(fi)
    assert [d.url for d, _ in docs] == [f"{ARCH}/data/1999261/000121390026111068/primary_doc.xml"]  # not xsl copy
    assert skipped == 2
    assert p.extract_state_for(fi.form, docs[0][0].doc_type) == "none"
    assert p.event_title(fi, "1999261").endswith("(filed by Abduvakhitova Irodakhon)")
    assert p.event_title(fi, "2160471").endswith("(subject: TEG OVERSEAS, INC.)")

    fi = p.parse_filing_index(_read("0001140361-26-040194-index.htm"), f"{ARCH}/data/2089208/x/y-index.htm")
    assert (fi.form, fi.period, fi.items[0][0]) == ("ABS-15G", date(2026, 10, 19), "2.01")
    docs, skipped = p.relevant_documents(fi)
    assert [(d.doc_type, d.description) for d, _ in docs] == [("ABS-15G", "ABS-15G"), ("EX-99.1", "EXHIBIT 99.1")]
    assert skipped == 2  # GRAPHIC and the submission text file
    assert p.event_title(fi, "2089208").startswith("ABS-15G: Unregistered Entity: Findings and Conclusions")


def test_form_and_document_rules():
    for form in ("8-K", "8-K/A", "10-Q", "10-K/A", "424B2", "424B5", "S-1/A", "F-1", "ABS-15G", "10-D",
                 "SC 13D", "SC 13D/A", "SCHEDULE 13D/A"):
        assert p.is_relevant_form(form), form
    for form in ("4", "144", "NPORT-P", "SCHEDULE 13G/A", "S-3ASR", "424H", "N-CSR"):
        assert not p.is_relevant_form(form), form
    assert p.extract_state_for("10-Q", "EX-10.1") == "queued"  # credit agreements, whatever the form
    assert p.extract_state_for("10-Q", "10-Q") == "none"
    assert p.extract_state_for("424B3", "424B3") == "queued"
    assert p.extract_state_for("8-K/A", "EX-99.1") == "queued"
    assert p.extract_state_for("ABS-15G", "EX-99.1") == "none"
    assert p.EXHIBIT.match("EX-10.1") and p.EXHIBIT.match("EX-4") and not p.EXHIBIT.match("EX-101.SCH")
    assert p.normalize_cik("0001769628") == "1769628" and p.normalize_cik("CIK1") is None
    assert p.daily_index_url(date(2026, 10, 20)) == f"{ARCH}/daily-index/2026/QTR4/form.20261020.idx"
    assert p.submissions_url("1769628") == f"{SUB}/CIK0001769628.json"
    assert p.previous_business_day(date(2026, 10, 19)) == date(2026, 10, 16)  # Monday -> Friday


def test_edgar_hours_gate():
    def ts(y, m, d, hh, mm):
        return int(datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp())

    assert sec_index.in_edgar_hours(None)
    assert sec_index.in_edgar_hours(ts(2026, 10, 21, 10, 0))  # 06:00 EDT Wednesday
    assert not sec_index.in_edgar_hours(ts(2026, 12, 2, 10, 0))  # 05:00 EST
    assert sec_index.in_edgar_hours(ts(2026, 10, 22, 3, 50))  # 23:50 EDT Wednesday
    assert sec_index.in_edgar_hours(ts(2026, 10, 24, 1, 0))  # Friday 21:00 EDT (Saturday in UTC)
    assert not sec_index.in_edgar_hours(ts(2026, 10, 24, 5, 0))  # Saturday 01:00 EDT
    assert not sec_index.in_edgar_hours(ts(2026, 10, 25, 14, 0))  # Sunday


def test_task_schedule_and_cli_are_registered():
    task = app.tasks["collect.sec_index"]
    assert task.queue == "collect"
    crons = {pid: t.cron for (name, pid), t in app.periodic_registry.periodic_tasks.items()
             if name == "collect.sec_index"}
    assert crons == {"day": sec_index.DAY_CRON, "evening": sec_index.EVENING_CRON}
    assert app.tasks["collect.sec_index_backfill"].queue == "collect"
    assert "sec_index" in cli._groups["collect"]


# ---------------------------------------------------------------- a run on the test database
ROUTES: dict[str, tuple[int, str]] = {
    f"{ARCH}/daily-index/2026/QTR4/form.20261020.idx": (200, "form.20261020.idx"),
    f"{ARCH}/daily-index/2026/QTR4/form.20261021.idx": (403, "form.20261021.idx.403.xml"),
    f"{SUB}/CIK0001999261.json": (200, "CIK0001999261.json"),
    f"{SUB}/CIK0002089208.json": (200, "CIK0002089208.json"),
    f"{SUB}/CIK0009999999.json": (404, "CIK0009999999.json.404.xml"),
    f"{ARCH}/data/1999261/000121390026111068/0001213900-26-111068-index.htm":
        (200, "0001213900-26-111068-index.htm"),
    f"{ARCH}/data/2089208/000114036126040194/0001140361-26-040194-index.htm":
        (200, "0001140361-26-040194-index.htm"),
    f"{ARCH}/data/1999261/000121390026111068/primary_doc.xml": (200, "primary_doc.xml"),
    f"{ARCH}/data/2089208/000114036126040194/ef20084046_abs15g.htm": (200, "ef20084046_abs15g.htm"),
    f"{ARCH}/data/2089208/000114036126040194/ef20084046_ex99-1.htm": (200, "ef20084046_ex99-1.htm"),
}
INDEX_ETAG = '"eac40877b73b4bc2dc2f11817f765870-gzip"'


class CapturedSec:
    """Serves the captured responses at their real URLs; anything else fails the test."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        assert "/cgi-bin" not in url and "/search" not in url
        assert request.headers["user-agent"] == get_settings().sec_user_agent
        status, name = ROUTES[url]
        headers = {"content-type": "application/octet-stream"}
        if name == "form.20261020.idx":
            if request.headers.get("if-none-match") == INDEX_ETAG:
                return httpx.Response(304, headers={"etag": INDEX_ETAG})
            headers["etag"] = INDEX_ETAG
        return httpx.Response(status, content=_read(name), headers=headers)


@pytest.fixture
def watched(committed):
    committed.execute(
        """INSERT INTO entity (id, name, kind, cik) VALUES
             ('teg-overseas-inc', 'TEG OVERSEAS, INC.', 'company', '1999261'),
             ('ffc-depositor-llc', 'FFC Depositor LLC', 'spv', '0002089208'),
             ('no-such-filer', 'No such filer', 'company', '9999999'),
             ('no-cik', 'No CIK', 'company', NULL);
           INSERT INTO entity (id, name, kind, parent_id)
           VALUES ('ffc-issuer', 'FFC Issuer LLC', 'spv', 'ffc-depositor-llc');
           INSERT INTO site (slug, name) VALUES ('site-a', 'Site A'), ('site-b', 'Site B'), ('site-c', 'Site C'),
                                                ('site-d', 'Site D');
           INSERT INTO instrument (id, name, kind, issuer_entity_id, issuer_name, site_slug) VALUES
             ('EX-TEST-001', 'FFC notes', 'abs', 'ffc-depositor-llc', 'FFC Depositor LLC', 'site-a'),
             ('EX-TEST-002', 'FFC 2026-1 A', 'abs', 'ffc-issuer', 'FFC Issuer LLC', 'site-b');
           INSERT INTO chain_party (site_slug, role, entity_id, label) VALUES
             ('site-c', 'developer', 'teg-overseas-inc', 'TEG'), ('site-d', 'tenant', 'teg-overseas-inc', 'TEG')"""
    )
    return committed


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in ("document", "event")}


def test_run_records_new_filings_once(watched):
    transport = CapturedSec()
    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(transport)), backoff_base=0.01)
    first = sec_index.run(today=date(2026, 10, 21), days=2, fetcher=fetcher)
    assert first.ok, first.summary()
    assert (first.watched_ciks, first.missing_ciks) == (3, ["9999999"])
    assert first.index_days_read == ["2026-10-20"] and first.index_days_pending == ["2026-10-21"]
    assert (first.filings_found, first.filings_new, first.events_new) == (2, 2, 7)
    assert first.documents_by_type == {"edgar_daily_index": 1, "sec_submissions": 2, "filing_index": 2,
                                       "SCHEDULE 13D": 1, "ABS-15G": 1, "EX-99.1": 1}
    docs = watched.execute(
        "SELECT doc_type, doc_date, external_id, extract_state FROM document WHERE doc_type <> ALL(%s) ORDER BY url",
        (["edgar_daily_index", "sec_submissions"],),
    ).fetchall()
    assert {(d["doc_type"], d["external_id"], d["extract_state"]) for d in docs} == {
        ("filing_index", "0001140361-26-040194", "none"), ("filing_index", "0001213900-26-111068", "none"),
        ("ABS-15G", "0001140361-26-040194", "none"), ("EX-99.1", "0001140361-26-040194", "none"),
        ("SCHEDULE 13D", "0001213900-26-111068", "none"),
    }
    assert {d["doc_date"] for d in docs} == {date(2026, 10, 20)}
    events = watched.execute(
        "SELECT subject_type, subject_id, event_date, kind, tag, status, title, source_url FROM event ORDER BY 1, 2"
    ).fetchall()
    assert [(e["subject_type"], e["subject_id"]) for e in events] == [  # no event on site-d (TEG is its tenant)
        ("entity", "ffc-depositor-llc"), ("entity", "teg-overseas-inc"), ("instrument", "EX-TEST-001"),
        ("instrument", "EX-TEST-002"), ("site", "site-a"), ("site", "site-b"), ("site", "site-c")]
    assert {(e["kind"], e["tag"], e["status"], e["event_date"]) for e in events} == {
        ("filing", "F", "published", date(2026, 10, 20))}
    assert events[1]["title"] == ("SCHEDULE 13D: General Statement of Acquisition of Beneficial Ownership "
                                  "(filed by Abduvakhitova Irodakhon)")
    assert events[0]["source_url"].endswith("0001140361-26-040194-index.htm")
    dirty = {r["view"] for r in watched.execute("SELECT view FROM read_model_dirty").fetchall()}
    assert {"payer", "instrument_detail", "site_detail"} <= dirty
    before = _counts(watched)

    transport.requests.clear()
    second = sec_index.run(today=date(2026, 10, 21), days=2, fetcher=fetcher)
    assert second.ok, second.summary()
    assert (second.documents_new, second.events_new, second.filings_known) == (0, 0, 2)
    assert _counts(watched) == before
    assert sorted(transport.requests) == sorted([
        f"{ARCH}/daily-index/2026/QTR4/form.20261020.idx", f"{ARCH}/daily-index/2026/QTR4/form.20261021.idx",
        f"{SUB}/CIK0001999261.json", f"{SUB}/CIK0002089208.json", f"{SUB}/CIK0009999999.json"])

    # an instrument linked to a filer later gets the filing event on the next run, with no new fetches
    watched.execute("""INSERT INTO instrument (id, name, kind, issuer_entity_id, issuer_name)
                       VALUES ('EX-TEST-003', 'TEG loan', 'loan', 'teg-overseas-inc', 'TEG')""")
    transport.requests.clear()
    healed = sec_index.run(today=date(2026, 10, 21), days=2, fetcher=fetcher)
    assert (healed.documents_new, healed.events_new, healed.filings_new) == (0, 1, 1)
    assert len(transport.requests) == 5
    backfill = sec_index.run(today=date(2026, 10, 21), days=2, backfill=True, fetcher=fetcher)
    assert backfill.ok and (backfill.documents_new, backfill.events_new) == (0, 0)
    runs = watched.execute("SELECT ok, items_seen, items_new, meta FROM source_run ORDER BY id").fetchall()
    assert [(r["ok"], r["items_seen"], r["items_new"]) for r in runs] == [
        (True, 2, 2), (True, 2, 0), (True, 2, 1), (True, 2, 0)]
    assert runs[0]["meta"]["watched_ciks"] == 3
    source = watched.execute("SELECT terms_note, blocked, last_ok_at FROM source WHERE id = 'sec_index'").fetchone()
    assert "may be copied or further distributed" in source["terms_note"] and not source["blocked"]


def test_fund_registrants_are_index_only(watched):
    """A fund registrant's CIK is matched in the daily index but its submissions file is not polled
    (unless asked for by CIK)."""
    watched.execute("INSERT INTO entity (id, name, kind, cik) VALUES ('fund-trust', 'A Fund Trust', 'fund', '844779')")
    w = store.watched_ciks(watched)
    assert "844779" in w.entities and "844779" not in w.polled
    assert w.polled == {"1999261", "2089208", "9999999"}
    assert store.watched_ciks(watched, ["844779"]).polled == {"844779"}
    transport = CapturedSec()
    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(transport)), backoff_base=0.01)
    stats = sec_index.run(today=date(2026, 10, 21), days=2, fetcher=fetcher)
    assert stats.ok and (stats.watched_ciks, stats.polled_ciks) == (4, 3)
    assert not any("CIK0000844779" in u for u in transport.requests)


def test_failed_fetch_is_recorded_on_the_source(watched):
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"unavailable")

    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(down)), max_retries=0)
    stats = sec_index.run(today=date(2026, 10, 21), days=2, fetcher=fetcher)
    assert not stats.ok and "fetch(es) failed" in (stats.error or "")
    assert _counts(watched) == {"document": 0, "event": 0}
    source = watched.execute("SELECT last_error, last_ok_at FROM source WHERE id = 'sec_index'").fetchone()
    assert "HTTP 503" in source["last_error"] and source["last_ok_at"] is None


# ---------------------------------------------------------------- live
@pytest.mark.network
def test_live_submissions_and_filing_index():
    with Fetcher() as fetcher:
        page = p.parse_submissions(fetcher.get(p.submissions_url("1769628")).content)
        assert page.name and page.name.startswith("CoreWeave")
        latest = next(f for f in page.filings if p.is_relevant_form(f.form))
        url = p.filing_index_url(latest.cik, latest.accession)
        fi = p.parse_filing_index(fetcher.get(url).content, url)
        assert fi.accession == latest.accession and fi.filing_date == latest.filing_date
        docs, _ = p.relevant_documents(fi)
        assert docs and docs[0][1], "the primary document is listed first"
        day, resp = datetime.now(sec_index.EASTERN).date(), None
        for _ in range(5):  # the latest published daily index (today's appears at night; holidays have none)
            day = p.previous_business_day(day)
            try:
                resp = fetcher.get(p.daily_index_url(day))
                break
            except FetchError as exc:
                assert exc.status in (403, 404)
        assert resp is not None
        lines, unparsed = p.parse_form_index(resp.text())
        assert len(lines) > 100 and unparsed == 0
