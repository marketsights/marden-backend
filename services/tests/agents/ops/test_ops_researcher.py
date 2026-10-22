"""Researcher: gaps from what Marden shows, queries, raw-first searches, relevance checks and the intake hand-off."""

from __future__ import annotations

import json
from datetime import date

import pytest
from ops_rows import FakeFercFetcher, FakeSec, ferc_answer, page, recorded, seed_sites

from marden.agents.config import BackendStatus, set_backend_status
from marden.agents.ops import gaps, researcher
from marden.agents.ops.research_plan import plan, relevance, tokens
from marden.agents.ops.search_edgar import EdgarSearch, parse
from marden.agents.ops.search_ferc import FercSearch
from marden.collectors.ferc_api import ElibraryClient
from marden.collectors.ferc_parse import parse_search
from marden.core import db, documents
from marden.core.http import TokenBucket
from marden.core.sources import upsert_spec

TODAY = date(2026, 10, 22)
GALAXY = recorded("edgar_fts_galaxy_helios_commencement.json")


@pytest.fixture(autouse=True)
def _agents_off():
    set_backend_status(BackendStatus("not_configured", "test: no backend"))
    yield
    set_backend_status(None)


def ferc_search(files: dict[str, bytes]) -> FercSearch:
    client = ElibraryClient("ferc_fts", fetcher=FakeFercFetcher(ferc_answer(), files),  # type: ignore[arg-type]
                            bucket=TokenBucket(1000.0), sleep=lambda s: None)
    return FercSearch(client)


def main_files() -> list[tuple[str, str]]:
    """(file id, public url) of each recorded eLibrary hit's main file, in the researcher's order."""
    from marden.agents.ops.search_ferc import rank

    _, filings = parse_search(ferc_answer())
    out = []
    for f in rank(filings):
        main, _ = f.main_file()
        assert main is not None
        out.append((main.file_id, main.url))
    return out


def test_name_phrases_and_entity_names():
    assert gaps.name_phrases("Project Jupiter (Stargate New Mexico)") == ["Project Jupiter", "Stargate New Mexico"]
    assert gaps.name_phrases("Childress (Microsoft)") == ["Childress"]
    assert gaps.name_phrases("Polaris Forge 1, Buildings 2 and 3") == ["Polaris Forge 1"]
    assert gaps.name_phrases("CoreWeave DDTL 3.0 (OpenAI servers)") == ["CoreWeave DDTL 3.0"]
    assert gaps.name_phrases('Saline "The Barn" (Stargate Michigan)') == ["Saline", "Stargate Michigan", "Barn"]
    names = ["CoreWeave Compute Acquisition Co. VII, LLC (co-borrower CCAC V)",
             "Iskandar Enterprise LLC and Kaveh Enterprise LLC", "Cloud Capital HoldCo LP issuers"]
    assert gaps.entity_phrases(names) == ["CoreWeave Compute Acquisition Co. VII LLC", "Cloud Capital HoldCo LP"]
    assert tokens("Co. VII, LLC") == tokens("Co. VII LLC")
    assert tokens("Doña Ana") == tokens("dona ana")


def test_gaps_and_plan_from_the_read_models():
    seed_sites()
    with db.transaction() as conn:
        found = gaps.collect(conn)
    items = {(g.subject.id, g.item, g.origin) for g in found}
    assert ("helios-1", "Lease: Commencement", "project_file") in items
    assert ("helios-1", "Permits: Zoning", "project_file") in items
    assert not any(g.item in ("Lease: Rent", "Insurance: Builder's risk") for g in found)  # sourced / n/p
    assert found[0].subject.watched  # watched first
    p = plan(found)
    queries = [q.query for q in p.edgar]
    assert queries[:2] == ['"Galaxy Helios I LLC" "commencement date"', '"Helios Phase I" "commencement date"']
    assert [q.query for q in p.ferc] == ["Project Jupiter", "Stargate New Mexico"]
    assert {n["item"] for n in p.not_searched} == {"Permits: Zoning"}
    jupiter = next(g for g in found if g.subject.id == "jupiter").subject
    assert {"Oracle", "OpenAI", "Dona Ana County", "Red Chiles A\u2013D LLC"} <= set(jupiter.context)


def test_edgar_answer_parses_and_filters_forms():
    total, hits = parse(GALAXY)
    assert total == 13 and len(hits) == 13
    first = hits[0]
    assert (first.form, first.file_type, first.cik) == ("8-K", "8-K", "0001859392")
    assert first.url.startswith("https://www.sec.gov/Archives/edgar/data/1859392/")
    assert first.url.endswith("/glxy-20260728.htm")
    assert all(h.wanted for h in hits)


def test_relevance_tells_namesakes_apart():
    seed_sites()
    with db.transaction() as conn:
        p = plan(gaps.collect(conn))
    q = p.ferc[0]  # "Project Jupiter"
    ok, why = relevance(tokens("Comments on Project Jupiter in Doña Ana County " * 20), q, None)
    assert ok and "Dona Ana County" in why
    ok, why = relevance(tokens("Enlight's Project Jupiter solar plant in Israel " * 20), q, None)
    assert not ok and "another project" in why
    ok, _ = relevance(tokens("Project Jupiter " * 50), q, "0001341439")  # filed by the tenant (Oracle)
    assert ok
    ok, why = relevance(None, q, None)
    assert ok and "scanned" in why


def test_run_stores_raw_checks_and_queues():
    seed_sites()
    _, hits = parse(GALAXY)
    held_url = hits[2].url
    upsert_spec(researcher.EDGAR_SOURCE)
    from marden.agents.intake import ensure_intake_sources

    ensure_intake_sources()
    held = documents.record_document("upload", held_url, page("Galaxy Helios I LLC indenture."),
                                     content_type="text/html", extract_state="none")
    files = {hits[0].url: page("Galaxy Helios I LLC: the commencement date of the lease."),
             hits[1].url: page("Galaxy Digital reports its second quarter results.")}
    (j1, j1_url), (j2, _), (j3, j3_url) = main_files()
    ferc_files = {j1: page("STACK supports the lateral to Project Jupiter in Doña Ana County."),
                  j2: page("Project Jupiter is a solar project in another state."),
                  j3: page("Comments on the Project Jupiter data center that Oracle will lease.")}
    sec = FakeSec(GALAXY, files)
    ferc = ferc_search(ferc_files)
    report = researcher.run(TODAY, edgar=EdgarSearch(sec), ferc=ferc,  # type: ignore[arg-type]
                            max_queries=1, max_ferc=1, collectors=False)
    assert report["edgar"] == {"planned": 2, "run": 1, "deferred": 1, "blocked": None}
    assert report["ferc"]["run"] == 1
    assert report["finds"] == {"queued": 4, "skipped": 2}
    assert sec.calls[0][1]["q"] == '"Galaxy Helios I LLC" "commencement date"'
    assert "forms" in sec.calls[0][1] and sec.calls[0][1]["startdt"] == "2025-10-22"
    body = ferc.client.fetcher.client.bodies[0]
    assert body["searchText"] == '"Project Jupiter"' and body["docketSearches"] == []
    queued = {q["url"]: q for q in report["queued"]}
    assert set(queued) == {hits[0].url, held_url, j1_url, j3_url}
    assert queued[held_url]["fetched"] is False and queued[held_url]["document_id"] == held.id
    assert report["agents"]["backend"] == "not_configured" and "wait in the intake" in report["agents"]["note"]
    with db.transaction() as conn:
        searches = conn.execute("SELECT * FROM research_search ORDER BY id").fetchall()
        finds = {r["url"]: r for r in conn.execute("SELECT * FROM research_find")}
        docs = {r["url"]: r for r in conn.execute("SELECT url, extract_state, meta, source_id FROM document")}
        run_row = conn.execute("SELECT * FROM agent_ops_run").fetchone()
        src = conn.execute("SELECT last_ok_at FROM source WHERE id = 'edgar_fts'").fetchone()
        jobs = conn.execute("SELECT count(*) AS n FROM agent_job").fetchone()["n"]
    assert [(s["endpoint"], s["status"], s["hits"]) for s in searches] == [("edgar_fts", "ok", 13),
                                                                           ("ferc_elibrary", "ok", 12)]
    assert searches[0]["window_from"] == date(2025, 10, 22) and searches[0]["document_id"] is not None
    efts = next(r for u, r in docs.items() if u.startswith("https://efts.sec.gov/"))
    assert efts["source_id"] == "edgar_fts"
    assert docs[hits[0].url]["extract_state"] == "queued"
    assert docs[hits[0].url]["meta"]["research"]["items"] == ["Lease: Commencement"]
    assert docs[hits[0].url]["meta"]["cik"] == "1859392"
    assert docs[hits[1].url]["extract_state"] == "none"  # stored raw, not about the subject
    assert "does not name" in finds[hits[1].url]["reason"]
    assert docs[held_url]["extract_state"] == "queued" and docs[held_url]["meta"]["research"]["run_id"]
    assert docs[j1_url]["extract_state"] == "queued" and docs[j1_url]["source_id"] == "ferc_fts"
    assert "another project" in next(f["reason"] for u, f in finds.items() if f["outcome"] == "skipped"
                                     and "elibrary" in u)
    assert run_row["status"] == "ok" and run_row["model_status"] == "not_used"
    assert src["last_ok_at"] is not None and jobs == 0  # not configured: no agent jobs, the intake waits


def test_second_run_is_idempotent_and_windows_overlap():
    seed_sites(watch_helios=True)
    _, hits = parse(GALAXY)
    files = {h.url: page("Galaxy Helios I LLC commencement date.") for h in hits}
    for _ in range(2):
        researcher.run(TODAY, edgar=EdgarSearch(FakeSec(GALAXY, files)), ferc=ferc_search({}),  # type: ignore[arg-type]
                       max_queries=1, max_ferc=0, collectors=False)
    with db.transaction() as conn:
        searches = conn.execute("SELECT query, window_from FROM research_search WHERE endpoint = 'edgar_fts' "
                                "ORDER BY id").fetchall()
        finds = conn.execute("SELECT outcome, count(*) AS n FROM research_find GROUP BY 1 ORDER BY 1").fetchall()
        n_docs = conn.execute("SELECT count(*) AS n FROM document WHERE source_id = 'edgar_fts' AND doc_type <> "
                              "'edgar_fts'").fetchone()["n"]
    # the second run takes the other query first (never searched), so both queries ran once each
    assert len({s["query"] for s in searches}) == 2
    assert n_docs == 3  # the same three files, stored once
    assert {f["outcome"]: f["n"] for f in finds} == {"queued": 3, "already_stored": 3}


def test_refusal_stops_edgar_and_is_recorded():
    seed_sites()
    report = researcher.run(TODAY, edgar=EdgarSearch(FakeSec(GALAXY, {}, status=403)),  # type: ignore[arg-type]
                            ferc=ferc_search({}), max_queries=5, max_ferc=0, collectors=False)
    assert report["edgar"]["blocked"] and report["edgar"]["run"] == 1 and report["edgar"]["deferred"] == 1
    with db.transaction() as conn:
        s = conn.execute("SELECT status FROM research_search").fetchone()
        src = conn.execute("SELECT last_error FROM source WHERE id = 'edgar_fts'").fetchone()
        run_row = conn.execute("SELECT status FROM agent_ops_run").fetchone()
    assert s["status"] == "blocked" and "403" in src["last_error"] and run_row["status"] == "partial"


def test_configured_agents_get_jobs_at_research_priority():
    seed_sites()
    set_backend_status(BackendStatus("api", "test"))
    _, hits = parse(GALAXY)
    files = {hits[0].url: page("Galaxy Helios I LLC: commencement date.")}
    report = researcher.run(TODAY, edgar=EdgarSearch(FakeSec(GALAXY, files)), ferc=ferc_search({}),  # type: ignore[arg-type]
                            max_queries=1, max_ferc=0, max_docs=1, collectors=False)
    assert report["agents"]["jobs"] == 1
    assert report["finds"] == {"queued": 1, "skipped": 2}  # the document budget stops the other two
    with db.transaction() as conn:
        job = conn.execute("SELECT priority, priority_reason, links FROM agent_job").fetchone()
    assert job["priority"] == 90 and job["priority_reason"].startswith("researcher: Lease: Commencement")
    assert job["links"] == {}  # a research find is never a person's link


def test_collector_items_are_checked_not_searched():
    from marden.agents.ops.research_sources import check_collectors
    from marden.collectors.air_permits_tx import SOURCE_CR

    seed_sites()
    with db.transaction() as conn:
        conn.execute("""INSERT INTO read_model (view, key, as_of, doc) VALUES ('project_file', 'helios-1',
                        '2026-10-22', '{"checklist": [{"area": "Permits", "item": "Air permits", "state": "n/d"}]}')""")
        found = [g for g in gaps.collect(conn) if g.plan.collector]
    upsert_spec(SOURCE_CR)
    out = check_collectors(found, run=False)
    assert out[0]["state"] == "TX" and out[0]["source"] == "tceq_cr_open_data"
    assert "missed its cadence" in out[0]["action"]  # never ran here, and run=False


def test_a_seed_record_naming_the_url_is_not_the_file():
    """The seed archive stores a JSON row per exhibit under the exhibit's URL; the researcher fetches the file."""
    seed_sites()
    _, hits = parse(GALAXY)
    with db.transaction() as conn:
        conn.execute("""INSERT INTO source (id, name, kind, terms_note) VALUES ('seed_marden_data', 'Seed', 'seed', 't')
                        ON CONFLICT (id) DO NOTHING""")
    stub = documents.record_document("seed_marden_data", hits[0].url, b'{"exhibit": "EX-99.1"}',
                                     content_type="application/json")
    files = {hits[0].url: page("Galaxy Helios I LLC: the commencement date of the lease.")}
    report = researcher.run(TODAY, edgar=EdgarSearch(FakeSec(GALAXY, files)), ferc=ferc_search({}),  # type: ignore[arg-type]
                            max_queries=1, max_ferc=0, max_docs=1, collectors=False)
    first = report["queued"][0]
    assert first["url"] == hits[0].url and first["fetched"] and first["document_id"] != stub.id


def test_large_files_and_report_forms_are_stored_not_queued(monkeypatch):
    seed_sites(watch_helios=False)
    monkeypatch.setattr(researcher, "MAX_QUEUE_BYTES", 1000)
    (j1, j1_url), (j2, j2_url), (j3, _) = main_files()
    answer = json.loads(ferc_answer())
    for hit in answer["searchHits"]:
        if any(t["fileId"] == j2 for t in hit["transmittals"]):
            hit["classTypes"] = [{"documentClass": "Report/Form",
                                  "documentType": "Form 1 - Annual Rpt. for Major Electric Utilities"}]
    files = {j: page("STACK supports the lateral to Project Jupiter in Doña Ana County.") for j in (j1, j3)}
    client = ElibraryClient("ferc_fts", fetcher=FakeFercFetcher(json.dumps(answer).encode(), files),  # type: ignore[arg-type]
                            bucket=TokenBucket(1000.0), sleep=lambda s: None)
    report = researcher.run(TODAY, edgar=EdgarSearch(FakeSec(GALAXY, {})), ferc=FercSearch(client),  # type: ignore[arg-type]
                            max_queries=0, max_ferc=1, collectors=False)
    with db.transaction() as conn:
        finds = {r["url"]: r for r in conn.execute("SELECT * FROM research_find")}
        state = conn.execute("SELECT extract_state FROM document WHERE url = %s", (j1_url,)).fetchone()
    assert report["queued"] == [] and report["finds"] == {"skipped": 3} and state["extract_state"] == "none"
    assert "more than the researcher queues" in finds[j1_url]["reason"]
    assert "periodic report form" in finds[j2_url]["reason"] and finds[j2_url]["document_id"] is None


def test_a_refused_file_stops_edgar_before_the_next_fetch():
    """sec.gov answering a filing with 403 (its 'undeclared automated tool' page) or 429 stops every EDGAR search
    and fetch of the run: never a second request after a refusal."""
    seed_sites()
    _, hits = parse(GALAXY)
    sec = FakeSec(GALAXY, {hits[0].url: 403, hits[1].url: page("Galaxy Helios I LLC commencement date.")})
    report = researcher.run(TODAY, edgar=EdgarSearch(sec), ferc=ferc_search({}),  # type: ignore[arg-type]
                            max_queries=5, max_ferc=0, collectors=False)
    assert report["edgar"]["blocked"] and "403" in report["edgar"]["blocked"]
    assert report["edgar"]["run"] == 1 and report["edgar"]["deferred"] == 1
    assert [u for u, _ in sec.calls[1:]] == [hits[0].url]  # the search, the refused file, nothing after
    with db.transaction() as conn:
        finds = conn.execute("SELECT url, outcome, reason FROM research_find").fetchall()
        src = conn.execute("SELECT last_error FROM source WHERE id = 'edgar_fts'").fetchone()
        run_row = conn.execute("SELECT status FROM agent_ops_run").fetchone()
    assert [(f["url"], f["outcome"]) for f in finds] == [(hits[0].url, "failed")]
    assert "refused" in finds[0]["reason"] and "403" in src["last_error"] and run_row["status"] == "partial"


def test_a_bot_check_on_an_elibrary_file_stops_the_ferc_searches():
    seed_sites()
    (j1, _), (j2, _), (j3, _) = main_files()
    files: dict[str, bytes | int] = {j1: 403, j2: page("Project Jupiter in Doña Ana County."), j3: page("x")}
    ferc = ferc_search(files)
    report = researcher.run(TODAY, edgar=EdgarSearch(FakeSec(GALAXY, {})), ferc=ferc,  # type: ignore[arg-type]
                            max_queries=0, max_ferc=1, collectors=False)
    assert report["ferc"]["blocked"] and report["finds"] == {"failed": 1}
    downloads = [b for b in ferc.client.fetcher.client.bodies if "fileidLst" in b]
    assert [b["fileidLst"] for b in downloads] == [[j1]]  # nothing fetched after the refusal
    with db.transaction() as conn:
        run_row = conn.execute("SELECT status FROM agent_ops_run").fetchone()
    assert run_row["status"] == "partial"


def test_a_held_file_too_large_is_not_queued(monkeypatch):
    """A stored file a collector never queued gets the same size rule as a new one."""
    seed_sites()
    monkeypatch.setattr(researcher, "MAX_QUEUE_BYTES", 100)
    _, hits = parse(GALAXY)
    from marden.agents.intake import ensure_intake_sources

    ensure_intake_sources()
    held = documents.record_document("upload", hits[0].url, page("Galaxy Helios I LLC commencement date."),
                                     content_type="text/html", extract_state="none")
    report = researcher.run(TODAY, edgar=EdgarSearch(FakeSec(GALAXY, {})), ferc=ferc_search({}),  # type: ignore[arg-type]
                            max_queries=1, max_ferc=0, max_docs=0, collectors=False)
    with db.transaction() as conn:
        find = conn.execute("SELECT outcome, reason FROM research_find WHERE url = %s", (hits[0].url,)).fetchone()
        state = conn.execute("SELECT extract_state FROM document WHERE id = %s", (held.id,)).fetchone()
    assert report["queued"] == [] and find["outcome"] == "skipped" and "more than the researcher" in find["reason"]
    assert state["extract_state"] == "none"
