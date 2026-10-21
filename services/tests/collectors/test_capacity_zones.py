"""capacity_zones: market and zone rules on the grid-fact texts Marden holds, and the facts written on the test DB."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from marden import cli
from marden.collectors import capacity_zones as cz
from marden.collectors.rates_common import RunStats
from marden.core import db
from marden.core.facts import FactWriter
from marden.core.queue import app

CTX = {"pjm_zones": set(cz.RPM_ZONES), "isone_states": {"MA": {"ROP"}, "ME": {"MAINE"}, "VT": {"NNE"}},
       "isone_date": date(2024, 2, 9), "isone_period": "2027-2028"}


def _row(field: str, value: Any, fact_id: int = 1, valid_from: date = date(2026, 10, 17)) -> dict[str, Any]:
    return {"field": field, "id": fact_id, "value": value, "source_name": "Seed", "source_url": "https://example.org",
            "valid_from": valid_from}


def _classify(state: str | None = None, **texts: str) -> cz.Assignment:
    facts = {f: _row(f, v, i) for i, (f, v) in enumerate(texts.items(), start=1)}
    return cz.classify(facts, state, CTX)


@pytest.mark.parametrize(("text", "markets"), [
    ("SPP (not ERCOT)", {"spp"}),
    ("WECC (El Paso Electric; not ERCOT)", {"non_rto"}),
    ("TVA (non-RTO)", {"non_rto"}),
    ("CAISO (WECC)", {"caiso"}),
    ("ERCOT (load zone LZ_West)", {"ercot"}),
    ("ISO-NE", {"isone"}),
    ("non-RTO", {"non_rto"}),
    ("Denton/Austin: ERCOT (municipal NOIEs); Dalton: non-RTO (SERC); Marble: TVA + Duke; Muskogee: SPP",
     {"ercot", "non_rto", "spp"}),
])
def test_markets_named_in_text(text, markets):
    assert cz.markets_in(text) == markets


def test_capacity_markets_and_zones():
    dom = _classify(power_iso="PJM (Dominion zone)", power_utility="Dominion Energy Virginia")
    assert (dom.market, dom.zone, dom.basis.field) == ("pjm", "DOM", "power_iso")
    hifld = _classify(iso="PJM", utility="VIRGINIA ELECTRIC & POWER CO")  # grid_join's HIFLD names
    assert (hifld.market, hifld.zone) == ("pjm", "DOM")
    assert _classify(iso="PJM", utility="APPALACHIAN POWER CO").zone == "AEP"  # pjm_load_forecast's table
    several = _classify(iso="PJM", utility="Dominion Energy Virginia; AEP Ohio")
    assert (several.market, several.zone) == ("pjm", None) and "several PJM zones" in several.reason
    assert _classify(power_iso="MISO (Zone 9)", power_utility="Entergy Louisiana").zone == "LRZ9"
    assert _classify(power_iso="MISO (Zone 2)").market == "miso"
    no_lrz = _classify(power_iso="MISO")
    assert (no_lrz.market, no_lrz.zone) == ("miso", None) and "no MISO Local Resource Zone named" in no_lrz.reason
    # MISO's LRZ by state where the state's MISO footprint is one zone; Wisconsin and Michigan by utility
    assert _classify("IA", iso="MISO", utility="MidAmerican Energy").zone == "LRZ3"
    assert _classify("MS", iso="MISO").zone == "LRZ10"
    assert _classify("LA", iso="MISO", power_iso="MISO (Zone 9)").zone_loc.startswith("MISO zone named")
    assert _classify("WI", iso="MISO", utility="We Energies").zone == "LRZ2"
    assert _classify("WI", iso="MISO", utility="Xcel Energy (Northern States Power-Wisconsin)").zone == "LRZ1"
    assert _classify("MI", iso="MISO", utility="DTE Electric").zone == "LRZ7"
    assert _classify("WI", iso="MISO").zone is None
    assert _classify(power_iso="NYISO (Zone A)").zone == "A"
    assert (_classify("MA", iso="ISO-NE").zone, _classify("ME", iso="ISO-NE").zone) == ("ROP", "MAINE")
    assert _classify("NY", iso="ISO-NE").zone is None


def test_markets_without_a_capacity_auction():
    ercot = _classify(power_iso="ERCOT (presumed)")
    assert (ercot.market, ercot.zone) == ("none", None) and ercot.reason == cz.NO_AUCTION["ercot"]
    assert _classify(power_iso="SPP (not ERCOT)").reason == cz.NO_AUCTION["spp"]
    assert _classify(power_iso="TVA (non-RTO)").reason == cz.NO_AUCTION["non_rto"]
    # no market field: a single market named in the utility text decides
    assert _classify(power_utility="Bluebonnet Electric Cooperative (territory inference) (ERCOT)").market == "none"


def test_unassigned_subjects():
    portfolio = _classify(power_iso="PJM (VA, OH); ERCOT (Dallas); WECC non-RTO (Phoenix)")
    assert portfolio.market is None and "several markets" in portfolio.reason
    assert _classify().reason == "no grid facts"
    assert _classify(power_utility="VoltaGrid microgrid (developer/operator)").reason == "no grid fact names a market"
    # the HIFLD boundary (grid_join `iso`) beats the seed's free text
    both = cz.classify({"iso": _row("iso", "ERCOT", 1), "power_iso": _row("power_iso", "SPP", 2)}, "TX", CTX)
    assert (both.market, both.basis.field, both.reason) == ("none", "iso", cz.NO_AUCTION["ercot"])


def test_portfolio_is_not_decided_by_one_boundary_point():
    # core-scientific-coreweave: grid_join placed its one point in ERCOT, the seed names five sites' markets
    text = "Denton/Austin: ERCOT (municipal NOIEs); Dalton: non-RTO (SERC); Marble: TVA + Duke; Muskogee: SPP"
    none = cz.classify({"iso": _row("iso", "ERCOT", 1), "power_iso": _row("power_iso", text, 2)}, None, CTX)
    assert (none.market, none.zone, none.basis.field) == ("none", None, "power_iso")
    assert all(cz.NO_AUCTION[m] in none.reason for m in ("ercot", "non_rto", "spp"))
    # one site in a capacity market: unassigned, even though the boundary point says ERCOT
    mixed = cz.classify({"iso": _row("iso", "ERCOT", 1), "power_iso": _row("power_iso", "PJM (VA); ERCOT (Dallas)", 2)},
                        "VA/TX", CTX)
    assert mixed.market is None and mixed.reason == "facts name several markets ['ercot', 'pjm']"


def _site(committed, slug: str, state: str, v1: bool, facts: dict[str, tuple[str, date]]) -> None:
    committed.execute("INSERT INTO site (slug, name, state, in_v1) VALUES (%s, %s, %s, %s)", (slug, slug, state, v1))
    with db.transaction() as conn:
        writer = FactWriter(conn)
        for name, (value, day) in facts.items():
            writer.add("site", slug, name, value, tag="R", source_name="DCD", source_url=f"https://example.org/{slug}",
                       valid_from=day, producer="seed:test", status="published")


def test_assign_writes_model_facts_once(committed):
    _site(committed, "va-site", "VA", True, {"power_iso": ("PJM (Dominion zone)", date(2026, 10, 17)),
                                             "power_utility": ("Dominion Energy Virginia", date(2026, 10, 1))})
    _site(committed, "tx-site", "TX", True, {"power_iso": ("ERCOT", date(2026, 10, 17))})
    _site(committed, "multi", "VA/TX", False, {"power_iso": ("PJM (VA); ERCOT (Dallas)", date(2026, 10, 17))})
    stats = RunStats()
    with db.transaction() as conn:
        summary = cz.assign(conn, stats)
    assert summary["v1_sites"] == {"va-site": "pjm:DOM", "tx-site": f"none ({cz.NO_AUCTION['ercot']})"}
    assert summary["unassigned"] == {"site:multi": "facts name several markets ['ercot', 'pjm']"}
    assert summary["by_market"] == {"none": 1, "pjm": 1, "unassigned": 1}
    rows = {(r["subject_id"], r["field"]): r for r in committed.execute(
        "SELECT * FROM fact_current WHERE field LIKE 'capacity%%'").fetchall()}
    assert set(rows) == {("va-site", "capacity_market"), ("va-site", "capacity_zone"), ("tx-site", "capacity_market"),
                         ("tx-site", "capacity_market_reason")}
    zone = rows[("va-site", "capacity_zone")]
    assert (zone["value"], zone["tag"], zone["method_version"], zone["producer"]) == (
        "DOM", "M", cz.METHOD_VERSION, cz.PRODUCER)
    assert zone["quote"] == "PJM (Dominion zone)" and zone["source_url"] == "https://example.org/va-site"
    assert zone["valid_from"] == date(2026, 10, 17)  # the newest input
    assert rows[("tx-site", "capacity_market")]["value"] == "none"
    again = RunStats()
    with db.transaction() as conn:
        cz.assign(conn, again)
    assert again.facts_new == 0 and again.facts_seen == stats.facts_seen == 4


def test_model_task_and_command():
    assert app.tasks["collect.capacity_zones"].queue == "collect"  # queued like grid_join, the other internal join
    assert "capacity_zones" in cli._groups["collect"]
