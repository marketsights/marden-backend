"""The Instruments curve card's fitted curves (marden.models.price_curve_card; spec 06 "Curve fitting"): a robust
fit of each tier's spreads over the Treasury curve, outliers flagged and left out, a slope only where the dots
pin one down, no line for a tier with too few dots, and the Treasury curve as the benchmark."""

from __future__ import annotations

from datetime import date

import pytest

from marden.models import price_curve_card as C
from marden.models.price_curve import bootstrap
from marden.models.price_marks import Obs

DAY = date(2026, 10, 22)
UPWARD = {"1m": 4.1, "3m": 4.26, "6m": 4.36, "1y": 4.51, "2y": 4.79, "3y": 4.86, "5y": 4.94, "7y": 5.02,
          "10y": 5.12, "20y": 5.47, "30y": 5.42}
CURVE = bootstrap(DAY, UPWARD)


def dots(tier: str, rows: list[tuple[float, float]], tier_priced: frozenset[int] = frozenset()) -> list[C.Dot]:
    return [C.Dot(f"{tier[:3]}-{i}", tier, years, CURVE.par_yield(years) + bp / 100.0, bp, i in tier_priced)
            for i, (years, bp) in enumerate(rows)]


def test_one_far_dot_does_not_pull_the_curve_and_is_flagged_as_an_outlier():
    rows = [(4.0, 300), (4.3, 320), (4.6, 310), (5.0, 290), (5.2, 305), (4.8, 1400)]
    tc = C.fit_tier("Neocloud bond", dots("Neocloud bond", rows))
    assert tc.fitted and tc.slope_bp is None  # 4.0-5.2y: too narrow a range for a slope
    assert tc.level_bp == pytest.approx(305, abs=8)  # the plain mean would be 488bp
    assert list(tc.excluded) == ["Neo-5"] and "outlier" in tc.excluded["Neo-5"]
    assert "Neo-5" not in tc.used and len(tc.used) == 5
    assert "too narrow a range" in (tc.note or "")


def test_a_wide_dot_within_three_robust_sds_stays_in_the_fit():
    rows = [(4.0, 300), (4.3, 420), (4.6, 250), (5.0, 380), (5.2, 520)]
    tc = C.fit_tier("Neocloud bond", dots("Neocloud bond", rows))
    assert tc.excluded == {} and len(tc.used) == 5


def test_no_outliers_are_flagged_with_fewer_than_five_dots():
    tc = C.fit_tier("IG project bond", dots("IG project bond", [(16, 160), (18, 380), (22, 220), (22.5, 190)]))
    assert tc.excluded == {} and tc.fitted


def test_a_tier_with_fewer_than_three_dots_gets_no_curve_and_says_why():
    tc = C.fit_tier("Junior chip paper", dots("Junior chip paper", [(5.15, 232), (5.15, 539)]))
    assert not tc.fitted and tc.spread(5) is None
    assert tc.note == "2 priced instruments: a curve needs at least 3, so only the dots are shown"


def test_the_slope_is_fitted_only_with_three_dots_at_each_end():
    level, slope = 250.0, 300.0
    years = [1.0, 1.3, 1.6, 6.0, 8.0, 10.0]
    rows = [(t, level + slope * C.ns(t)) for t in years]
    tc = C.fit_tier("Contract-backed loan", dots("Contract-backed loan", rows))
    assert tc.slope_bp == pytest.approx(slope, abs=1) and tc.level_bp == pytest.approx(level, abs=1)
    assert tc.note is None
    one_long = [(1.0, 300.0), (1.3, 320.0), (1.6, 290.0), (4.0, 310.0), (16.0, 280.0)]  # one long dot would set it
    flat = C.fit_tier("Backstopped senior", dots("Backstopped senior", one_long))
    assert flat.slope_bp is None and "too few dots at each end" in (flat.note or "") and flat.excluded == {}
    short = [(3.8, 880.0), (3.8, 560.0), (4.0, 235.0), (4.1, 365.0), (5.1, 220.0), (5.2, 180.0), (6.3, 230.0)]
    narrow = C.fit_tier("Contract-backed loan", dots("Contract-backed loan", short))  # 3.8-6.3y: under a doubling
    assert narrow.slope_bp is None and "too narrow a range" in (narrow.note or "")


def test_a_dot_priced_off_the_pricing_tier_curve_is_drawn_but_not_fitted():
    rows = [(3, 400), (4, 410), (5, 420), (6, 900)]
    tc = C.fit_tier("Construction loan", dots("Construction loan", rows, tier_priced=frozenset({3})))
    assert "Con-3" in tc.excluded and "pricing tier curve" in tc.excluded["Con-3"]
    assert tc.level_bp == pytest.approx(410, abs=6)


def test_the_tier_curve_is_the_treasury_curve_plus_the_fitted_spread_over_the_dots_and_a_short_dash():
    tc = C.fit_tier("Neocloud bond", dots("Neocloud bond", [(4.0, 500), (4.4, 520), (4.8, 510)]))
    doc = C.tier_doc(tc, CURVE, DAY, "price v0.1", "docs/methods/pricing-v0.1.md", {})
    assert doc["range"] == [4.0, 4.8]
    assert doc["fit"][0]["years"] == pytest.approx(4.0 / C.EXTEND, abs=1e-3)
    assert doc["fit"][-1]["years"] == pytest.approx(4.8 * C.EXTEND, abs=1e-3)
    for p in doc["fit"]:
        assert p["yieldPct"] == pytest.approx(CURVE.par_yield(p["years"]) + p["spreadBp"] / 100, abs=2e-3)
    assert {p["spreadBp"] for p in doc["fit"]} == {round(tc.level_bp or 0, 1)}  # flat spread: Treasury-shaped curve
    point = doc["points"][0]
    assert point["source"] == {"tag": "M", "asOf": "2026-10-22", "sourceName": "Marden model price",
                               "sourceUrl": "docs/methods/pricing-v0.1.md", "methodVersion": "price v0.1"}
    assert point["excluded"] is None and point["lastObservation"] is None
    assert doc["source"]["tag"] == "M" and doc["source"]["methodVersion"] == "price v0.1"


def test_the_treasury_benchmark_is_the_run_days_par_curve_tagged_filed():
    doc = C.benchmark_doc(CURVE)
    assert doc["kind"] == "benchmark" and doc["points"] == []
    assert doc["source"]["tag"] == "F" and doc["source"]["asOf"] == "2026-10-22"
    by_years = {p["years"]: p["yieldPct"] for p in doc["fit"]}
    assert by_years[5.0] == 4.94 and by_years[30.0] == 5.42 and min(by_years) == 0.25


def test_card_reads_the_run_prices_and_tags_each_dots_last_observation():
    prices = {
        "a": {"tier": "Neocloud bond", "yieldPct": 9.5, "basis": {"mode": "facility"}},
        "b": {"tier": "Neocloud bond", "yieldPct": 9.9, "basis": {"mode": "facility"}},
        "c": {"tier": "Neocloud bond", "yieldPct": 10.1, "basis": {"mode": "facility"}},
        "issuer": {"tier": None, "yieldPct": 7.0, "basis": {"mode": "issuer bond"}},
    }
    years = {"a": 4.0, "b": 4.5, "c": 5.0, "issuer": 6.0}
    obs = {"a": [Obs("a", date(2026, 10, 20), date(2026, 10, 20), 95.15, "trace", "FINRA TRACE", None),
                 Obs("a", date(2026, 6, 30), date(2026, 8, 29), 97.0, "nport", "N-PORT median of 3 funds", None)],
           "b": [Obs("b", date(2026, 9, 18), date(2026, 9, 18), 90.0, "dealer", "Desk", "https://x")]}
    docs, method = C.card(prices, years, obs, CURVE, DAY, "price v0.1", "m")
    assert [d["tier"] for d in docs] == ["U.S. Treasury", "Neocloud bond"]
    pts = {p["instrumentId"]: p for p in docs[1]["points"]}
    assert pts["a"]["lastObservation"] == {"date": "2026-10-20", "price": 95.15, "tag": "F", "kind": "TRACE print",
                                           "sourceName": "FINRA TRACE"}
    assert pts["b"]["lastObservation"]["tag"] == "R" and pts["c"]["lastObservation"] is None
    assert pts["a"]["spreadBp"] == pytest.approx((9.5 - CURVE.par_yield(4.0)) * 100, abs=0.1)
    assert method["tiers"]["Neocloud bond"]["used"] == 3 and method["minPoints"] == 3


def test_a_slope_that_would_take_the_curve_below_treasuries_is_not_used():
    rows = [(1.0, 900.0), (1.2, 860.0), (1.4, 820.0), (3.0, 60.0), (3.5, 40.0), (4.0, 20.0)]
    tc = C.fit_tier("Construction loan", dots("Construction loan", rows))
    assert tc.slope_bp is None and tc.note == "Spread held flat: a fitted slope would take the curve below Treasuries"
