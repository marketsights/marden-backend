"""Recovery v0.3, the issuer balance-sheet waterfall (marden.models.recovery_issuer), and price v0.3, which prices
issuer bonds at the issuer's unsecured recovery and solves the issuer's bond-implied curve at that same recovery.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from price_rows import copy_price_inputs

from marden.core import db
from marden.models import recovery_issuer as ri
from marden.models import runs
from marden.models.price_chain import IssuerRecovery, issuer_recovery, recovery_basis, recovery_doc

DAY = date(2026, 10, 14)
F = {"tag": "F", "asOf": "2026-06-30", "sourceName": "SEC XBRL: Test 10-Q"}


def _claim(iid: str, rank: str, amount: float, **kw: Any) -> ri.Claim:
    return ri.Claim(iid, iid, kw.pop("kind", "bond"), rank, amount, {**F, "value": amount}, **kw)


def _issuer(**kw: Any) -> ri.IssuerInputs:
    base: dict[str, Any] = {
        "issuer": "acme", "name": "Acme", "filer": "acme", "period_end": date(2026, 6, 30),
        "values": {"total_assets_usd": 100e9, "cash_usd": 10e9, "receivables_usd": 5e9, "ppe_net_usd": 60e9,
                   "operating_lease_rou_asset_usd": 15e9, "goodwill_usd": 5e9, "total_liabilities_usd": 90e9,
                   "operating_lease_liability_usd": 15e9, "operating_lease_liability_current_usd": 1e9,
                   "finance_lease_liability_usd": 1e9},
        "gpu_share": 0.5, "gpu_keep": 0.6,
    }
    return ri.IssuerInputs(**{**base, **kw})


def test_claims_rank_by_seniority_and_where_they_sit():
    top = {"acme"}
    assert ri.classify("Senior unsecured", "bond", "acme", top, False) == "unsecured"
    assert ri.classify("Convertible", "convert", "acme", top, False) == "unsecured"
    assert ri.classify("Junior subordinated", "bond", "acme", top, False) == "junior"
    assert ri.classify("Secured (SPV)", "ddtl", "acme-spv", top, False) == "spv"
    assert ri.classify("Senior secured", "bond", "acme-opco", top, False) == "spv"  # below the parent: ahead of it
    assert ri.classify("Senior secured", "bond", "acme", top, True) == "spv"  # sited project debt
    assert ri.classify("Secured (parent)", "facility", "acme-holdco", top, False) == "parent"
    assert ri.classify("Senior secured", "facility", "acme", top, False) == "parent"
    for not_debt in ("Guarantee / off-balance-sheet", "Preferred / hybrid", "Lease / other", "TBD (collateral n/d)",
                     "Private credit (per BDC schedule)", None):
        assert ri.classify(not_debt, "other", "acme", top, False) is None
    assert ri.classify("Senior unsecured", "lease", "acme", top, False) is None  # leases come from the balance sheet
    assert ri.guaranteed("pledge of equity; parent-guaranteed") and not ri.guaranteed("non-recourse to the parent")
    assert not ri.guaranteed("no parent guarantee") and not ri.guaranteed(None)


def test_lease_rejection_claims_are_capped_like_502b6():
    assert ri.lease_cap(10.0, 4.0) == 10.0  # one year (15% of 4 years is less)
    assert ri.lease_cap(10.0, 12.0) == pytest.approx(18.0)  # 15% of 12 years
    assert ri.lease_cap(10.0, 40.0) == 30.0  # at most three years
    rent, years, claim = ri.commenced_lease(16.319e9, 0.584e9)
    assert rent == pytest.approx(0.584e9 + ri.LEASE_RATE * 16.319e9)
    assert rent * ri.annuity(years) == pytest.approx(16.319e9, rel=1e-6)  # the liability is the rent's present value
    assert claim == pytest.approx(ri.lease_cap(rent, years)) and claim < 16.319e9
    _, years_ifrs, _ = ri.commenced_lease(1e9, None)
    assert years_ifrs == ri.UNKNOWN_LEASE_YEARS
    assert ri.term_years("7 to 16 years") == 11.5 and ri.term_years("15 years") == 15 and ri.term_years(None) is None


def test_waterfall_pays_spv_debt_first_then_parent_then_unsecured_pro_rata():
    inp = _issuer(claims=[
        _claim("SPV-A", "spv", 8e9, kind="ddtl", pool="gpus:A", recourse=True),  # guaranteed: shortfall moves up
        _claim("SPV-B", "spv", 4e9, kind="ddtl", pool="gpus:B"),  # non-recourse: shortfall stays at the SPV
        _claim("RCF", "parent", 2e9, kind="facility"),
        _claim("NOTE", "unsecured", 20e9),
        _claim("CVT", "unsecured", 5e9, kind="convert"),
    ], collateral={"gpus:A": ri.Collateral("gpus:A", "pool A", 5e9), "gpus:B": ri.Collateral("gpus:B", "pool B", 1e9)})
    w = ri.waterfall(inp)
    by = {a["key"]: a for a in w["assets"]}
    assert by["cash"]["valueUsd"] == 10e9 and by["receivables"]["valueUsd"] == pytest.approx(4e9)
    assert by["gpus"]["valueUsd"] == pytest.approx(30e9 * 0.6) and by["buildings"]["valueUsd"] == pytest.approx(
        30e9 * ri.BUILDING_KEEP)
    assert by["rou"]["valueUsd"] == 0 and by["goodwill"]["valueUsd"] == 0
    assert by["other"]["valueUsd"] == pytest.approx(5e9 * ri.KEEP["other"])  # 100 - 10 - 5 - 60 - 15 - 5
    value = 10e9 + 4e9 + 18e9 + 16.5e9 + 1.25e9
    assert w["assetsUsd"] == pytest.approx(value)
    assert w["spvCoveredUsd"] == pytest.approx(6e9)  # each pool takes its collateral, no more
    assert w["parent"]["coveredUsd"] == pytest.approx(3e9)  # the facility and the finance leases
    assert w["leftUsd"] == pytest.approx(value - 9e9)
    claims = {u["key"]: u["claimsUsd"] for u in w["unsecured"]}
    assert claims["notes"] == 20e9 and claims["converts"] == 5e9
    assert claims["shortfall"] == pytest.approx(3e9)  # pool A's guaranteed shortfall only
    _, _, lease = ri.commenced_lease(15e9, 1e9)
    assert claims["leases"] == pytest.approx(lease)
    # other liabilities: total liabilities less the debt on the balance sheet and the leases
    assert claims["other"] == pytest.approx(90e9 - 39e9 - 15e9 - 1e9)
    assert w["rates"]["unsecured"] == pytest.approx(w["leftUsd"] / sum(claims.values()))
    assert w["rates"]["parent"] == 1.0
    worse, better = ri.waterfall(inp, 0.8), ri.waterfall(inp, 1.2)
    assert worse["rates"]["unsecured"] < w["rates"]["unsecured"] < better["rates"]["unsecured"]


def test_structural_subordination_and_the_clamp():
    rich = _issuer(values={**_issuer().values, "cash_usd": 60e9, "ppe_net_usd": 10e9, "total_liabilities_usd": 20e9},
                   claims=[_claim("NOTE", "unsecured", 1e9)])
    doc = ri.issuer_doc(rich, DAY, "recovery v0.3")
    assert doc["ranks"]["unsecured"]["cents"] == 95  # at most 95c
    poor = _issuer(claims=[_claim("SPV", "spv", 90e9, pool="site:x"), _claim("NOTE", "unsecured", 10e9)])
    doc = ri.issuer_doc(poor, DAY, "recovery v0.3")  # the SPV takes everything first (no collateral model: covered)
    assert doc["leftUsd"] == 0 and doc["ranks"]["unsecured"]["cents"] == 5  # at least 5c
    steps = doc["lines"]["unsecured"]
    assert [s["label"] for s in steps] == ["Assets after haircuts", "Less secured claims", "Left for unsecured",
                                           "Recovery"]
    assert [s["op"] for s in steps] == [None, "minus", "equals", "equals"] and steps[-1]["cents"] == 5
    assert "SPV and site debt" in steps[1]["note"] and "GPUs 60% of book" in steps[0]["note"]


def test_debt_raised_after_the_balance_sheet_counts_its_proceeds():
    inp = _issuer(claims=[_claim("NEW", "unsecured", 4e9, issue_date=date(2026, 9, 1))])
    w = ri.waterfall(inp)
    raised = next(a for a in w["assets"] if a["key"] == "raised")
    assert raised["bookUsd"] == 4e9 and raised["keep"] == pytest.approx(w["ppeBlend"])
    claims = {u["key"]: u["claimsUsd"] for u in w["unsecured"]}
    assert claims["other"] == pytest.approx(90e9 - 15e9 - 1e9)  # not on the June balance sheet


def test_an_issuer_without_filings_keeps_a_stated_assumption():
    doc = ri.issuer_doc(ri.IssuerInputs("privco", "PrivCo", None), DAY, "recovery v0.3")
    assert doc["method"] is None and doc["assumedCents"] == 40 and doc["private"] is True
    assert "no SEC filings" in doc["missing"][0] and "40% senior unsecured convention" in doc["missing"][0]
    listed = ri.issuer_doc(ri.IssuerInputs("pubco", "PubCo", None, listed=True), DAY, "recovery v0.3")
    assert "files with the SEC" in listed["missing"][0] and listed["private"] is False


def test_price_recovery_doc_says_which_recovery_and_how_the_curve_is_solved():
    rec = IssuerRecovery("acme", 0.48, None, DAY, "0.3", 0.39, 0.61,
                         {"tag": "M", "asOf": DAY.isoformat(), "sourceName": "Marden recovery model"},
                         {"lines": [{"label": "Recovery", "cents": 48}], "rank": "unsecured"})
    doc = recovery_doc(None, 0.48, "price v0.3", 0.4, rec)
    assert doc["basis"] == recovery_basis(None, rec) == "issuer_waterfall" and doc["cents"] == 48.0
    assert doc["rangeCents"] == [39, 61] and doc["waterfall"] == [{"label": "Recovery", "cents": 48}]
    assert "solved from its bond prices at this same issuer unsecured recovery" in doc["calibration"]
    none = IssuerRecovery("privco", None, "PrivCo has no SEC filings")
    conv = recovery_doc(None, 0.4, "price v0.3", 0.4, none)
    assert conv["method"] == "bond_convention" and conv["why"] == "PrivCo has no SEC filings"
    assert recovery_basis(None, none) == "default_assumption" and none.value(0.4) == 0.4


def _entity_output(cents: int) -> dict[str, Any]:
    return {"issuer": "oracle", "name": "Oracle", "method": "issuer_waterfall", "methodVersion": "recovery v0.3",
            "ranks": {"unsecured": {"cents": cents, "rangeCents": [cents - 7, cents + 8]}},
            "lines": {"unsecured": [{"label": "Recovery", "cents": cents, "op": "equals"}]},
            "filing": {"form": "10-Q", "periodEnd": "2026-08-31"}, "leftUsd": 1.0, "missing": []}


def test_issuer_bonds_price_at_the_issuer_recovery_with_the_curve_solved_at_it(committed):
    copy_price_inputs(["EX-ORCL-239"])
    from marden.models.price import run_day

    with db.transaction() as conn:
        before = run_day(conn, DAY, ["EX-ORCL-239"])
    run_id = runs.start_run("recovery", "0.3", trigger="test")
    runs.write_output(committed, run_id, "recovery", "0.3", "entity", "oracle", DAY, _entity_output(49))
    got = issuer_recovery(committed, "oracle", DAY)
    assert got.base == 0.49 and (got.low, got.high) == (0.42, 0.57)
    with db.transaction() as conn:
        after = run_day(conn, DAY, ["EX-ORCL-239"])
    b, a = before["prices"]["EX-ORCL-239"], after["prices"]["EX-ORCL-239"]
    assert b["recovery"] == 0.4 and b["recoveryInputs"]["method"] == "bond_convention"
    assert a["recovery"] == 0.49 and a["recoveryBasis"] == "issuer_waterfall"
    assert after["curves"]["oracle"].recovery == 0.49 and before["curves"]["oracle"].recovery == 0.4
    assert after["cds"]["oracle"]["recovery"] == {"basis": "issuer_waterfall", "cents": 49.0}
    # solved and priced at one recovery: the bond's model price barely moves, the intensity rises
    assert abs(a["price"] - b["price"]) < 1.5
    assert after["cds"]["oracle"]["hazard"] > before["cds"]["oracle"]["hazard"]
    assert issuer_recovery(committed, "oracle", date(2026, 10, 13)).base is None  # point in time
