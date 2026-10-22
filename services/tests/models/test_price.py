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


def _comp(cid: str, **kw: object) -> dict:
    base = {"id": cid, "issue_date": date(2025, 11, 20), "issue_spread_bp": None, "issue_spread_tag": "F",
            "tier": None, "type_code": "dc_project_bond", "site_slug": None, "rate": "", "coupon_pct": None,
            "maturity": None, "tenor_years": None, "benchmark": "UST", "rating": None, "contract": None,
            "name": cid, "issue_price": None, "spread_method": None, "tenant": None, "source_name": "test",
            "source_url": None}
    return {**base, **kw}


def _inst(iid: str, name: str, kind: str, site: str | None, maturity: str, rate: float | None = None,
          margin: float | None = None, tag: str = "F"):
    from marden.models.price_terms import Instrument

    facts = {"maturity": {"value": maturity, "tag": "F"}}
    if rate is not None:
        facts |= {"coupon_rate_pct": {"value": rate, "tag": tag}, "coupon_type": {"value": "fixed", "tag": tag}}
    if margin is not None:
        facts |= {"coupon_spread_bp": {"value": margin, "tag": tag}, "coupon_type": {"value": "floating", "tag": tag}}
    return Instrument(iid, name, kind, None, "", site, facts)


def test_an_issue_comp_links_only_the_deal_it_describes():
    """Coupon, deal type and maturity year alone are not identity: CoreWeave's 9.25% notes due 2030 once took
    Applied Digital's 9.25% 2030 comp (issued 2025-11-20 at 97), and its 9.00% 2031 notes the floating DDTL
    2.1 comp ('... 9% effective'), so their basis carried another deal's issue date and price."""
    from marden.models.price_terms import attach, contradicts

    apld_comp = _comp("issue_comp:APLD_COMPUTECO_2030", site_slug="polaris-forge-1", coupon_pct=9.25,
                      rate="9.25% fixed; issued at 97.0", maturity="2030-12", issue_price=97.0, issue_spread_bp=634.0)
    ddtl_comp = _comp("issue_comp:CRWV_DDTL21", type_code="gpu_loan", benchmark="SOFR", maturity="2031-03",
                      rate="Term SOFR + 4.25% (base + 3.25%); 1.50% upfront fee per draw; 9% effective",
                      issue_date=date(2025, 9, 29), issue_spread_bp=425.0)
    apld = _inst("EX-APLD-010", "9.25% Sr Secured Notes 2030", "bond", "polaris-forge-1", "2030-12", 9.25)
    crwv_2030 = _inst("EX-CRWV-101", "9.25% Sr Notes 2030", "bond", None, "2030-06", 9.25)
    crwv_2031 = _inst("EX-CRWV-102", "9.00% Sr Notes 2031", "bond", None, "2031-02", 9.0)
    ddtl = _inst("EX-CRWV-093", "DDTL 2.1", "ddtl", None, "2031-03", margin=425.0)
    insts = {i.id: i for i in (apld, crwv_2030, crwv_2031, ddtl)}
    assert contradicts(crwv_2030, apld_comp) == "finances site polaris-forge-1"
    assert contradicts(crwv_2031, ddtl_comp) == "floating rate, not a 9% fixed coupon"
    attach(insts, [apld_comp, ddtl_comp])
    assert apld.comp is not None and apld.comp["id"] == apld_comp["id"]
    assert (apld.issue_date, apld.issue_price) == (date(2025, 11, 20), 97.0)
    assert ddtl.comp is not None and ddtl.comp["id"] == ddtl_comp["id"]
    assert crwv_2030.comp is None and crwv_2030.issue_date is None and crwv_2030.terms is not None
    assert crwv_2031.comp is None and crwv_2031.issue_date is None


def test_an_issue_comp_goes_to_one_instrument_at_most():
    """xAI's VCI AH2 comp (7.375% due 2031-02) also fit VCI AH3 (6.875% due 2031-04) on site, deal type and
    maturity year. A sourced 6.875% coupon contradicts it; without one, the better-scored AH2 loan keeps it."""
    from marden.models.price_terms import attach

    comp = _comp("issue_comp:XAI_VCI_AH2_NOTES", type_code="gpu_loan", site_slug="colossus-2", coupon_pct=7.375,
                 rate="7.375% fixed", maturity="2031-02", issue_date=date(2026, 2, 18), issue_spread_bp=372.0)
    ah2 = _inst("EX-XAI_VCI-389", "VCI AH2 1L Term Loan", "term_loan", "colossus-2", "2031-02", 7.375)
    ah3 = _inst("EX-XAI_VCI-390", "VCI AH3 1L Term Loan", "term_loan", "colossus-2", "2031-04", 6.875)
    attach({i.id: i for i in (ah2, ah3)}, [comp])
    assert ah2.comp is not None and ah3.comp is None
    legacy = _inst("EX-XAI_VCI-390", "VCI AH3 1L Term Loan", "term_loan", "colossus-2", "2031-04", 6.875, tag="M")
    attach({i.id: i for i in (ah2, legacy)}, [comp])  # scores 7 on its own: site, deal type, maturity year
    assert ah2.comp is not None and ah2.comp["linkScore"] == 10 and legacy.comp is None
    alone = _inst("EX-XAI_VCI-390", "VCI AH3 1L Term Loan", "term_loan", "colossus-2", "2031-04", 6.875, tag="M")
    attach({alone.id: alone}, [comp])
    assert alone.comp is not None  # with no rival it still links (the legacy coupon is not evidence)
    twin = _inst("EX-XAI_VCI-391", "VCI AH3 1L Term Loan B", "term_loan", "colossus-2", "2031-04", 6.875, tag="M")
    alone2 = _inst("EX-XAI_VCI-390", "VCI AH3 1L Term Loan", "term_loan", "colossus-2", "2031-04", 6.875, tag="M")
    attach({i.id: i for i in (alone2, twin)}, [comp])
    assert alone2.comp is None and twin.comp is None  # a tie links neither


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
    assert jup["methodVersion"] == "price v0.2" and jup["band"]["label"] == "model v0"
    assert jup["price"] is not None and jup["band"]["low"] <= jup["price"] <= jup["band"]["high"]
    assert {"price", "spreadBp", "yieldPct", "recovery", "change1w", "inputs", "band", "missing"} <= set(jup)
    assert any(i["key"] == "tenantCdsBp" for i in jup["inputs"])
    # no recovery output copied: the default 55c, said so, with its price change per 10c of recovery
    assert jup["recoveryBasis"] == "default_assumption" and jup["recovery"] == 0.55
    assert jup["recoveryInputs"]["method"] == "building_default" and jup["recoveryInputs"]["why"]
    assert any(i["key"] == "recoveryCents" and i["value"] == 55.0 for i in jup["inputs"])
    sens = jup["recoverySensitivity"]
    assert sens["per10cPts"] is not None and sens["holdingSpreadPer10cPts"] > 0
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
    assert isinstance(tiers, list) and tiers
    assert all({"tier", "kind", "points", "fit", "note", "source"} <= set(t) for t in tiers)
    assert tiers[0]["tier"] == "U.S. Treasury" and tiers[0]["kind"] == "benchmark" and tiers[0]["source"]["tag"] == "F"
    assert all({"years", "yieldPct", "instrumentId", "spreadBp", "source", "lastObservation", "excluded"} == set(p)
               for t in tiers for p in t["points"])
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


def test_trace_is_collected_every_weekday_evening_and_its_prints_re_run_the_price():
    """The daily price follows the TRACE ingest: the collector runs weekdays at 18:45 ET (after the 18:30 ET
    close), its trade rows mark the price model dirty (the run within a minute re-prices the day), and the
    scheduled 09:40 UTC run reprices the next morning."""
    from marden.collectors import finra_trace
    from marden.core.queue import app

    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.finra_trace"] == finra_trace.CRON_UTC == "45 22,23 * * 1-5"
    assert finra_trace.RUN_AT_ET == (18, 45) and finra_trace.RUN_AT_ET > finra_trace.TRACE_CLOSE_ET
    assert crons["models.price_daily"] == price_run.DAILY_CRON
    assert "models.run_dirty" in crons or any("dirty" in n for n in crons)  # marks are drained every minute
