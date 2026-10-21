"""eia_860m: the release page (real), robots.txt (real), the August 2026 workbook (real rows, trimmed), change-only
history and slip counts, plant matching, idempotent reruns, the schedule and a live run (network)."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import eia_860m
from marden.collectors import eia_860m_parse as parse
from marden.collectors import eia_860m_store as store
from marden.collectors.rates_common import ParseError
from marden.core import sources
from marden.core.facts import FactWriter
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "eia_860m"
PAGE = (DATA / "eia860m_page.html").read_text()
ROBOTS = (DATA / "robots.txt").read_text()
WORKBOOK = (DATA / "august_generator2026_trimmed.xlsx").read_bytes()
NEWEST = "https://www.eia.gov/electricity/data/eia860m/xls/august_generator2026.xlsx"
ETAG = '"8098ac47aa4bdd1:0"'
# Generators on the trimmed Planned sheet in data-center states (Lerdo Middle, CA, is not one).
DC_PLANNED = {"63798_ATL01", "69834_AVON2", "64672_BLUJS", "65043_DUSO", "65811_CYSTD", "70203_GEN1", "3122_CA5",
              "3122_CA6", "59047_GEN 1", "3122_CA1"}


def _fetcher(seen: list[str]) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url == eia_860m.ROBOTS_URL:
            return httpx.Response(200, text=ROBOTS, headers={"content-type": "text/plain"})
        if url == eia_860m.PAGE_URL:
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
        if url == NEWEST:
            if request.headers.get("if-none-match") == ETAG:
                return httpx.Response(304, headers={"etag": ETAG})
            return httpx.Response(200, content=WORKBOOK, headers={"etag": ETAG, "content-type": eia_860m.XLSX})
        return httpx.Response(404)

    return Fetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)


def _facts(committed, subject_type: str, subject_id: str) -> dict[str, object]:
    rows = committed.execute(
        "SELECT field, value FROM fact_current WHERE subject_type = %s AND subject_id = %s", (subject_type, subject_id)
    ).fetchall()
    return {r["field"]: r["value"] for r in rows}


def test_page_lists_released_files_and_ignores_commented_placeholders():
    files, others = parse.list_files(PAGE, eia_860m.PAGE_URL)
    newest = files[-1]
    assert (newest.label, newest.url, newest.archived) == ("2026-08", NEWEST, False)
    assert (files[0].label, len(files)) == ("2015-07", 134)  # July 2015 .. August 2026, one per month
    assert {f.label for f in files}.isdisjoint({"2026-09", "2026-10", "2026-11", "2026-12"})  # commented out
    assert all(f.archived for f in files[:-1]) and files[-2].url.endswith("/archive/xls/july_generator2026.xlsx")
    assert others == 1  # energy_source_codes_prime_mover.xlsx
    with pytest.raises(ParseError, match="lists no"):
        parse.list_files("<html><!-- <a href='xls/may_generator2026.xlsx'> --></html>", eia_860m.PAGE_URL)


def test_robots_rules_allow_the_newest_file_and_disallow_the_archive():
    rules = parse.robots_rules(ROBOTS, "MardenBot")
    assert parse.robots_allowed(rules, "/electricity/data/eia860m/xls/august_generator2026.xlsx")
    assert parse.robots_allowed(rules, "/electricity/data/eia860m/")
    assert not parse.robots_allowed(rules, "/electricity/data/eia860m/archive/xls/july_generator2026.xlsx")
    assert parse.robots_allowed(rules, "/beta/international/x") and not parse.robots_allowed(rules, "/beta/x")
    assert parse.robots_allowed(rules, "/reports/upcoming.php") and not parse.robots_allowed(rules, "/reports/a")
    own = parse.robots_rules("User-agent: *\nDisallow: /\n\nUser-agent: MardenBot\nAllow: /\nDisallow: /x$",
                             "MardenBot/0.1")
    assert parse.robots_allowed(own, "/a") and not parse.robots_allowed(own, "/x")


def test_parses_the_real_workbook():
    report = parse.parse_workbook(WORKBOOK, (2026, 8))
    assert report.label == "2026-08"
    assert report.rows == {"Operating": 6, "Planned": 11, "Retired": 3, "Canceled or Postponed": 3}
    assert report.skipped == {"blank": 4, "notes": 4, "no_plant_id": 1}  # 'Bonus' (retired 1968) has no Plant ID
    gens = {g.key: g for g in report.generators}
    atlas = gens["63798_ATL01"]
    assert (atlas.sheet, atlas.status, atlas.planned, atlas.state) == ("Planned", "V", "2026-08", "AZ")
    assert atlas.status_text == "(V) Under construction, more than 50 percent complete"
    assert atlas.descriptor["plant_name"] == "Atlas" and atlas.descriptor["nameplate_mw"] == 300
    assert atlas.descriptor["latitude"] == 33.94345 and "unit_code" not in atlas.descriptor
    assert gens["59047_GEN 1"].planned == "2028-11" and gens["69834_AVON2"].status == "TS"
    gaia = gens["66342_GAIAB"]
    assert (gaia.sheet, gaia.status, gaia.operating, gaia.planned) == ("Operating", "OP", "2026-07", None)
    assert (gens["361_1"].operating, gens["361_1"].status) == ("1899-02", "OS")  # Santa Ana 1 hydro
    menasha = gens["4127_IC1"]
    assert (menasha.status, menasha.operating, menasha.retired) == ("retired", "1949", "2014-06")  # month code 0
    assert gens["3489_CT1"].status == "canceled_or_postponed"
    assert gens["3489_CT1"].status_text.startswith("Inventory of Canceled or Indefinitely Postponed Projects")
    with pytest.raises(ParseError, match="expected"):
        parse.parse_workbook(WORKBOOK, (2026, 7))
    with pytest.raises(ParseError, match="not a readable workbook"):
        parse.parse_workbook(b"<html>maintenance</html>")


def test_run_writes_planned_history_once_and_never_fetches_the_archive(committed):
    seen: list[str] = []
    first = eia_860m.run(fetcher=_fetcher(seen))
    assert first["ok"], first
    assert not any("/archive/" in u for u in seen) and seen.count(NEWEST) == 1
    backfill = first["backfill"]
    assert backfill["window"] == ["2024-09", "2026-08"] and len(backfill["robots_disallowed"]) == 23
    assert backfill["robots_disallowed"][0] == "2024-09" and backfill["outside_window"] == 110
    (report,) = first["reports"]
    assert report["report"] == "2026-08" and report["dc_state_planned"] == len(DC_PLANNED)
    assert report["slips"] == {"first_seen_planned": 10, "planned": 10}
    assert report["facts_new"] == {f"eia_generator.{f}": 10 for f in ("generator", "planned_online", "status")}
    ids = {r["subject_id"] for r in committed.execute(
        "SELECT DISTINCT subject_id FROM fact WHERE subject_type = 'eia_generator'").fetchall()}
    assert ids == DC_PLANNED
    row = committed.execute(
        """SELECT tag, source_name, source_url, valid_from, producer, document_id, quote, quote_loc, status
             FROM fact WHERE subject_type = 'eia_generator' AND subject_id = '3122_CA5' AND field = 'status'"""
    ).fetchone()
    assert (row["tag"], row["source_name"], row["source_url"], row["valid_from"]) == (
        "F", "EIA-860M", NEWEST, date(2026, 8, 31))
    assert row["producer"] == "collector:eia_860m" and row["status"] == "published" and row["document_id"]
    assert row["quote"] == "(P) Planned for installation, but regulatory approvals not initiated"
    assert row["quote_loc"] == "august_generator2026.xlsx!Planned row 11"
    assert _facts(committed, "eia_generator", "3122_CA5")["planned_online"] == "2028-07"

    second = eia_860m.run(fetcher=_fetcher(seen))
    assert second["ok"], second
    assert second["facts_new"] == 0 and second["documents_new"] == 0 and second["documents_seen"] == 3
    runs = committed.execute(
        "SELECT ok, items_new, meta FROM source_run WHERE source_id = 'eia_860m' ORDER BY id").fetchall()
    assert [r["ok"] for r in runs] == [True, True] and runs[1]["items_new"] == 0
    assert runs[0]["meta"]["reports"][0]["slips"]["planned"] == 10


def _prior(conn, sid: str, values: dict[str, object]) -> None:
    """What an earlier report said about a generator, as this collector writes it (valid_from = that report's
    month end). Test setup only: the July 2026 workbook sits under /archive/, which robots.txt disallows."""
    facts = FactWriter(conn)
    for fld, value in values.items():
        facts.add(store.SUBJECT, sid, fld, value, tag="F", source_name=store.SOURCE_NAME,
                  source_url="https://www.eia.gov/electricity/data/eia860m/archive/xls/july_generator2026.xlsx",
                  valid_from=date(2026, 7, 31), producer=store.PRODUCER, status="published")


def test_slips_and_outcomes_are_counted_against_the_previous_report(committed):
    sources.upsert_spec(eia_860m.SOURCE)
    _prior(committed, "3122_CA5", {"status": "P", "planned_online": "2028-01"})  # now 2028-07: slipped
    _prior(committed, "63798_ATL01", {"status": "V", "planned_online": "2026-10"})  # now 2026-08: pulled in
    _prior(committed, "66342_GAIAB", {"status": "V", "planned_online": "2026-05"})  # online 2026-07
    _prior(committed, "3489_CT1", {"status": "L", "planned_online": "2027-06"})  # now canceled or postponed
    _prior(committed, "99999_GONE", {"status": "U", "planned_online": "2026-12"})  # not in the report
    out = eia_860m.run(fetcher=_fetcher([]))
    assert out["ok"], out
    slips = out["reports"][0]["slips"]
    assert slips == {"came_online": 1, "came_online_after_last_plan": 1, "canceled_or_postponed": 1,
                     "first_seen_planned": 8, "planned": 10, "planned_then_unlisted": 1, "pulled_in": 1,
                     "slipped": 1}
    assert out["reports"][0]["slipped_by_state"] == {"PA": 1}
    gaia = _facts(committed, "eia_generator", "66342_GAIAB")
    assert (gaia["status"], gaia["operating_month"], gaia["planned_online"]) == ("OP", "2026-07", "2026-05")
    assert _facts(committed, "eia_generator", "3489_CT1")["status"] == "canceled_or_postponed"
    assert "66342_GAIAS" not in {r["subject_id"] for r in committed.execute(
        "SELECT subject_id FROM fact WHERE subject_type = 'eia_generator'").fetchall()}  # never tracked as planned
    history = committed.execute(
        """SELECT valid_from, value FROM fact WHERE subject_type = 'eia_generator' AND subject_id = '3122_CA5'
             AND field = 'planned_online' ORDER BY valid_from""").fetchall()
    assert [(r["valid_from"], r["value"]) for r in history] == [(date(2026, 7, 31), "2028-01"),
                                                                 (date(2026, 8, 31), "2028-07")]
    unchanged = committed.execute(
        """SELECT count(*) AS n FROM fact WHERE subject_type = 'eia_generator' AND subject_id = '3122_CA5'
             AND field = 'status'""").fetchone()
    assert unchanged["n"] == 1  # 'P' in both reports: not written again


def _plant(conn, pid: str, name: str, state: str, *, eia_id: int | None = None,
           point: tuple[float, float] | None = None) -> None:
    geom = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)" if point else "NULL"
    params = [pid, name, state, eia_id, *((point[1], point[0]) if point else ())]
    conn.execute(f"INSERT INTO power_plant (id, name, state, eia_plant_id, geom) VALUES (%s, %s, %s, %s, {geom})",
                 params)


def test_plants_match_by_eia_id_or_name_state_and_two_km(committed):
    _plant(committed, "p-atlas", "Atlas solar", "AZ", eia_id=63798)
    _plant(committed, "p-homer", "Homer City Energy Campus gas plant", "PA", point=(40.5218, -79.1961))  # 1.0 km
    _plant(committed, "p-homer-far", "Homer City Energy Campus", "PA", point=(40.6028, -79.1961))  # 10 km
    _plant(committed, "p-homer-noloc", "Homer City Energy Campus", "PA")
    _plant(committed, "p-homer-oh", "Homer City Energy Campus", "OH", point=(40.5218, -79.1961))  # other state
    out = eia_860m.run(fetcher=_fetcher([]))
    assert out["ok"], out
    plants = out["reports"][0]["plants"]
    assert plants == {"plants": 5, "matched_by_eia_plant_id": 1, "matched_by_name_location": 1,
                      "name_match_not_within_2km": 1, "name_match_without_location": 1, "no_name_match": 1,
                      "name_candidates_without_location": [{
                          "plant": "p-homer-noloc", "eia_plant_id": 3122, "eia_name": "Homer City Generating Station"}]}
    atlas = _facts(committed, "plant", "p-atlas")
    assert atlas == {"eia_status": {"ATL01": "V"}, "planned_online": {"ATL01": "2026-08"}}
    homer = committed.execute(
        """SELECT field, value, tag, method_version, quote_loc FROM fact
            WHERE subject_type = 'plant' AND subject_id = 'p-homer' ORDER BY field""").fetchall()
    assert [(r["field"], r["tag"], r["method_version"]) for r in homer] == [
        ("eia_plant_id", "M", store.MATCH_METHOD), ("eia_status", "M", store.MATCH_METHOD),
        ("planned_online", "M", store.MATCH_METHOD)]
    by_field = {r["field"]: r for r in homer}
    assert by_field["eia_plant_id"]["value"] == 3122 and "1.00 km" in by_field["eia_plant_id"]["quote_loc"]
    assert by_field["eia_status"]["value"] == {"1": "retired", "CA1": "P", "CA5": "P", "CA6": "P"}
    assert by_field["planned_online"]["value"] == {"CA1": "2029-11", "CA5": "2028-07", "CA6": "2028-07"}
    assert _facts(committed, "plant", "p-homer-far") == {}
    dirty = {r["key"] for r in committed.execute("SELECT key FROM read_model_dirty WHERE view = 'plant_row'")}
    assert dirty == {"p-atlas", "p-homer"}
    assert eia_860m.run(fetcher=_fetcher([]))["facts_new"] == 0


def test_imported_file_must_be_the_month_it_is_named(tmp_path, committed):
    shutil.copy(DATA / "august_generator2026_trimmed.xlsx", tmp_path / "july_generator2026.xlsx")
    out = eia_860m.run(fetcher=_fetcher([]), import_dir=tmp_path)
    assert not out["ok"] and "ParseError" in out["error"] and "expected (2026, 7)" in out["error"]
    doc = committed.execute(
        "SELECT url, meta FROM document WHERE source_id = 'eia_860m' AND external_id = 'july_generator2026.xlsx'"
    ).fetchone()
    assert doc["url"].endswith("/archive/xls/july_generator2026.xlsx") and "by a person" in doc["meta"]["obtained"]
    assert committed.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0
    source = committed.execute("SELECT last_error FROM source WHERE id = 'eia_860m'").fetchone()
    assert "expected (2026, 7)" in source["last_error"]


def test_task_runs_weekly_in_the_collect_queue_and_cli_is_registered():
    task = app.tasks["collect.eia_860m"]
    assert task.queue == "collect"
    periodic = {t.cron for (n, _), t in app.periodic_registry.periodic_tasks.items() if n == "collect.eia_860m"}
    assert periodic == {eia_860m.CRON_UTC}
    assert "eia_860m" in cli._groups["collect"]
    assert eia_860m.SOURCE.cadence_minutes == 7 * 24 * 60 and not eia_860m.SOURCE.blocked
    assert "Disallow: /*archive/" in eia_860m.TERMS_NOTE and "public domain" in eia_860m.TERMS_NOTE


@pytest.mark.network
def test_live_run_twice_writes_nothing_new(committed):
    first = eia_860m.run()
    assert first["ok"], first
    newest = first["reports"][-1]
    assert newest["report"] >= "2026-08" and newest["dc_state_planned"] > 500
    assert first["facts_new"] >= 3 * newest["dc_state_planned"]
    second = eia_860m.run()
    assert second["ok"], second
    assert second["facts_new"] == 0 and second["documents_new"] == 0
