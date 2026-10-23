"""Delivery record and Mispriced read models: the pure rules (parties, slips, ranking, groups, trust notes)."""

from __future__ import annotations

from marden.readmodels.views import delivery as dv
from marden.readmodels.views import delivery_parties as dp
from marden.readmodels.views import delivery_sites as ds
from marden.readmodels.views import mispriced as mp


def keys(parties: list[dp.Party]) -> list[str]:
    return [p.key for p in parties]


def test_labels_resolve_to_one_company_each():
    assert keys(dp.from_label("Crusoe / Blue Owl")) == ["crusoe-energy-systems", "blue-owl-capital"]
    assert keys(dp.from_label("STACK (Blue Owl)")) == ["stack-infrastructure"]  # the owner in brackets is not a party
    assert keys(dp.from_label("Fluidstack (Google)")) == ["fluidstack"]  # 'stack' inside Fluidstack is not STACK
    assert keys(dp.from_label("Solaris Energy Infrastructure (50.1%) / MZX Tech (49.9%)")) == [
        "solaris-energy-infrastructure",
        "mzx-tech",
    ]
    assert dp.from_label("n/d") == [] and dp.from_label("Colocation customers") == []


def test_issuers_resolve_through_parent_or_name():
    assert keys(dp.from_issuer("Cipher Compute LLC", "Cipher Compute LLC", "Cipher Digital")) == ["cipher-digital-inc"]
    assert keys(dp.from_issuer("WULF Compute LLC", "WULF Compute LLC", "TeraWulf Inc.")) == ["terawulf-inc"]
    assert keys(dp.from_issuer("Abilene JV (Crusoe / Blue Owl / Primary Digital)", "Abilene JV", None)) == [
        "crusoe-energy-systems",
        "blue-owl-capital",
    ]
    assert dp.from_issuer("Beignet Investor LLC", "Beignet Investor LLC", None) == []


def c(month: str) -> dict:
    return {"month": month}


def test_slips_against_the_first_target():
    now = ds.month_no("2026-10-22")
    assert ds.judged(c("2026-09"), c("2026-12"), done=False, now=now) == ("open_late", 3, False)
    assert ds.judged(c("2026-12"), c("2026-12"), done=False, now=now) == ("open", 0, False)
    assert ds.judged(c("2026-08"), c("2025-09"), done=True, now=now) == ("on_time", -11, False)
    assert ds.judged(c("2026-01"), c("2026-04"), done=True, now=now) == ("late", 3, False)
    # still open past its own date: late by at least the months since the first target
    assert ds.judged(c("2026-03"), c("2026-03"), done=False, now=now) == ("open_late", 7, True)
    assert ds.judged(None, c("2026-01"), done=True, now=now)[0] == "done_unjudged"


def row(name: str, avg: float | None, pushes: int, roles: list[str]) -> dict:
    return {
        "name": name,
        "roles": roles,
        "avgSlipMonths": {"value": avg},
        "pushes90d": {"value": pushes},
        "worstSlipMonths": {"value": avg},
        "sitesTracked": {"value": 1},
    }


def test_rank_worst_first_and_peer_median():
    rows = [
        row("A", 1.0, 0, ["developer"]),
        row("B", None, 5, ["tenant"]),
        row("C", 7.0, 1, ["developer"]),
        row("D", 3.0, 2, ["developer"]),
    ]
    summary = dv.peers(rows)
    assert summary["medianAvgSlipMonths"]["value"] == 3.0 and summary["developers"] == 3
    assert [r["name"] for r in sorted(rows, key=dv.rank_key)] == ["C", "D", "A", "B"]
    assert rows[2]["vsPeers"]["deltaMonths"] == 4.0
    assert rows[1]["vsPeers"]["deltaMonths"] is None


def ev(pts: float | None = None, severity: str | None = None, when: str = "2026-10-21") -> dict:
    return {"effectPts": pts, "severity": severity, "date": when, "kind": "slip", "text": "x"}


def test_groups():
    assert mp.groups_of(-7.8, [])[0] == ["downside"]
    assert mp.groups_of(-1.0, [ev(-0.5)])[0] == ["downside"]  # negative event since, market above us
    assert mp.groups_of(1.0, [ev(-0.5)])[0] == []  # a negative event but the model still sits above the market
    assert mp.groups_of(3.2, [])[0] == ["bounce"]
    assert mp.groups_of(0.5, [ev(severity="green")])[0] == ["bounce"]
    assert mp.groups_of(0.4, [ev(0.01)])[0] == []


def test_stale_marks_need_a_later_trade_three_points_below():
    fund = {"price": 97.0, "date": "2026-07-31"}
    assert mp.stale_of(fund, {"price": 81.75, "date": "2026-10-20"})["gapPts"]["value"] == 15.25
    assert mp.stale_of(fund, {"price": 95.0, "date": "2026-10-20"}) is None
    assert mp.stale_of(fund, {"price": 80.0, "date": "2026-07-01"}) is None  # the trade is older than the marks


def test_confidence_notes():
    trade = {"kind": "trade", "date": "2026-10-20", "price": 98.0}
    assert mp.confidence(None, [], None, [], []).startswith("Model only: no trade, quote or fund mark")
    assert mp.confidence({"kind": "fund_mark", "date": "2026-06-30"}, [], -10, [], []).startswith("Fund marks only")
    assert mp.confidence(trade, [], -2, [], [ev(-1, when="2026-10-21")]) == "Model only: no trade or quote since Oct 21"
