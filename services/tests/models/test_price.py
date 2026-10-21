"""Price engine: formulas on tiny cases, then the bond-implied CDS, the validation table and full runs on the
real rows copied from the local `marden` database (tests/models/price_rows.py).

Expectations for the real-data tests come from the methodology's own numbers (docs/methods/pricing-v0.1.md:
CoreWeave's 2026-07-31 month-end bond-implied CDS of ~736bp, the leave-one-bond-out error near 0.76pt) and
from the stored rows themselves (an N-PORT mark is not an input before its period + 60 days).
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest
from price_rows import copy_price_inputs

from marden.core import config, db
from marden.models import price_marks, price_run, price_signals, price_validate, triggers
from marden.models.price_bonds import Terms, clean_price, flows, solve_flat_hazard, yield_to_maturity
from marden.models.price_cds import cds_maturity, hazard_for_spread, interpolate, isda
from marden.models.price_chain import Chain, Legs, hazard_fn
from marden.models.price_curve import bootstrap
from marden.models.price_equity import creditgrades_spread
from marden.models.price_kalman import Reading
from marden.models.price_kalman import run as kalman
from marden.models.price_tiers import ns, tier_of

FLAT = {k: 4.0 for k in ("1m", "3m", "6m", "1y", "2y", "3y", "5y", "7y", "10y", "20y", "30y")}
CRWV = ["EX-CRWV-101", "EX-CRWV-102", "EX-CRWV-103", "EX-CRWV-104"]


# ---------------------------------------------------------------- formulas (tiny cases)
def test_par_bond_on_a_flat_curve_prices_at_par_without_default():
    day = date(2026, 1, 15)
    curve = bootstrap(day, FLAT)
    f = flows(Terms("t", date(2031, 1, 15), "fixed", 4.0), curve, day, None)
    assert clean_price(f, curve, 0.0) == pytest.approx(100.0, abs=0.05)
    assert yield_to_maturity(f, 100.0) == pytest.approx(4.0, abs=0.01)


def test_flat_hazard_solve_round_trips_and_floors_rich_prices():
    day = date(2026, 1, 15)
    curve = bootstrap(day, FLAT)
    f = flows(Terms("t", date(2030, 7, 1), "fixed", 9.0), curve, day, None)
    px = clean_price(f, curve, 0.08)
    assert solve_flat_hazard(f, curve, px).value == pytest.approx(0.08, abs=1e-6)
    rich = solve_flat_hazard(f, curve, clean_price(f, curve, 0.0) + 1.0)
    assert rich.value == 0.0 and rich.bound == "floor"


def test_isda_conversion_and_standard_maturity():
    day = date(2026, 10, 15)
    assert cds_maturity(day) == date(2031, 12, 20)
    assert cds_maturity(date(2025, 9, 26)) == date(2030, 12, 20)
    assert cds_maturity(date(2026, 6, 15)) == date(2031, 6, 20)
    curve = bootstrap(day, FLAT)
    q = isda(day, curve, 0.15)
    assert 850 < q.spread_bp < 940 and q.upfront_pct > 0  # ~ (1 - 0.4) x 15%, above the 500bp coupon
    assert hazard_for_spread(day, curve, q.spread_bp) == pytest.approx(0.15, abs=1e-6)
    assert isda(day, curve, 0.02).upfront_pct < 0


def test_interpolation_to_the_cds_maturity():
    assert interpolate([(4.0, 0.10), (6.0, 0.14)], 5.0) == (pytest.approx(0.12), "interpolated")
    assert interpolate([(1.0, 0.10), (2.0, 0.12)], 5.0) == (pytest.approx(0.18), "extrapolated")
    assert interpolate([(3.0, 0.10)], 5.0) == (0.10, "flat (one bond)")
    assert interpolate([(1.0, 0.30), (2.0, 0.10)], 5.0)[0] == 0.0  # floored


def test_creditgrades_spread_falls_as_the_share_price_rises():
    lo, hi = creditgrades_spread(20.0, 30.0, 0.6, 0.04), creditgrades_spread(60.0, 30.0, 0.6, 0.04)
    assert lo > hi > 0


def test_kalman_reads_exact_and_stale_observations():
    exact = kalman(10, 0.0, 1.0, 1e-6, [Reading(5, 2.0, 1e-8)])
    assert exact.at(9)[0] == pytest.approx(2.0, abs=1e-3)
    # A stale (GLM) reading of 2.0 when the state is known to be 0 at day 0 and still: weight 0.6 now.
    stale = kalman(60, 0.0, 1e-8, 1e-8, [Reading(50, 2.0, 1e-4, stale=True)])
    assert stale.at(59)[0] == pytest.approx(0.0, abs=0.02)  # a confident state barely moves
    loose = kalman(60, 0.0, 1.0, 1e-8, [Reading(50, 2.0, 1e-6, stale=True)])
    assert loose.at(59)[0] == pytest.approx(2.0, abs=0.01)


def test_locked_signal_rules():
    assert price_signals.completion_multiplier(1) == 1.75
    assert price_signals.completion_multiplier(4) == 6.0
    assert price_signals.fm_probability(2) == 0.5 and price_signals.fm_probability(9) == 1.0
    sigs = price_signals.read("NM State Land Office denied trust-land ROW twice (Apr, Jul 2026)", date(2026, 10, 17),
                              "x", None, event=False)
    assert [(s.kind, s.count) for s in sigs] == [("permit_denial", 2)]
    procedural = price_signals.read("Order Denying Motion for Reconsideration", date(2026, 8, 14), "x", None,
                                    event=True)
    assert procedural == []
    slip = price_signals.read("in-service slipped from Sep/Aug-2026 to 2027-02-01.", date(2026, 10, 17), "x", None,
                              event=False)
    assert slip and slip[0].kind == "power_delay" and slip[0].months >= 3
    assert price_signals.counts(price_signals.JUPITER_LOCKED, date(2026, 8, 17)) == (2, 1)
    assert price_signals.counts(price_signals.JUPITER_LOCKED, date(2026, 9, 24)) == (2, 2)


def test_chain_intensity_build_up():
    day = date(2026, 1, 1)
    ch = Chain("x", tenant="t", completion=date(2028, 1, 1))
    hz = hazard_fn(ch, Legs(0.02, None, None, 1, 1), day)
    before, after = hz(np.array([1.0]))[0], hz(np.array([3.0]))[0]
    assert before == pytest.approx(0.5 * 0.02 + 0.015 * 1.75 + 0.15 * 0.25)
    assert after == pytest.approx(0.5 * 0.02)
    backed = hazard_fn(Chain("x", tenant="t"), Legs(0.05, None, 0.01, 0, 0), day)
    assert backed(np.array([2.0]))[0] < 0.01  # joint default with the guarantor is rarer than either


def test_tier_rules_and_nelson_siegel_loading():
    assert tier_of("dc_construction_loan", "orcl", None) == "Construction loan"
    assert tier_of("dc_project_bond", "aa", "A (S&P)") == "IG project bond"
    assert tier_of("dc_project_bond", "back", "BBB (S&P)") == "Backstopped senior"
    assert tier_of("dc_project_bond", "lab", "BB (S&P)") == "Neocloud bond"
    assert tier_of("gpu_loan", "ig", None) == "Contract-backed loan"
    assert tier_of("gpu_loan", "nd", None) == "Junior chip paper"
    assert tier_of("dc_abs", "ig", "A") is None
    assert ns(1e-9) == pytest.approx(1.0) and ns(30.0) < 0.11


# ---------------------------------------------------------------- real rows
@pytest.fixture
def crwv_rows():
    return copy_price_inputs(CRWV)


def test_coreweave_month_end_bond_implied_cds_matches_the_methodology(crwv_rows):
    with db.transaction() as conn:
        rep = price_validate.crwv_cds(conn)
        lobo = price_validate.crwv_lobo(conn)
    by_date = {r["date"]: r for r in rep["rows"]}
    jul = by_date["2026-07-29"]
    assert jul["bondDate"] == "2026-07-31" and jul["bonds"] == 4
    assert jul["mardenBp"] == pytest.approx(736, abs=15)  # methodology: 736
    assert by_date["2026-06-15"]["mardenBp"] == pytest.approx(436, abs=15)  # methodology: 436
    assert lobo["n"] >= 10 and lobo["maePts"] < 1.2


def test_nport_marks_wait_sixty_days(crwv_rows):
    with db.transaction() as conn:
        before = price_marks.load(conn, date(2026, 9, 28), CRWV)
        after = price_marks.load(conn, date(2026, 9, 29), CRWV)
    def july(obs):
        return [o for v in obs.values() for o in v if o.source == "nport" and o.day == date(2026, 7, 31)]
    assert not july(before) and len(july(after)) == 4
    assert all(o.holders >= 3 for o in july(after))


def test_unlicensed_prices_are_dropped_from_inputs(crwv_rows, monkeypatch):
    with db.transaction() as conn:
        shown = price_marks.load(conn, date(2026, 10, 21), CRWV)
    monkeypatch.setenv("MARDEN_SHOW_UNLICENSED_PRICES", "false")
    config.reload_settings()
    try:
        with db.transaction() as conn:
            hidden = price_marks.load(conn, date(2026, 10, 21), CRWV)
    finally:
        monkeypatch.delenv("MARDEN_SHOW_UNLICENSED_PRICES")
        config.reload_settings()
    sources = {o.source for v in shown.values() for o in v}
    if "trace" not in sources:
        pytest.skip("no TRACE rows loaded for CoreWeave")
    assert "trace" not in {o.source for v in hidden.values() for o in v}
    assert "nport" in {o.source for v in hidden.values() for o in v}


def test_full_run_writes_outputs_marks_views_and_does_not_retrigger_itself():
    copy_price_inputs(["EX-JUPITER-190", "EX-CRWV-103", "EX-CRWV-096"])
    day = date(2026, 10, 14)
    result = price_run.run(day, trigger="test")
    assert {"coreweave", "oracle"} <= set(result["cds"])
    crwv = result["cds"]["coreweave"]
    assert crwv["label"] == "Marden bond-implied CDS" and 600 < crwv["spreadBp"] < 1600
    jup = result["prices"]["EX-JUPITER-190"]
    assert jup["methodVersion"] == "price v0.1" and jup["band"]["label"] == "model v0"
    assert jup["price"] is not None and jup["band"]["low"] <= jup["price"] <= jup["band"]["high"]
    assert {"price", "spreadBp", "yieldPct", "recovery", "change1w", "inputs", "band", "missing"} <= set(jup)
    assert any(i["key"] == "tenantCdsBp" for i in jup["inputs"])
    with db.transaction() as conn:
        outs = conn.execute("SELECT model, subject_type, count(*) n FROM model_output WHERE run_id = %s "
                            "GROUP BY 1, 2", (result["run_id"],)).fetchall()
        got = {(r["model"], r["subject_type"]): r["n"] for r in outs}
        marden = conn.execute("SELECT count(*) n FROM price_obs WHERE kind = 'marden' AND obs_date = %s", (day,))
        dirty = conn.execute("SELECT count(*) n FROM model_dirty WHERE model = 'price'").fetchone()["n"]
        views = {r["view"] for r in conn.execute("SELECT view FROM read_model_dirty").fetchall()}
        curve = conn.execute("SELECT output FROM model_output WHERE model = 'curve' AND run_id = %s",
                             (result["run_id"],)).fetchone()
        n_marden = marden.fetchone()["n"]
    assert got[("price", "instrument")] >= 3 and got[("cds", "entity")] >= 2 and got[("curve", "series")] == 1
    assert got[("curve_fit", "series")] == 1
    assert n_marden >= 3 and dirty == 0  # the model's own prices never mark it dirty
    assert {"instrument_detail", "instruments", "curve"} <= views
    tiers = curve["output"]
    assert isinstance(tiers, list) and tiers and all(set(t) == {"tier", "points", "fit"} for t in tiers)
    assert all({"years", "yieldPct", "instrumentId"} == set(p) for t in tiers for p in t["points"])
    again = price_run.run(day, trigger="test")  # a rerun replaces the day's own marden rows
    with db.transaction() as conn:
        n_again = conn.execute("SELECT count(*) n FROM price_obs WHERE kind = 'marden' AND obs_date = %s",
                               (day,)).fetchone()["n"]
    assert n_again == n_marden and again["prices"]["EX-JUPITER-190"]["price"] == jup["price"]


def test_an_observed_print_marks_the_model_dirty():
    copy_price_inputs(["EX-CRWV-103"])
    from marden.core.records import PriceWriter

    with db.transaction() as conn:
        PriceWriter(conn).add("EX-CRWV-103", date(2026, 10, 20), "dealer", 81.0, tag="R", source_name="Test desk")
        marks = triggers.dirty_marks(conn, "price")
    assert [k for k, _ in marks] == ["*"]


def test_backtest_before_the_tenant_curve_exists_reports_it_missing():
    copy_price_inputs(["EX-JUPITER-190"])
    with db.transaction() as conn:
        from marden.models.price import run_day

        res = run_day(conn, date(2025, 11, 14), ["EX-JUPITER-190"])
    out = res["prices"]["EX-JUPITER-190"]
    assert any("tenant oracle" in m for m in out["missing"])
    assert out["price"] is None or not math.isnan(out["price"])


def test_an_instrument_is_not_priced_before_its_issue_date():
    copy_price_inputs(["EX-CRWV-097"])  # CoreWeave DDTL 5.5, issued 2026-08-07 (issue comp CRWV_DDTL55)
    from marden.models.price import run_day

    with db.transaction() as conn:
        before = run_day(conn, date(2026, 7, 1), ["EX-CRWV-097"])["prices"]["EX-CRWV-097"]
        after = run_day(conn, date(2026, 10, 14), ["EX-CRWV-097"])["prices"]["EX-CRWV-097"]
    assert before["price"] is None and any("issued 2026-08-07" in m for m in before["missing"])
    assert after["price"] is not None and after["basis"]["calibration"]["comp"] == "issue_comp:CRWV_DDTL55"


def test_a_print_outside_the_engine_universe_does_not_mark_it():
    copy_price_inputs(["EX-CRWV-103"])
    from marden.core.records import PriceWriter

    with db.transaction() as conn:
        conn.execute("INSERT INTO instrument (id, name, kind, issuer_name) VALUES ('T-OTHER', 'x', 'bond', 'x')")
        PriceWriter(conn).add("T-OTHER", date(2026, 10, 20), "dealer", 99.0, tag="R", source_name="Test desk")
        assert triggers.dirty_marks(conn, "price") == []
