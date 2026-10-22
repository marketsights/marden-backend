"""Status rule L2 reads the Marden price rate-neutral (status v0.2, marden.models.status_credit).

A fixed-rate instrument whose price fell only because Treasury yields rose keeps its spread, so L2 does
not fire; one whose spread widened enough to cost 5+ points on its own does; a floating-rate loan (no rate
duration) trips L2 on its price below 95. The price outputs are built with the price model's own bond math
(price_bonds) on flat Treasury par curves, in the shape `price_facility.price` writes.
"""

from __future__ import annotations

from datetime import date

import pytest

from marden.models import runs, status_rules
from marden.models.price_bonds import Terms, clean_at_yield, flows, g_spread_bp
from marden.models.price_curve import TENORS, CurveBook
from marden.models.status_credit import credit_test
from marden.models.status_inputs import Inputs, Party, Price, SiteInputs, prices_from_outputs

ISSUE, TODAY = date(2026, 1, 15), date(2026, 10, 15)
TERMS = Terms("FIX-2041", date(2041, 1, 15), "fixed", 6.0, None, 2)


def _flat(pct: float) -> dict[str, float]:
    return {tenor: pct for tenor in TENORS}


def _book(issue_rate: float, today_rate: float) -> CurveBook:
    return CurveBook(TODAY, {ISSUE: _flat(issue_rate), TODAY: _flat(today_rate)}, {})


def _fixed_output(book: CurveBook, spread_bp: float, terms: Terms = TERMS, issue: date = ISSUE) -> dict:
    """A price output of a fixed-rate bond issued at par on `issue`, priced today at `spread_bp` over the
    Treasury par yield at its maturity (what price_facility writes: price, spreadBp, basis.terms ...)."""
    curve = book.curve(TODAY)
    f = flows(terms, curve, TODAY, None)
    px = clean_at_yield(f, curve.par_yield(f.maturity_years) + spread_bp / 100.0)
    return {"price": round(px, 3), "spreadBp": round(g_spread_bp(f, curve, px), 1), "yieldPct": None,
            "mardenPrice": {"value": round(px, 3), "tag": "M", "asOf": TODAY.isoformat(),
                            "sourceName": "Marden model price", "methodVersion": "price v0.1"},
            "basis": {"mode": "facility", "terms": {
                "maturity": terms.maturity.isoformat(), "couponType": "fixed", "couponPct": terms.coupon_pct,
                "marginBp": None, "frequency": 2, "issueDate": issue.isoformat(), "issuePrice": 100.0}}}


def _floating_output(px: float) -> dict:
    return {"price": px, "spreadBp": 610.0, "mardenPrice": {"value": px, "tag": "M", "asOf": TODAY.isoformat(),
                                                            "sourceName": "Marden model price",
                                                            "methodVersion": "price v0.1"},
            "basis": {"mode": "facility", "terms": {"maturity": "2029-11-15", "couponType": "floating",
                                                    "couponPct": None, "marginBp": 250.0, "frequency": 4,
                                                    "issueDate": "2025-11-15", "issuePrice": 100.0}}}


def _price(iid: str, output: dict, book: CurveBook) -> Price:
    p = Price(iid, output["price"], TODAY, "price v0.1", None, output["basis"]["terms"]["couponType"])
    if p.coupon_type == "fixed":
        p.credit, p.credit_missing = credit_test(iid, output, TODAY, book, None)
    return p


def _lenders(*prices: Price) -> tuple[dict, str, list[str]]:
    site = SiteInputs("test-site", "Test Site", ["Test"], {"lenders": Party("lenders", "Bank", None, ["Bank"])},
                      [p.instrument_id for p in prices], prices=list(prices))
    out = status_rules.evaluate(site, Inputs(TODAY, {site.slug: site}, {}, 0, 0), {})
    return next(link for link in out["chain"] if link["role"] == "lenders"), out["status"], out["missing"]


def test_a_rate_rise_with_an_unchanged_spread_does_not_fire_l2():
    book = _book(4.50, 5.15)  # Treasuries +65bp; the spread stays 150bp
    output = _fixed_output(book, 150.0)
    assert 93.0 < output["price"] <= 94.5  # the price fell ~6 points on rates alone
    p = _price("FIX-2041", output, book)
    assert p.credit is not None and p.credit.ref.basis == "issue"
    assert p.credit.ref.spread_bp == pytest.approx(150.0, abs=0.5)  # par at 6.0% on a flat 4.5% curve
    assert abs(p.credit.loss) < 0.5 and p.credit.ref_price == pytest.approx(100.0, abs=0.5)
    link, status, _ = _lenders(p)
    assert link["status"] != "red" and link.get("ruleCode") != "L2" and status != "Broken"
    assert "spread-only loss" in link["reason"]
    # The old rule (raw price below 95) would have broken this chain.
    assert output["price"] < status_rules.PRICE_RED_BELOW


def test_a_spread_widening_worth_5_points_fires_l2():
    book = _book(4.50, 4.50)  # rates unchanged
    wide = _price("FIX-2041", _fixed_output(book, 215.0), book)  # +65bp x spread duration ~9.6
    assert wide.credit is not None and wide.credit.loss >= 5.0
    assert wide.credit.loss == pytest.approx(0.65 * wide.credit.spread_duration, rel=0.08)  # full revaluation
    link, status, _ = _lenders(wide)
    assert (link["status"], link["ruleCode"], status) == ("red", "L2", "Broken")
    assert link["reason"].startswith(f"L2 {status_rules.L2_SPREAD}")
    assert link["fact"]["tag"] == "M" and "status v0.2" in link["fact"]["methodVersion"]
    events_ok(link)

    # A smaller widening (+30bp, ~3 points) on top of a rate rise: the price is below 95, L2 does not fire.
    book2 = _book(4.50, 5.00)
    mild = _price("FIX-2041", _fixed_output(book2, 180.0), book2)
    assert mild.value < 95.0 and 2.0 < mild.credit.loss < 5.0
    assert _lenders(mild)[0]["status"] != "red"


def test_a_floating_loan_below_95_fires_l2_on_its_price():
    book = _book(4.50, 5.15)
    loan = _price("FLT-2029", _floating_output(93.2), book)
    assert loan.credit is None and loan.coupon_type == "floating"
    link, status, missing = _lenders(loan)
    assert (link["status"], link["ruleCode"], status) == ("red", "L2", "Broken")
    assert link["reason"].startswith(f"L2 {status_rules.L2_PRICE}") and "floating rate" in link["reason"]
    assert not any("rate-neutral" in m for m in missing)
    above = _price("FLT-2029", _floating_output(96.0), book)  # L1 unchanged: price 95-98
    assert _lenders(above)[0]["ruleCode"] == "L1"
    # With a rate-only fixed bond on the same site, the floating loan still sets the link.
    fixed = _price("FIX-2041", _fixed_output(book, 150.0), book)
    assert _lenders(fixed, loan)[0]["ruleCode"] == "L2"


def test_without_an_issue_date_the_first_marden_price_is_the_reference(committed):
    """`prices_from_outputs` reads the stored price outputs: a fixed bond with no issue comp is measured
    from its first stored Marden price with a spread."""
    book = _book(4.50, 4.50)
    first_day = date(2026, 7, 1)
    first = _fixed_output(book, 200.0)
    run_id = runs.start_run("price", "price v0.1", trigger="test")
    runs.write_output(committed, run_id, "price", "price v0.1", "instrument", "FIX-NOISSUE", first_day,
                      {**first, "basis": {"terms": {**first["basis"]["terms"], "issueDate": None,
                                                    "issuePrice": None}}})
    today = _fixed_output(book, 275.0)
    today["basis"]["terms"].update(issueDate=None, issuePrice=None)
    priced = prices_from_outputs(committed, TODAY, {"FIX-NOISSUE": (TODAY, today, "price v0.1")},
                                 CurveBook(TODAY, {first_day: _flat(4.5), TODAY: _flat(4.5)}, {}))
    p = priced["FIX-NOISSUE"]
    assert p.credit is not None and (p.credit.ref.basis, p.credit.ref.day) == ("first Marden price", first_day)
    assert p.credit.ref.spread_bp == pytest.approx(200.0, abs=0.5) and p.credit.loss > 5.0
    # Before its first stored price (a backtest on an earlier day) there is no reference: L2 says so.
    early = prices_from_outputs(committed, date(2026, 6, 30), {"FIX-NOISSUE": (date(2026, 6, 30), today, None)},
                                CurveBook(TODAY, {first_day: _flat(4.5), TODAY: _flat(4.5)}, {}))["FIX-NOISSUE"]
    assert early.credit is None and "first Marden price" in (early.credit_missing or "")
    link, _, missing = _lenders(early)
    assert link["status"] != "red" and any("rate-neutral L2 test" in m for m in missing)


def events_ok(link: dict) -> None:
    from marden.core import events

    events.validate({"type": "status", "siteSlug": "test-site", "chain": [link]})
