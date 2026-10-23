"""Yield model (docs/methods/yield-v0.1.md): yield to maturity, yield to worst and discount margin on tiny
cases, the options read from the term texts, the plain-word n/d reasons, then a run on real rows copied from
the local `marden` database (tests/models/price_rows.py) with its Marden daily prices.

Expectations for the real rows come from the rows themselves: a TRACE print's reported yield (the source's
own number, which our yield to maturity of the same price should match for a bullet bond without calls) and
the Vantage construction loan's stated terms (SOFR + 2.50%, two one-year extensions).
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from price_rows import _copy, copy_price_inputs

from marden.core import db
from marden.models import price_curve, yields
from marden.models.price_bonds import Terms, flows, yield_to_maturity
from marden.models.price_terms import Instrument
from marden.models.yields import Options, cash_flows, discount_margin, options, solve, workouts

FLAT = {k: 4.0 for k in ("1m", "3m", "6m", "1y", "2y", "3y", "5y", "7y", "10y", "20y", "30y")}
DAY = date(2026, 1, 15)
FRONTIER, HUT = "EX-VANTAGE_ORCL-366", "EX-HUT-171"


def _fact(value, tag="M"):
    return {"value": value, "tag": tag, "source_name": "Marden research archive", "source_url": None,
            "valid_from": date(2026, 10, 20), "method_version": None}


def _inst(kind="bond", terms: Terms | None = None, **facts) -> Instrument:
    inst = Instrument("t", "test note", kind, "issuer", "Issuer", None, terms=terms)
    inst.facts = {k: _fact(v) for k, v in facts.items()}
    return inst


# ---------------------------------------------------------------- formulas (tiny cases)
def test_a_par_bond_on_a_coupon_date_yields_its_coupon_and_matches_the_price_engine():
    t = Terms("t", date(2031, 1, 15), "fixed", 6.0)
    # (30/360 coupons discounted on the engine's Act/365.25 time axis: within a fraction of a basis point)
    assert yield_to_maturity(cash_flows(t, DAY, None), 100.0) == pytest.approx(6.0, abs=0.005)
    # Same flows as the price engine's, so a Marden price's YTM is the price output's yieldPct
    curve = price_curve.bootstrap(DAY, FLAT)
    settle = date(2026, 3, 2)
    assert yield_to_maturity(cash_flows(t, settle, None), 93.5) == pytest.approx(
        yield_to_maturity(flows(t, curve, settle, None), 93.5), abs=1e-9)


def test_price_down_yield_up():
    t = Terms("t", date(2031, 1, 15), "fixed", 9.875)
    ys = [yield_to_maturity(cash_flows(t, DAY, None), px) for px in (101.0, 95.0, 90.0)]
    assert ys[0] < 9.875 < ys[1] < ys[2]


def test_a_floater_at_par_on_a_coupon_date_has_a_discount_margin_equal_to_its_margin():
    t = Terms("t", date(2030, 4, 15), "floating", None, 250.0, 4)
    settle = date(2026, 10, 15)
    f = cash_flows(t, settle, 3.88)
    assert discount_margin(t, settle, 3.88, None, f, 100.0) * 1e4 == pytest.approx(250.0, abs=0.5)
    # the current coupon 6.38% Act/360, paid quarterly, is 6.47% on a 365-day year: 6.52% compounded twice a year
    act365 = 0.0638 * 365.25 / 360
    assert yield_to_maturity(f, 100.0) == pytest.approx(200 * ((1 + act365 / 4) ** 2 - 1), abs=0.01)
    below = cash_flows(t, settle, 3.88)
    assert discount_margin(t, settle, 3.88, None, below, 97.3) * 1e4 > 300  # below par: more than the margin


def test_a_stated_sofr_floor_sets_the_coupon_when_sofr_is_below_it():
    t = Terms("t", date(2028, 8, 15), "floating", None, 475.0, 4)
    floored = cash_flows(t, DAY, 2.0, floor_pct=2.5).amounts[0]
    plain = cash_flows(t, DAY, 2.0).amounts[0]
    assert floored > plain and cash_flows(t, DAY, 3.88, floor_pct=2.5).amounts[0] == cash_flows(t, DAY, 3.88).amounts[0]


# ---------------------------------------------------------------- options and yield to worst
def test_extensions_are_read_from_the_coupon_or_the_research_terms_and_set_ytw_below_par():
    a = options(_inst("construction_loan", coupon="~SOFR+2.50% (6.17% at 31 Jul per PGIM N-PORT); two 1-yr extensions"))
    b = options(_inst("construction_loan", research_terms={"terms": "Matures 15 Apr 2030 with two one-year extension "
                                                                     "options. Amortization not disclosed."}))
    assert (a.extensions, a.extension_months, b.extensions, b.extension_months) == (2, 12, 2, 12)
    assert a.reported == "6.17% on Jul 31" and a.prepay_at_par and not b.amortizing
    t = Terms("t", date(2030, 4, 15), "floating", None, 250.0, 4)
    settle = date(2026, 10, 22)
    kinds = {(w.kind, w.day) for w in workouts(t, a, settle)}
    assert {("maturity", date(2030, 4, 15)), ("extended", date(2031, 4, 15)), ("extended", date(2032, 4, 15))} <= kinds
    below = solve(t, a, settle, 3.88, 97.3)
    assert below.ytw is not None and below.ytw.workout.day == date(2032, 4, 15) and below.ytw.workout.kind == "extended"
    assert below.ytw.yield_pct < below.ytm.yield_pct  # a discount accretes over a longer life: lower yield
    above = solve(t, a, settle, 3.88, 101.0)
    assert above.ytw is not None and above.ytw.workout.kind == "prepay"  # prepaid at par on the next interest date
    assert above.ytw.workout.day == date(2027, 1, 15)


def test_a_stated_par_call_counts_a_presumed_one_does_not_and_make_whole_never_does():
    t = Terms("t", date(2042, 11, 15), "fixed", 6.192)
    stated = options(_inst(research_terms={"terms": "Final maturity 15 Nov 2042. Callable at par from 15 May 2042."}))
    assert [(c.day, c.price, c.counted) for c in stated.calls] == [(date(2042, 5, 15), 100.0, True)]
    rich = solve(t, stated, DAY, None, 104.0)
    assert rich.ytw is not None and rich.ytw.workout.kind == "call" and rich.ytw.yield_pct < rich.ytm.yield_pct
    cheap = solve(t, stated, DAY, None, 93.5)
    assert cheap.ytw is not None and cheap.ytw.workout.kind == "maturity"
    presumed = options(_inst(research_terms={"terms": "First call presumed in about Aug 2028 at 104.94 (model "
                                                      "assumption). Make-whole premium for prepayment before 30 "
                                                      "Sep 2031."}))
    assert [c.counted for c in presumed.calls] == [False]
    assert any("not a term from the indenture" in n for n in presumed.notes)
    assert any("make-whole call applies before Sep 30, 2031" in n for n in presumed.notes)
    g = Terms("g", date(2031, 8, 15), "fixed", 9.875)
    s = solve(g, presumed, DAY, None, 110.0)  # far above the presumed call price: still not counted
    assert s.ytw is not None and s.ytw.workout.kind == "maturity" and any(not x.workout.counted for x in s.worked)


def test_no_yield_reasons_are_plain_words():
    book = price_curve.CurveBook(DAY, {}, {})
    inst = _inst("lease")
    inst.terms_missing = ["instrument t: maturity", "instrument t: coupon terms"]
    assert yields.instrument_output(inst, book, DAY, [], None)["why"] == (
        "No yield: the coupon and maturity are not in the sources.")
    perpetual = _inst(terms=Terms("t", date(3999, 12, 31), "fixed", 10.25))
    assert "perpetual" in yields.instrument_output(perpetual, book, DAY, [], None)["why"]
    unpriced = yields.instrument_output(_inst(terms=Terms("t", date(2031, 1, 15), "fixed", 6.0)), book, DAY, [], None)
    assert unpriced["why"] == "No yield: no Marden or observed price yet." and unpriced["assumptions"]


def _trade(day: date, price: float, kind="trade", tag="F", source="FINRA TRACE (finra.org)"):
    return yields.PricePoint(day, price, kind, tag, source, "https://www.finra.org/x", None)


def test_a_convertible_with_no_marden_price_is_worked_from_its_trade_ignoring_the_conversion_option():
    """CoreWeave 2.875% 2033 (no Marden price; last TRACE trade 83.05 on Oct 20): yields on the trade price, tagged
    as the trade (F, FINRA), with the note; a Frankfurt quote is R and says 'quoted price'."""
    book = price_curve.CurveBook(DAY, {}, {})
    t = Terms("c", date(2033, 4, 15), "fixed", 2.875)
    day = date(2026, 10, 20)
    out = yields.instrument_output(_inst("convert", terms=t), book, day, [], _trade(day, 83.05))
    assert out["why"] is None and out["ytw"]["value"] == out["ytm"]["value"]
    assert out["ytw"]["value"] == pytest.approx(6.09, abs=0.05)  # FINRA reports 6.094 on that print
    assert out["ytw"]["tag"] == "F" and out["ytw"]["sourceName"] == "FINRA TRACE (finra.org)"
    assert out["ytw"]["asOf"] == "2026-10-20" and out["ytw"]["methodVersion"] == "yield v0.1"
    assert out["note"] == "Yield on the trade price, ignoring the conversion option."
    assert "ignoring the conversion option" in out["assumptions"][0] and out["basis"]["kind"] == "trade"
    quote = yields.instrument_output(_inst("convert", terms=t), book, day, [],
                                     _trade(day, 83.16, "exchange", "R", "Börse Frankfurt"))
    assert quote["ytw"]["tag"] == "R" and quote["note"] == "Yield on the quoted price, ignoring the conversion option."
    # a straight bond with no Marden price: the market price's tag too, and no convertible note
    bond = yields.instrument_output(_inst(terms=t), book, day, [], _trade(day, 83.05))
    assert bond["ytw"]["tag"] == "F" and bond["note"] is None
    marden = yields.PricePoint(day, 83.05, "marden", "M", "Marden model price", None, "price v0.1")
    assert yields.instrument_output(_inst(terms=t), book, day, [marden], None)["ytw"]["tag"] == "M"


def test_a_stated_holder_put_counts_in_ytw_and_a_fundamental_change_put_is_only_noted():
    t = Terms("c", date(2031, 6, 1), "fixed", 3.0)
    settle = date(2026, 10, 20)
    o = options(_inst("convert", research_terms={"terms": "Holders may require the company to repurchase the notes "
                                                           "at par on 1 Jun 2029. Matures 1 Jun 2031."}))
    assert [(p.kind, p.day, p.price) for p in o.puts] == [("put", date(2029, 6, 1), 100.0)]
    assert {("put", date(2029, 6, 1))} <= {(w.kind, w.day) for w in workouts(t, o, settle)}
    rich = solve(t, o, settle, None, 105.0)  # above par the put is worked and is the worst: an earlier par exit
    assert rich.ytw is not None and rich.ytw.workout.kind == "put" and rich.ytw.yield_pct < rich.ytm.yield_pct
    cheap = solve(t, o, settle, None, 83.0)  # below par: the put yields more, so YTW = YTM
    assert cheap.ytw is not None and cheap.ytw.workout.kind == "maturity"
    assert any(x.workout.kind == "put" and x.yield_pct > cheap.ytm.yield_pct for x in cheap.worked)
    assert options(_inst("convert", coupon="1.75%; putable at 100 in Dec 2028")).puts[0].day == date(2028, 12, 1)
    fc = options(_inst("convert", covenants="Article 4 covenants: payment of principal and interest; holders' "
                                            "repurchase right on a fundamental change (Article 14)"))
    assert not fc.puts and any("fundamental change" in n and "not worked" in n for n in fc.notes)
    book = price_curve.CurveBook(DAY, {}, {})
    inst = _inst("convert", terms=t, research_terms={"terms": "Holders may require repurchase at par on 1 Jun 2029."})
    out = yields.instrument_output(inst, book, settle, [], _trade(settle, 105.0))
    assert out["ytwTo"]["kind"] == "put" and any("Holder put" in a for a in out["assumptions"])
    assert any(i["key"] == "put" for i in out["inputs"])


def test_options_defaults_are_empty():
    assert Options().extensions == 0 and not Options().calls


# ---------------------------------------------------------------- real rows
@pytest.fixture
def real_rows():
    found = copy_price_inputs([FRONTIER, HUT])
    with psycopg.connect(BASE_URL) as src, psycopg.connect(TEST_URL) as dst:  # the model's own daily prices
        _copy(src, dst, "price_obs", "instrument_id = ANY(%(ids)s) AND kind = 'marden'", {"ids": found},
              nulls=("document_id",))
        _copy(src, dst, "fact", "subject_type = 'instrument' AND subject_id = ANY(%(ids)s) AND field = ANY(%(f)s) "
              "AND status IN ('published', 'superseded')", {"ids": found, "f": list(yields.EXTRA_FIELDS)},
              nulls=("document_id",))
        dst.execute("SELECT setval(pg_get_serial_sequence('fact', 'id'), coalesce(max(id), 0) + 1, false) FROM fact")
        n = dst.execute("SELECT count(*) FROM price_obs WHERE kind = 'marden'").fetchone()[0]
    if n == 0:
        pytest.skip("no Marden daily prices in the source database yet (run the price model)")
    return found


def test_real_run_works_every_marden_price_and_matches_trace_where_trace_states_a_yield(real_rows):
    with db.transaction() as conn:
        last = conn.execute("SELECT max(obs_date) d FROM price_obs WHERE kind = 'marden'").fetchone()["d"]
    result = yields.run(last, [FRONTIER, HUT], trigger="test", mark=False)
    loan, note = result["outputs"][FRONTIER], result["outputs"][HUT]
    with db.transaction() as conn:
        days = conn.execute("SELECT count(DISTINCT obs_date) n FROM price_obs WHERE kind = 'marden' "
                            "AND instrument_id = %s", (FRONTIER,)).fetchone()["n"]
        stored = conn.execute("SELECT count(*) n FROM model_output WHERE model = 'yield' AND run_id = %s",
                              (result["run_id"],)).fetchone()["n"]
    assert stored == 2 and len(loan["points"]) == days
    assert loan["why"] is None and loan["ytw"]["tag"] == "M" and loan["ytw"]["methodVersion"] == "yield v0.1"
    assert loan["couponType"] == "floating" and loan["ytwTo"]["kind"] in ("extended", "prepay", "maturity")
    if loan["basis"]["price"] < 100:
        assert loan["ytwTo"]["date"] == "2032-04-15" and loan["ytw"]["value"] < loan["ytm"]["value"]
        assert loan["dmBp"]["value"] > 250
    assert any(i["key"] == "sofrPct" and i["tag"] == "F" for i in loan["inputs"])
    assert any("current coupon" in a for a in loan["assumptions"])
    assert note["couponType"] == "fixed" and any(w["kind"] == "call" for w in note["workouts"])
    obs = note["observed"]
    if obs and obs["reportedYieldPct"] is not None and obs["ytm"] is not None and obs["price"] < 100:
        assert obs["ytm"] == pytest.approx(obs["reportedYieldPct"], abs=0.05)  # FINRA's own yield of that print


def test_the_model_runs_after_the_price_run_and_on_its_inputs():
    from marden.core.queue import app
    from marden.models import triggers

    assert "yield" in triggers.registered_models()
    deps = {(d.subject_type, d.field_prefix) for d in triggers.model_dependencies("yield")}
    assert {("instrument", "@model:price"), ("instrument", "@price"), ("instrument", "coupon"),
            ("series", "value")} <= deps
    assert app.tasks["models.yield"].queue == "models"
    crons = {name: task.cron for (name, _), task in app.periodic_registry.periodic_tasks.items()}
    assert crons["models.yield"] == yields.CRON_UTC == "44 9 * * *"  # after the 09:40 price run
