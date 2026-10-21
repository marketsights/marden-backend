"""grid_evidence: grid-region review items resolved from quotes in stored primary documents.

The grid_join side is set up as grid_join leaves it (candidate facts from HIFLD boundaries, one review item per
subject and field); the documents are stored through the raw store under the collector's source, so the quote
check reads real bytes. One network test checks the listed quotes against the live SEC filings.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from marden.collectors import grid_evidence as ge
from marden.collectors import grid_join, grid_registry
from marden.collectors.grid_evidence_list import EVIDENCE, Doc, Statement
from marden.collectors.grid_store import Region, upsert
from marden.collectors.rates_common import ParseError
from marden.core import db, documents, sources
from marden.core.facts import FactWriter
from marden.core.http import Fetcher

DOC = Doc("https://www.sec.gov/Archives/edgar/data/1/000000000126000001/test-10k.htm", "Test Co. Form 10-K for 2025",
          "10-K", date(2026, 2, 27))
OTHER = Doc("https://www.sec.gov/Archives/edgar/data/2/000000000226000001/other-10q.htm", "Other Co. Form 10-Q",
            "10-Q", date(2026, 8, 6))
HTML = (b"<html><body><p>Our <b>Prairie</b> campus is located in Texas, within the Southwest Power Pool (SPP).</p>"
        b"<p>The <span>Mesa Site</span> has conditional ERCOT interconnection approval of up to 100&#160;MW.</p>"
        b"<p>Grid: SPP &#8226; Utility: Southwestern Public Service Company service territory</p>"
        b"<p>The Mesa Site is in SPP.</p></body></html>")
SPP_QUOTE = "Our Prairie campus is located in Texas, within the Southwest Power Pool (SPP)."
ERCOT_QUOTE = "The Mesa Site has conditional ERCOT interconnection approval of up to 100 MW."


def _setup(conn) -> None:
    sources.upsert_spec(ge.SOURCE)
    grid_registry.ensure_registry(conn)
    for code, name, parent in (("ERCO", "ERCOT", grid_registry.ERCOT), ("SWPP", "SPP", grid_registry.EASTERN),
                               ("SWPW", "SPP RTO West", grid_registry.WESTERN),
                               ("MISO", "MISO", grid_registry.EASTERN)):
        upsert(conn, Region(f"ba:{code}", "balancing_authority", name, "US", parent, {"eia930": {"code": code}}))
    conn.execute("""INSERT INTO site (slug, name) VALUES ('prairie', 'Prairie'), ('mesa', 'Mesa');
                    INSERT INTO campus (id, name) VALUES ('prairie-campus', 'Prairie campus')""")
    for subject in (("site", "prairie"), ("campus", "prairie-campus")):
        _join(conn, subject, "iso", ["ERCOT", "SPP"])
        _join(conn, subject, "balancing_authority", ["ERCO", "SWPP"])
        _join(conn, subject, "interconnection", ["Eastern"])
    _join(conn, ("site", "prairie"), "utility_territories", [["SOUTHWESTERN PUBLIC SERVICE CO", "SOUTH PLAINS"]],
          published=True)
    # Mesa: placed by a county polygon drawn inside SPP, while the NERC union says ERCOT
    _join(conn, ("site", "mesa"), "iso", ["SPP"])
    _join(conn, ("site", "mesa"), "balancing_authority", ["SWPP"])
    _join(conn, ("site", "mesa"), "interconnection", ["ERCOT"])
    _join(conn, ("site", "mesa"), "utility_territories", [["ONCOR ELECTRIC DELIVERY COMPANY LLC"]], published=True)


def _join(conn, subject: tuple[str, str], field: str, values: list, *, published: bool = False) -> list[int]:
    """Facts as grid_join writes them: candidates with one review item for the subject and field."""
    w = FactWriter(conn)
    ids = [w.add(*subject, field, v, tag="F", source_name="HIFLD", source_url=f"https://hifld.test/{field}.gpkg",
                 valid_from=date(2025, 4, 8), producer=grid_join.PRODUCER, quote=str(v), quote_loc="spatial join",
                 status="published" if published else "candidate").id for v in values]
    if not published:
        w.request_review(ids[0], title=f"{subject[0]} {subject[1]}: which {field}?", reason="2 boundaries contain it",
                         payload={"field": field, "candidates": [{"fact_id": i} for i in ids]})
    return ids


def _store(doc: Doc, body: bytes = HTML) -> documents.DocumentRow:
    return documents.record_document(ge.SOURCE_ID, doc.url, body, content_type="text/html", doc_type=doc.doc_type)


def _facts(committed, stype: str, sid: str, field: str, producer: str) -> list[dict]:
    return committed.execute(
        "SELECT * FROM fact WHERE subject_type = %s AND subject_id = %s AND field = %s AND producer = %s ORDER BY id",
        (stype, sid, field, producer)).fetchall()


def _review(committed, sid: str, field: str) -> dict:
    return committed.execute(
        """SELECT r.* FROM review_item r JOIN fact f ON f.id = r.ref_id::bigint
            WHERE f.subject_id = %s AND f.field = %s""", (sid, field)).fetchone()


EVIDENCE_OK = (
    Statement(DOC, SPP_QUOTE, (("iso", "SPP"),), (("site", "prairie"), ("campus", "prairie-campus")), "Item 1"),
    Statement(DOC, "Grid: SPP • Utility: Southwestern Public Service Company service territory",
              (("utility", "SOUTHWESTERN PUBLIC SERVICE CO"),), (("site", "prairie"),), "Item 1"),
    Statement(DOC, ERCOT_QUOTE, (("iso", "ERCOT"),), (("site", "mesa"),), "Item 1"),
)


def test_spp_statement_settles_iso_and_takes_the_ba_off_the_map(committed):
    with db.transaction() as conn:
        _setup(conn)
        doc = _store(DOC)
    out = ge.run(evidence=EVIDENCE_OK)
    assert out["ok"], out

    iso = _facts(committed, "site", "prairie", "iso", ge.PRODUCER)
    assert [(r["value"], r["tag"], r["status"]) for r in iso] == [("SPP", "F", "published")]
    assert iso[0]["quote"] == SPP_QUOTE and iso[0]["document_id"] == doc.id and iso[0]["source_url"] == DOC.url
    assert iso[0]["valid_from"] == date(2026, 2, 27) and iso[0]["source_name"] == "SEC EDGAR"
    ba = _facts(committed, "site", "prairie", "balancing_authority", ge.PRODUCER)[0]
    assert (ba["value"], ba["tag"]) == ("SWPP", "R")  # SPP has two BAs: the map decides between them
    assert "only SWPP's HIFLD Control Areas boundary contains the subject" in ba["quote_loc"]
    inter = _facts(committed, "site", "prairie", "interconnection", ge.PRODUCER)[0]
    assert (inter["value"], inter["tag"]) == ("Eastern", "R")
    util = _facts(committed, "site", "prairie", "utility", ge.PRODUCER)[0]
    assert (util["value"], util["tag"]) == ("SOUTHWESTERN PUBLIC SERVICE CO", "F")

    joined = {(r["value"], r["status"]) for r in _facts(committed, "site", "prairie", "iso", grid_join.PRODUCER)}
    assert joined == {("ERCOT", "rejected"), ("SPP", "published")}
    joined = {(r["value"], r["status"]) for r in _facts(committed, "campus", "prairie-campus", "balancing_authority",
                                                         grid_join.PRODUCER)}
    assert joined == {("ERCO", "rejected"), ("SWPP", "published")}
    for field, status in (("iso", "rejected"), ("balancing_authority", "rejected"), ("interconnection", "approved")):
        review = _review(committed, "prairie", field)
        assert (review["status"], review["decided_by"]) == (status, ge.PRODUCER), field
        resolution = review["payload"]["resolution"]
        assert resolution["document_id"] == doc.id and resolution["quote"] and resolution["fact_id"]
    reason = committed.execute("SELECT meta FROM audit_log WHERE action = 'fact.reject' ORDER BY id LIMIT 1").fetchone()
    assert DOC.url in reason["meta"]["reason"]
    current = committed.execute("SELECT value, producer FROM fact_current WHERE subject_id = 'prairie' "
                                "AND field = 'iso'").fetchone()
    assert (current["value"], current["producer"]) == ("SPP", ge.PRODUCER)


def test_a_primary_statement_overrules_a_generalized_boundary(committed):
    with db.transaction() as conn:
        _setup(conn)
        _store(DOC)
    assert ge.run(evidence=EVIDENCE_OK)["ok"]
    mine = {f: _facts(committed, "site", "mesa", f, ge.PRODUCER)[0]
            for f in ("iso", "balancing_authority", "interconnection")}
    assert {f: (r["value"], r["tag"]) for f, r in mine.items()} == {
        "iso": ("ERCOT", "F"), "balancing_authority": ("ERCO", "F"), "interconnection": ("ERCOT", "F")}
    assert "ERCOT's only balancing authority per EIA-930 is ERCO" in mine["balancing_authority"]["quote_loc"]
    assert [r["status"] for r in _facts(committed, "site", "mesa", "iso", grid_join.PRODUCER)] == ["rejected"]
    assert [r["status"] for r in _facts(committed, "site", "mesa", "interconnection", grid_join.PRODUCER)] == [
        "published"]
    assert committed.execute("SELECT count(*) AS n FROM review_item WHERE status = 'open'").fetchone()["n"] == 0


def test_rerun_writes_nothing_new(committed):
    with db.transaction() as conn:
        _setup(conn)
        _store(DOC)
    first = ge.run(evidence=EVIDENCE_OK)
    again = ge.run(evidence=EVIDENCE_OK)
    assert first["facts_new"] == 10 and again["facts_new"] == 0, (first, again)
    assert (first["documents_seen"], again["documents_new"]) == (1, 0)
    assert again["decided"] == {"published": 0, "rejected": 0}
    assert committed.execute("SELECT count(*) AS n FROM document").fetchone()["n"] == 1


def test_a_utility_outside_the_hifld_territories_is_not_written(committed):
    with db.transaction() as conn:
        _setup(conn)
        _store(DOC)
    evidence = (Statement(DOC, ERCOT_QUOTE, (("utility", "LYNTEGAR ELECTRIC COOP, INC"),), (("site", "mesa"),), "x"),)
    out = ge.run(evidence=evidence)
    assert out["ok"] and _facts(committed, "site", "mesa", "utility", ge.PRODUCER) == []
    assert out["results"] == [{"subject": "site:mesa", "field": "utility", "unresolved": (
        "LYNTEGAR ELECTRIC COOP, INC is not among the subject's HIFLD retail territories "
        "(['ONCOR ELECTRIC DELIVERY COMPANY LLC'])")}]


def test_disagreeing_documents_leave_the_review_open_with_the_evidence(committed):
    with db.transaction() as conn:
        _setup(conn)
        _store(DOC)
        _store(OTHER)
    evidence = (Statement(DOC, ERCOT_QUOTE, (("iso", "ERCOT"),), (("site", "mesa"),), "Item 1"),
                Statement(OTHER, "The Mesa Site is in SPP.", (("iso", "SPP"),), (("site", "mesa"),), "Item 2"))
    out = ge.run(evidence=evidence)
    assert out["ok"] and _facts(committed, "site", "mesa", "iso", ge.PRODUCER) == []
    review = _review(committed, "mesa", "iso")
    assert review["status"] == "open"
    assert [e["source_url"] for e in review["payload"]["evidence"]] == [DOC.url, OTHER.url]
    assert review["payload"]["evidence_note"] == "primary documents disagree: ERCOT, SPP"
    assert [r["status"] for r in _facts(committed, "site", "mesa", "iso", grid_join.PRODUCER)] == ["candidate"]


def test_a_quote_missing_from_the_document_stops_the_run(committed):
    with db.transaction() as conn:
        _setup(conn)
        _store(DOC, b"<html><p>Nothing about the grid.</p></html>")
    out = ge.run(evidence=EVIDENCE_OK)
    assert not out["ok"] and "does not contain the quote" in out["error"]
    assert committed.execute("SELECT count(*) AS n FROM fact WHERE producer = %s", (ge.PRODUCER,)).fetchone()["n"] == 0
    with db.transaction() as conn, pytest.raises(ParseError):
        ge.resolve(conn, evidence=EVIDENCE_OK)


def test_missing_documents_are_fetched_once_and_stored_raw(committed):
    with db.transaction() as conn:
        _setup(conn)
    seen: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})

    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(serve)))
    assert ge.run(evidence=EVIDENCE_OK, fetcher=fetcher)["ok"]
    assert ge.run(evidence=EVIDENCE_OK, fetcher=fetcher)["ok"]
    assert len(seen) == 1 and "@" in seen[0]  # the declared SEC User-Agent, one request in two runs
    row = documents.latest_document(ge.SOURCE_ID, DOC.url)
    assert row is not None and row.read() == HTML


def test_document_text_reads_html_entities_and_split_tags():
    text = ge.document_text(HTML, "text/html")
    assert ERCOT_QUOTE in text and ge.has_quote(text, "The Mesa\n   Site has conditional")
    assert not ge.has_quote(text, "") and not ge.has_quote(text, "Mesa Site is in ERCOT")


def test_listed_evidence_is_well_formed():
    fields = {"iso", "balancing_authority", "interconnection", "utility"}
    for s in EVIDENCE:
        assert s.doc.url.startswith("https://www.sec.gov/Archives/edgar/data/") and s.doc.doc_date < date(2026, 10, 22)
        assert s.quote == ge.squash(s.quote) and s.loc and s.subjects
        assert {f for f, _ in s.states} <= fields
        assert {t for t, _ in s.subjects} <= {"site", "campus", "building"}
    sites = {sid for s in EVIDENCE for t, sid in s.subjects if t == "site"}
    assert sites == {"abernathy", "childress", "helios-1", "helios-2", "polaris-forge-1", "polaris-forge-1-building-4",
                     "stingray"}


@pytest.mark.network
def test_listed_quotes_are_in_the_live_filings():
    with Fetcher() as fetcher:
        for doc, quotes in ge.quotes_by_doc(EVIDENCE).items():
            resp = fetcher.get(doc.url)
            text = ge.document_text(resp.content, resp.content_type)
            assert all(ge.has_quote(text, q) for q in quotes), doc.url
            if doc.doc_type in {"10-K", "10-Q", "20-F"}:  # the title names the registrant on the cover page
                registrant = doc.title.split(" Form ")[0].split(" (now ")[0]
                assert ge.has_quote(text.lower(), f"{registrant} (exact name of registrant".lower()), doc.title
