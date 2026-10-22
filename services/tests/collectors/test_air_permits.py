"""air_permits runs against captured records (data/air_permits, see test_air_permits_parse): Texas (Central Registry
rows -> match -> Records Online pages and files), New Mexico (notice post -> legal notice) and Iowa (plant page ->
permit collection). Checks the facts and events written, raw-first storage, that a second run writes nothing, the
stop rules (403, challenge page: the run is recorded as failed and writes nothing), that the match never feeds on
this collector's own facts, the plant card mapping, the systems registry, the schedule and the CLI."""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from marden import cli
from marden.collectors import air_permits, air_permits_ia, air_permits_la, air_permits_nm, air_permits_tx
from marden.collectors.air_permits_match import load_profiles
from marden.collectors.air_permits_systems import SYSTEMS
from marden.core import http
from marden.core.facts import FactWriter
from marden.core.http import Fetcher
from marden.core.queue import app
from marden.readmodels.views import facilities_plants

DATA = Path(__file__).parent / "data" / "air_permits"
CR_ROWS = (DATA / "cr_rows_20261021.json").read_bytes()
RN_PAGE = gzip.decompress((DATA / "ro_rn112029061_trimmed.html.gz").read_bytes())
PERMIT_PAGE = gzip.decompress((DATA / "ro_permit_177263.html.gz").read_bytes())
EMPTY_PAGE = re.sub(rb"<tr><td scope=\"row\" class=\"xuiListContentCell.*?</tr>", b"", PERMIT_PAGE, flags=re.S)
NOT_PUBLIC = (DATA / "ro_not_public_10105437.html").read_bytes()
FILES = {"8600163": gzip.decompress((DATA / "tr_177263_387309.pdf.gz").read_bytes()),
         "8600167": gzip.decompress((DATA / "fa_177263_387309.pdf.gz").read_bytes())}
NM_POST = (DATA / "nmed_post_7565.json").read_bytes()
NM_NOTICE = gzip.decompress((DATA / "nmed_legal_notice_10883.pdf.gz").read_bytes())
IA_PAGE = (DATA / "aqrr_plant_11491.html").read_bytes()
IA_PDF = gzip.decompress((DATA / "aqrr_25-350_pages1-3.pdf.gz").read_bytes())
LA_PAGE = gzip.decompress((DATA / "ldeq_notices_entergy_louisiana.html.gz").read_bytes())
LA_EMPTY = b'<table><tbody id="pn-table-body"></tbody></table>'
HOSTS = ("data.texas.gov", "records.tceq.texas.gov", "www.env.nm.gov", "service.web.env.nm.gov",
         "idnraqrr.iowadnr.gov", "programs.iowadnr.gov", "www.deq.louisiana.gov")
CR_URL = "https://data.texas.gov/resource/5eqq-7nad.json?ref_num_txt=RN112029061&additional_id_text="
FA_URL = "https://records.tceq.texas.gov/cs/idcplg?IdcService=TCEQ_EXTERNAL_SEARCH_GET_FILE&dID=8600167&Rendition=Web&searchType=External"


@pytest.fixture
def fast_rate():
    for host in HOSTS:
        http.set_rate_limit(host, 1000.0)
    yield
    http.set_rate_limit("data.texas.gov", 1.0)
    for host in HOSTS[1:]:
        http.set_rate_limit(host, 0.5)


def handler(seen: list[str], *, refuse_files: bool = False, challenge_cr: bool = False, reorder_cr: bool = False):
    def respond(request: httpx.Request) -> httpx.Response:
        url, q = str(request.url), parse_qs(urlparse(str(request.url)).query)
        seen.append(url)
        assert request.headers["user-agent"] == "Marden research cody@marden.ai"
        host = request.url.host
        if host == "data.texas.gov":
            if challenge_cr:
                return httpx.Response(200, html="<title>Just a moment...</title>")
            rows = json.dumps(json.loads(CR_ROWS)[::-1]).encode() if reorder_cr else CR_ROWS  # same rows, new order
            return httpx.Response(200, content=rows if "5eqq-7nad" in url else b"[]",
                                  headers={"content-type": "application/json"})
        if host == "records.tceq.texas.gov":
            service = q["IdcService"][0]
            if service == "TCEQ_SEARCH":
                return httpx.Response(200, html="<title>TCEQ Records Online</title>")
            if service == "TCEQ_SEARCH_ADD_ACCESS":
                return httpx.Response(200, json={"LocalData": {"accessID": "1"}})
            if service == "TCEQ_PERFORM_SEARCH":
                query = q["QueryText"][0]
                page = RN_PAGE if "xRefNumTxt" in query else PERMIT_PAGE if "`177263`" in query else EMPTY_PAGE
                return httpx.Response(200, content=page, headers={"content-type": "text/html"})
            if refuse_files:
                return httpx.Response(403, text="Forbidden")
            did = q["dID"][0]
            if did in FILES:
                return httpx.Response(200, content=FILES[did], headers={"content-type": "application/pdf"})
            return httpx.Response(200, content=NOT_PUBLIC, headers={"content-type": "text/html"})
        if host == "www.env.nm.gov":
            if "/public-notices/wp-json/wp/v2/search" in url and re.search(r"ygi|jupiter", url, re.I):
                return httpx.Response(200, json=[{"id": 7565, "title": "Yucca Growth Infrastructure, LLC &#8211; YGI "
                                                  "Microgrid &#8212; (Project Jupiter)", "subtype": "post"}])
            if url.endswith("/public-notices/wp-json/wp/v2/posts/7565"):
                return httpx.Response(200, content=NM_POST, headers={"content-type": "application/json"})
            return httpx.Response(200, json=[])
        if host == "service.web.env.nm.gov":
            return httpx.Response(200, content=NM_NOTICE, headers={"content-type": "application/pdf"})
        if host == "idnraqrr.iowadnr.gov":
            if "AutoComplete" in url:
                return httpx.Response(200, json=["Orient Energy Center"] if q.get("prefix") == ["orient"] else [])
            if "ByConstructionPermitPlantNameView" in url:
                return httpx.Response(200, html='<a class="btn selectFacility" data-selectclick="11491">Select</a>')
            if "ConstructionPermit?id=11491" in url:
                return httpx.Response(200, content=IA_PAGE, headers={"content-type": "text/html"})
            if "DownloadFile" in url and "25-350" in url:
                return httpx.Response(200, content=IA_PDF, headers={"content-type": "application/pdf"})
            return httpx.Response(200, html="<html>not a permit file</html>")
        if host == "www.deq.louisiana.gov":
            page = LA_PAGE if q.get("q") == ["entergy louisiana"] else LA_EMPTY
            return httpx.Response(200, content=page, headers={"content-type": "text/html"})
        return httpx.Response(404)

    return respond


def fetcher(seen: list[str], **kw) -> Fetcher:
    return Fetcher(client=httpx.Client(transport=httpx.MockTransport(handler(seen, **kw)), follow_redirects=True),
                   max_retries=0)


def _plant(committed, pid: str, name: str, state: str, county: str, operator: str, **extra: object) -> None:
    committed.execute("INSERT INTO us_county (geoid, statefp, countyfp, stusps, name, namelsad, geom) VALUES "
                      "(%s, '00', %s, %s, %s, %s, ST_GeomFromText('MULTIPOLYGON(((0 0,1 0,1 1,0 0)))', 4326)) "
                      "ON CONFLICT DO NOTHING", (f"00{pid[-3:]}", pid[-3:], state, county, f"{county} County"))
    committed.execute("INSERT INTO power_plant (id, name, state, county) VALUES (%s, %s, %s, %s)",
                      (pid, name, state, county))
    facts = FactWriter(committed)
    for field, value in {"operator": operator, **extra}.items():
        facts.add("plant", pid, field, value, tag="R", source_name="Marden research archive", valid_from="2026-10-17",
                  producer="seed:facilities", status="published")


def _current(committed, pid: str) -> dict[str, object]:
    rows = committed.execute("SELECT field, value FROM fact_current WHERE subject_type = 'plant' AND subject_id = %s",
                             (pid,)).fetchall()
    return {r["field"]: r["value"] for r in rows}


def _counts(committed) -> tuple[int, int, int]:
    one = committed.execute("SELECT (SELECT count(*) FROM fact WHERE producer = 'collector:air_permits') f, "
                            "(SELECT count(*) FROM event WHERE kind = 'permit') e, (SELECT count(*) FROM document) d")
    row = one.fetchone()
    return row["f"], row["e"], row["d"]


def test_texas_run_writes_permits_units_and_history_once(committed, fast_rate):
    _plant(committed, "n-abilene-onsite", "Stargate Abilene on-site gas generation & expansion", "TX", "Taylor",
           "Crusoe / Lancium (Blue Owl & Primary Digital JV owners)")
    seen: list[str] = []
    out = air_permits.run(states=["TX"], fetcher=fetcher(seen))
    assert out["ok"], out
    cov = out["states"]["TX"]["run"]["coverage"]
    assert cov["matched_plants"] == ["n-abilene-onsite"] and cov["plants"] == 1
    facts = _current(committed, "n-abilene-onsite")
    main = facts["air_permit"]
    assert (main["agency"], main["number"], main["type"], main["status"]) == ("TCEQ", "182126", "PSD", "pending")
    assert main["ids"] == ["182126", "GHGPSDTX263", "PSDTX1688", "HAP89"]  # HAP89 has no documents of its own
    assert main["match"]["county"] == {"county": "Taylor", "from": "plant county"}
    std = next(p for p in facts["air_permits"] if p["number"] == "177263")
    assert (std["type"], std["status"], std["expires"]) == ("Standard", "issued", "2034-08-21")
    assert [(u["count"], u["model"], u["rating_mw_each"]) for u in std["units"]] == [(5, "Titan 350", 38.0),
                                                                                       (5, "LM2500", 34.1)]
    assert all(u["url"].endswith("dID=8600163&Rendition=Web&searchType=External") for u in std["units"])
    assert "2025-01-22" in [a["date"] for a in std["amendments"]]
    assert "177263" in facts["air_permit_number"] and "O4721" in facts["air_permit_number"]
    row = committed.execute("SELECT tag, source_name, document_id, quote, method_version FROM fact_current WHERE "
                            "subject_id = 'n-abilene-onsite' AND field = 'air_permit'").fetchone()
    assert (row["tag"], row["source_name"], row["method_version"]) == ("F", "TCEQ", "air-permit-match@0.1")
    assert row["document_id"] and row["quote"].startswith("RN112029061 182126 PENDING")
    ev = committed.execute("SELECT event_date::text d, title FROM event WHERE subject_id = 'n-abilene-onsite' AND "
                           "kind = 'permit' AND title LIKE '%%177263: decision (project 387309)'").fetchone()
    assert ev["d"] == "2025-01-22"
    docs = committed.execute("SELECT doc_type, extract_state FROM document WHERE source_id = 'tceq_records_online' "
                             "AND doc_type LIKE 'tceq_%%'").fetchall()
    assert {"tceq_technical_review", "tceq_final_action", "tceq_not_public_html"} <= {d["doc_type"] for d in docs}
    assert {d["extract_state"] for d in docs if d["doc_type"] != "tceq_not_public_html"} == {"queued"}
    before = _counts(committed)
    again = air_permits.run(states=["TX"], fetcher=fetcher(seen, reorder_cr=True))
    assert again["ok"] and again["totals"]["facts_new"] == 0 and again["totals"]["events_new"] == 0
    assert _counts(committed) == before  # nothing new: facts, events and documents
    profile = next(p for p in load_profiles(committed, ["TX"]) if p.id == "n-abilene-onsite")
    assert profile.numbers == {}  # the match never feeds on this collector's own permit facts
    blocked = committed.execute("SELECT blocked, blocked_reason FROM source "
                                "WHERE id = 'tceq_air_permits_web'").fetchone()
    assert blocked["blocked"] and "Disallow: /" in blocked["blocked_reason"]


def test_refusal_or_challenge_stops_the_run_and_writes_nothing(committed, fast_rate):
    _plant(committed, "n-abilene-onsite", "Stargate Abilene on-site gas generation & expansion", "TX", "Taylor",
           "Crusoe")
    seen: list[str] = []
    out = air_permits.run(states=["TX"], fetcher=fetcher(seen, refuse_files=True))
    run = out["states"]["TX"]["run"]
    assert not out["ok"] and not run["ok"] and "403" in run["error"]
    assert _counts(committed)[:2] == (0, 0)
    ro = committed.execute("SELECT ok, error FROM source_run WHERE source_id = 'tceq_records_online' ORDER BY id DESC "
                           "LIMIT 1").fetchone()
    assert ro["ok"] is False and "403" in ro["error"]
    files = [u for u in seen if "GET_FILE" in u]
    assert len(files) == 1  # the first refusal ends the run: nothing is retried or worked around
    seen.clear()
    out = air_permits.run(states=["TX"], fetcher=fetcher(seen, challenge_cr=True))
    assert "challenge" in out["states"]["TX"]["run"]["error"] and len(seen) == 1
    assert _counts(committed)[:2] == (0, 0)


def test_new_mexico_run_reads_the_legal_notice(committed, fast_rate):
    _plant(committed, "n-jupiter-microgrid", "Project Jupiter on-site power plant / microgrid", "NM", "Doña Ana",
           "Stack Infrastructure (Blue Owl) / BorderPlex Digital Assets",
           air_permit={"agency": "NMED", "number": "NSR 10883", "applicant": "YGI Microgrid"})
    out = air_permits.run(states=["NM"], fetcher=fetcher([]))
    assert out["ok"], out
    main = _current(committed, "n-jupiter-microgrid")["air_permit"]
    assert (main["number"], main["status"], main["filed"], main["applicant"]) == (
        "NSR 10883", "pending", "2026-04-27", "Yucca Growth Infrastructure, LLC")
    assert main["facility"]["county"] == "Doña Ana" and main["match"]["name"]["phrase"] == "ygi microgrid"
    assert main["units"][0]["model"] == "Bloom Energy Server"
    assert {e["pollutant"]: e["value"] for e in main["emission_limits"]}["CO"] == 161.2
    assert air_permits.run(states=["NM"], fetcher=fetcher([]))["totals"]["facts_new"] == 0


def test_iowa_run_reads_the_plant_page_and_collection(committed, fast_rate):
    _plant(committed, "p-050", "Orient Energy Center", "IA", "Adair", "MidAmerican Energy")
    out = air_permits.run(states=["IA"], fetcher=fetcher([]))
    assert out["ok"], out
    run = out["states"]["IA"]["run"]
    main = _current(committed, "p-050")["air_permit"]
    assert main["agency"] == "Iowa DNR" and main["number"] == "26-A-117, 26-A-118, 26-A-119, 26-A-120, 26-A-121"
    assert (main["status"], main["issued"]) == ("issued", "2026-04-30")
    assert main["match"]["name"] == {"phrase": "midamerican", "from": "plant operator"}
    assert [u["emission_unit"] for u in main["units"]] == ["EU-1", "EU-2"]
    assert run["adapter"]["not_pdf"]  # project 26-153's file was not a PDF here: skipped, not guessed


def test_louisiana_run_ties_the_cited_station_only(committed, fast_rate):
    _plant(committed, "n-hyp-ccgt1", "Entergy Louisiana Hyperion CCGTs", "LA", "Richland", "Entergy Louisiana",
           air_permit={"agency": "LDEQ", "number": "PSD-LA-869"})
    out = air_permits.run(states=["LA"], fetcher=fetcher([]))
    assert out["ok"], out
    cov = out["states"]["LA"]["run"]["coverage"]
    assert cov["matched_plants"] == ["n-hyp-ccgt1"] and cov["facilities_found"] >= 2
    main = _current(committed, "n-hyp-ccgt1")["air_permit"]
    assert (main["agency"], main["number"], main["type"], main["status"]) == ("LDEQ", "PSD-LA-869", "PSD", "pending")
    assert main["facility"]["key"] == "AI 249132" and main["match"]["permit_numbers"]["numbers"] == ["PSD-LA-869"]
    # Richland Parish 3 and 4 (AI 249365) has the same operator and parish but no number of the plant's: reported
    other = next(u for u in cov["unmatched"] if u["facility"] == "AI 249365")
    assert "2 facilities of the operator" in other["reasons"][0]
    assert air_permits.run(states=["LA"], fetcher=fetcher([]))["totals"]["facts_new"] == 0


def _tceq(number: str, kind: str, status: str, filed: str | None, issued: str | None, **extra: object) -> dict:
    return {"agency": "TCEQ", "number": number, "type": kind, "status": status, "filed": filed, "issued": issued,
            "url": CR_URL + number.removeprefix("O"), **extra}


def test_plant_card_maps_dates_types_and_amendments():
    # Abilene's permits as stored (local database, 2026-10-21), 177263 as main with write_plant's facility record.
    amend = {"date": "2025-01-22", "text": "Final Action (Standard Permit Application)"}
    main = _tceq("177263", "Standard", "issued", "2024-08-15", "2024-08-21", ids=["177263"],
                 applicant="Crusoe Technologies Llc", facility={"key": "RN112029061", "name": "LONGHORN DATA CENTER"},
                 amendments=[dict(amend, url=FA_URL)])
    others = [("177262", "PBR", "issued", "2024-08-15", "2024-09-05"), ("182126", "PSD", "pending", None, None),
              ("182267", "NSR", "withdrawn", "2025-11-20", None), ("O4721", "Title V", "issued", None, "2026-01-16")]
    src = {"valid_from": None, "tag": "F", "source_name": "TCEQ", "source_url": main["url"]}
    every = [dict(main, facility="RN112029061"), *(_tceq(*o) for o in others)]
    card = facilities_plants.air_permit(None, {"air_permit": dict(src, value=main),
                                               "air_permits": dict(src, value=every)})
    related = [("177262", "Minor", "Permitted", "2024-08-15", "2024-09-05"),
               ("182267", "Minor", "Withdrawn", "2025-11-20", None),
               ("O4721", "Title V", "Permitted", None, "2026-01-16"), ("182126", "Major", "Permit pending", None, None)]
    assert card == {
        "agency": "TCEQ", "number": "177263", "type": "Standard", "status": "Permitted", "filed": "2024-08-15",
        "decided": "2024-08-21", "url": CR_URL + "177263",
        "amendments": [dict(amend, tag="F", sourceName="TCEQ", sourceUrl=FA_URL)],
        "holder": "Crusoe Technologies Llc", "facility": {"id": "RN112029061", "name": "LONGHORN DATA CENTER"},
        "otherIds": [], "limits": [],  # 177263's records state no limits
        "related": [{"agency": "TCEQ", "number": n, "type": t, "status": st, "filed": f, "decided": d,
                     "url": CR_URL + n.removeprefix("O")} for n, t, st, f, d in related],  # oldest filing first
    }
    # Project Jupiter's permit as stored from the NMED legal notice (same database): the limits it states, CO here.
    lim = {"pollutant": "CO", "value": 161.2, "unit": "tpy", "basis": "estimated in the application (legal notice)",
           "url": "https://service.web.env.nm.gov/urls/IzYCTYNY"}
    permit = {"agency": "NMED", "number": "NSR 10883", "facility": {"key": "NSR 10883", "name": "YGI Microgrid"},
              "applicant": "Yucca Growth Infrastructure, LLC", "emission_limits": [dict(lim, more_than=False)]}
    card = facilities_plants.air_permit(None, {"air_permit": {"value": permit, "valid_from": None, "tag": "F"}})
    assert (card["holder"], card["facility"]) == (permit["applicant"], {"id": "NSR 10883", "name": "YGI Microgrid"})
    assert card["limits"] == [dict(lim, moreThan=False)]


def test_systems_sources_schedule_and_cli():
    states = {s.state for s in SYSTEMS}
    assert {"TX", "NM", "IA", "LA", "KS", "TN", "MN", "IN", "VA", "PA", "OH", "ND", "NE", "OK", "MS", "WY", "WI", "SD",
            "IL", "WA"} <= states
    for spec in (air_permits_tx.SOURCE_CR, air_permits_tx.SOURCE_RO, air_permits_nm.SOURCE, air_permits_ia.SOURCE,
                 air_permits_la.SOURCE):
        assert "Checked 2026-10-21" in spec.terms_note and not spec.blocked
    assert air_permits_tx.SOURCE_WEB.blocked and all(s.status != "built" or s.source_id for s in SYSTEMS)
    crons = {t.cron for (n, _), t in app.periodic_registry.periodic_tasks.items() if n == "collect.air_permits"}
    assert crons == {air_permits.CRON_UTC}
    assert "air_permits" in cli._groups["collect"]
    assert json.dumps(air_permits.totals({}))  # the report shape serialises
