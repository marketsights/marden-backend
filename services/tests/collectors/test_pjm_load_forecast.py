"""pjm_load_forecast: Table B-9b from the real 2026 workbook, zone lookup from our utility facts, facts on
PJM sites only, reruns, and a live run (network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from marden.collectors import pjm_load_forecast as lf
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, sources
from marden.core.facts import FactWriter
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "pjm_load_forecast"
WORKBOOK = DATA / "total-load-adjustments-breakdown.xlsx"
URL = "https://www.pjm.com/-/media/DotCom/planning/res-adeq/load-forecast/total-load-adjustments-breakdown.xlsx"


def test_parses_table_b9b():
    table = lf.parse_table(WORKBOOK.read_bytes())
    assert table["table"] == "Table B-9b"
    assert table["title"].startswith("Total Adjustments to Summer Peak Load (MW) for Each PJM Zone and RTO")
    dom = table["zones"]["DOM"]
    assert dom["by_year"]["2026"] == 7066 and dom["by_year"]["2030"] == 13896
    assert sorted(dom["areas"]) == ["DOM", "NVEC", "ODEC", "REC"] and dom["areas"]["NVEC"]["2026"] == 1995.4
    assert table["zones"]["AEP"]["areas"]["AEPOHIO"]["2026"] == 1743.8
    assert table["zones"]["PJM RTO"]["by_year"]["2026"] == 11479
    with pytest.raises(ParseError):
        lf.parse_table(b"not a workbook")


def test_finds_the_workbook_link():
    assert lf.find_workbook((DATA / "load_forecast_page_excerpt.html").read_bytes()) == URL


def test_zones_from_utility_text():
    assert lf.zones_for("Dominion Energy Virginia") == [("DOM", None)]
    assert lf.zones_for("Dominion Energy Virginia (VA, presumed), AEP Ohio (Columbus/New Albany, presumed), "
                        "Oncor (Dallas, presumed), APS or SRP (Phoenix, n/d)") == [("DOM", None), ("AEP", "AEPOHIO")]
    assert lf.zones_for("NOVEC") == [("DOM", "NVEC")] and lf.zones_for("Entergy Louisiana") == []


def _site(conn, slug: str, iso: str, utility: str) -> None:
    conn.execute("INSERT INTO site (slug, name) VALUES (%s, %s)", (slug, slug))
    w = FactWriter(conn)
    for field, value in (("power_iso", iso), ("power_utility", utility)):
        w.add("site", slug, field, value, tag="R", source_name="test", valid_from=date(2026, 10, 1),
              producer="seed:test", status="published")


def test_load_writes_zone_facts_on_pjm_sites_only(committed):
    _site(committed, "cloudhq", "PJM (Dominion zone)", "Dominion Energy Virginia")
    _site(committed, "hyperion", "MISO (Zone 9)", "Entergy Louisiana")
    sources.upsert_spec(lf.SOURCE)
    doc = documents.record_document(lf.SOURCE_ID, URL, WORKBOOK.read_bytes(), content_type="application/xlsx",
                                    meta={"http": {"last_modified": "Wed, 14 Jan 2026 19:10:31 GMT"}})
    stats = RunStats()
    lf.load(doc, stats)
    assert stats.facts_new == 1 and stats.extra["subjects"] == {"site:cloudhq": ["DOM"]}
    row = committed.execute(f"SELECT * FROM fact_current WHERE subject_id = 'cloudhq' AND field = '{lf.FIELD}'"
                            ).fetchone()
    assert (row["tag"], row["source_name"], row["valid_from"], row["source_url"]) == (
        "F", "PJM", date(2026, 1, 14), URL)
    zone = row["value"]["zones"][0]
    assert zone["zone"] == "DOM" and zone["by_year"]["2026"] == 7066 and zone["zone_from"]["presumed"] is False
    again = RunStats()
    lf.load(doc, again)
    assert again.facts_new == 0
    assert app.tasks["collect.pjm_load_forecast"].queue == "collect"


@pytest.mark.network
def test_live_run(committed):
    out = lf.run()
    assert out["ok"], out
    assert "DOM" in out["pjm_zones"] and out["documents_seen"] == 2
