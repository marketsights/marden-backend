"""Status rules L1 and L2 read prices rate-neutral (L2 from status v0.2, L1 from v0.3,
marden.models.status_credit), and from v0.5 only market evidence (a trade, an exchange or dealer quote, a
fund mark), never the Marden model price on its own.

A fixed-rate instrument whose price fell only because Treasury yields rose keeps its spread, so neither L1
nor L2 fires; one whose spread widened enough to cost 5+ points on its own trips L2, 2 up to 5 points L1; a
floating-rate loan (no rate duration) trips L2 on its price below 95 and L1 at 95-98. The price outputs are
built with the price model's own bond math (price_bonds) on flat Treasury par curves, in the shape
`price_facility.price` writes; unless a test says otherwise each comes with a TRACE print at the same price
on the same day, the market evidence the rules read.
"""

from __future__ import annotations

from datetime import date

import pytest

from marden.models import runs, status_rules
from marden.models.price_bonds import Terms, clean_at_yield, flows, g_spread_bp
from marden.models.price_curve import TENORS, CurveBook
from marden.models.status_credit import credit_test, quote_test
from marden.models.status_inputs import Inputs, Party, Price, Quote, SiteInputs, market_quotes, prices_from_outputs

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


def _trace(iid: str, px: float, day: date = TODAY) -> Quote:
    return Quote(iid, day, px, "trace", "FINRA TRACE (finra.org)", None, "F")


def _price(iid: str, output: dict, book: CurveBook, market: float | str | None = "same") -> Price:
    """A status price input; `market`: the TRACE price on file ('same': the output's price; None: no quote)."""
    p = Price(iid, output["price"], TODAY, "price v0.1", None, output["basis"]["terms"]["couponType"])
    quote = output["price"] if market == "same" else market
    if quote is not None:
        p.market = _trace(iid, float(quote))
    if p.coupon_type == "fixed":
        p.credit, p.credit_missing = credit_test(iid, output, TODAY, book, None)
        if p.market is not None:
            p.market_credit, p.market_missing = quote_test(iid, output, TODAY, p.market.price, book, None)
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
    assert p.market_credit is not None and abs(p.market_credit.loss) < 0.5  # the TRACE print reads the same
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
    assert link["reason"].startswith(f"L2 {status_rules.L2_SPREAD}") and "TRACE trade" in link["reason"]
    assert link["fact"]["tag"] == "M" and status_rules.METHOD_VERSION in link["fact"]["methodVersion"]
    assert "FINRA TRACE" in link["fact"]["sourceName"]
    events_ok(link)

    # A smaller widening (+30bp, ~3 points) on top of a rate rise: the price is below 95, L2 does not fire;
    # the spread move alone is L1's 2-5 points.
    book2 = _book(4.50, 5.00)
    mild = _price("FIX-2041", _fixed_output(book2, 180.0), book2)
    assert mild.value < 95.0 and 2.0 < mild.credit.loss < 5.0
    assert _lenders(mild)[0]["status"] == "amber" and _lenders(mild)[0]["ruleCode"] == "L1"


def test_a_fixed_bond_down_3_points_on_rates_only_is_green_on_l1():
    book = _book(4.50, 4.82)  # Treasuries +32bp; the spread stays 150bp
    output = _fixed_output(book, 150.0)
    assert 96.5 < output["price"] < 97.5  # 95-98: the v0.2 L1 (raw price) would have tripped amber
    p = _price("FIX-2041", output, book)
    assert p.credit is not None and abs(p.credit.loss) < 0.5
    assert status_rules.price_rule(p) is None
    link, status, _ = _lenders(p)
    assert (link["status"], link.get("ruleCode"), status) == ("green", None, "Holding")
    assert "spread-only loss" in link["reason"]


def test_a_fixed_bond_with_3_points_of_spread_loss_is_amber_on_l1():
    book = _book(4.50, 4.50)  # rates unchanged; the spread widens 150 -> 181bp
    p = _price("FIX-2041", _fixed_output(book, 181.0), book)
    assert p.credit is not None and 2.5 < p.credit.loss < 3.5 and 96.5 < p.value < 97.5
    link, status, _ = _lenders(p)
    assert (link["status"], link["ruleCode"], status) == ("amber", "L1", "Strained")
    assert link["reason"].startswith(f"L1 {status_rules.L1_SPREAD}") and "points on spread alone" in link["reason"]
    assert link["fact"]["tag"] == "M" and status_rules.METHOD_VERSION in link["fact"]["methodVersion"]
    events_ok(link)
    # Beside a bond that fell 3 points on rates only, the spread loss still sets the link.
    rates = _book(4.50, 4.82)
    rate_only = _price("FIX-2041R", _fixed_output(rates, 150.0), rates)
    assert _lenders(rate_only, p)[0]["reason"].startswith("L1 Market spread loss") and \
        "FIX-2041:" in _lenders(rate_only, p)[0]["reason"]
    # Under 2 points (+15bp, ~1.4 points) is no rule.
    small = _price("FIX-2041", _fixed_output(book, 165.0), book)
    assert 1.0 < small.credit.loss < 2.0 and _lenders(small)[0]["status"] == "green"


def test_a_floating_loan_below_95_fires_l2_on_its_price():
    book = _book(4.50, 5.15)
    loan = _price("FLT-2029", _floating_output(93.2), book)
    assert loan.credit is None and loan.coupon_type == "floating"
    link, status, missing = _lenders(loan)
    assert (link["status"], link["ruleCode"], status) == ("red", "L2", "Broken")
    assert link["reason"].startswith(f"L2 {status_rules.L2_PRICE}") and "floating rate" in link["reason"]
    assert link["fact"]["tag"] == "F" and link["fact"]["sourceName"] == "FINRA TRACE (finra.org)"
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
    assert p.market is None  # no trade, quote or fund mark on file: no price test, whatever the model says
    assert p.credit is not None and (p.credit.ref.basis, p.credit.ref.day) == ("first Marden price", first_day)
    assert p.credit.ref.spread_bp == pytest.approx(200.0, abs=0.5) and p.credit.loss > 5.0
    # Before its first stored price (a backtest on an earlier day) there is no reference: L2 says so.
    early = prices_from_outputs(committed, date(2026, 6, 30), {"FIX-NOISSUE": (date(2026, 6, 30), today, None)},
                                CurveBook(TODAY, {first_day: _flat(4.5), TODAY: _flat(4.5)}, {}))["FIX-NOISSUE"]
    assert early.credit is None and "first Marden price" in (early.credit_missing or "")
    early.market = _trace("FIX-NOISSUE", 90.0, date(2026, 6, 30))
    early.market_credit, early.market_missing = quote_test(
        "FIX-NOISSUE", today, date(2026, 6, 30), 90.0, CurveBook(TODAY, {date(2026, 6, 30): _flat(4.5)}, {}), None)
    link, _, missing = _lenders(early)
    assert link["status"] == "green" and any("rate-neutral L1 / L2 test" in m for m in missing)


# ---------------------------------------------------------------- v0.5: market evidence only
def test_the_marden_model_price_alone_does_not_trip_l2():
    """Project Jupiter's construction loans: the model marks the loan at 83.3 and no trade, quote or fund
    mark is on file. The model's own mark is not evidence that lenders are in trouble: no L2, a note."""
    book = _book(4.50, 4.50)
    loan = _price("EX-JUPITER-190", _floating_output(83.3), book, market=None)
    assert status_rules.price_rule(loan) is None and status_rules.model_rule(loan) == "L2"
    link, status, missing = _lenders(loan)
    assert (link["status"], link.get("ruleCode"), status) == ("green", None, "Holding")
    assert "Marden model 83.3 (EX-JUPITER-190), no market quote; not used for status" in link["reason"]
    assert any("market price of EX-JUPITER-190" in m for m in missing)
    # A fixed bond whose model spread alone lost 5+ points: no quote, no rule either.
    wide = _price("FIX-2041", _fixed_output(book, 215.0), book, market=None)
    assert wide.credit is not None and wide.credit.loss >= 5.0
    link, _, _ = _lenders(wide)
    assert link["status"] == "green" and "no market quote; not used for status" in link["reason"]


def test_a_market_quote_sets_l1_and_l2_whatever_the_model_says():
    book = _book(4.50, 4.50)
    # The model is at 83.3, a dealer quote at 96.5: amber on the quote, the model named in a note.
    loan = _price("FLT-2029", _floating_output(83.3), book, market=None)
    loan.market = Quote("FLT-2029", date(2026, 9, 18), 96.5, "dealer", "Kfgo", "https://kfgo.com/x", "R")
    link, status, _ = _lenders(loan)
    assert (link["status"], link["ruleCode"], status) == ("amber", "L1", "Strained")
    assert link["reason"].startswith(f"L1 {status_rules.L1_PRICE}: market price 96.5")
    assert "dealer quote, Kfgo, 2026-09-18" in link["reason"]
    assert (link["fact"]["tag"], link["fact"]["sourceName"], link["fact"]["asOf"]) == ("R", "Kfgo", "2026-09-18")
    # The model at 99, a fund mark at 93: red on the fund mark.
    marked = _price("FLT-2029", _floating_output(99.0), book, market=None)
    marked.market = Quote("FLT-2029", date(2026, 6, 30), 93.0, "nport", "N-PORT median of 3 funds (2026-06-30)",
                          None, "F")
    link, status, _ = _lenders(marked)
    assert (link["status"], link["ruleCode"], status) == ("red", "L2", "Broken")
    assert "fund mark" in link["reason"]
    # The model at 83.3, the market at 99: no rule; the note says which price set the status.
    fine = _price("FLT-2029", _floating_output(83.3), book, market=99.0)
    link, _, _ = _lenders(fine)
    assert link["status"] == "green" and "market price 99.0" in link["reason"]
    assert "Marden model 83.3 (FLT-2029) not used for status" in link["reason"]


def test_a_default_notice_still_trips_l2_without_a_market_quote():
    from marden.models.status_inputs import Item

    book = _book(4.50, 4.50)
    loan = _price("FLT-2029", _floating_output(99.0), book, market=None)
    notice = Item("Lender sent a notice of default to the borrower", date(2026, 10, 1), "F", "SEC EDGAR", None,
                  "fact:1", "instrument:FLT-2029", "research_terms")
    site = SiteInputs("test-site", "Test Site", ["Test"], {"lenders": Party("lenders", "Bank", None, ["Bank"])},
                      ["FLT-2029"], instrument_items=[notice], prices=[loan])
    out = status_rules.evaluate(site, Inputs(TODAY, {site.slug: site}, {}, 0, 0), {})
    link = next(lk for lk in out["chain"] if lk["role"] == "lenders")
    assert (link["status"], link["ruleCode"]) == ("red", "L2") and "Default notice" in link["reason"]


def test_market_quotes_read_trades_quotes_and_fund_marks_never_the_model(committed):
    committed.execute("INSERT INTO instrument (id, name, kind, issuer_name) VALUES ('T-LOAN', 'x', 'loan', 'x')")
    rows = [  # (day, kind, price, tag, source)
        ("2026-10-14", "marden", 83.3, "M", "Marden model price"),  # the model's own daily price: never
        ("2026-10-10", "exchange", 97.0, "M", "Some estimate"),  # an exchange row tagged M: not evidence
        ("2026-09-18", "dealer", 90.0, "R", "Kfgo"),
        ("2026-06-30", "fund_mark", 94.25, "F", "N-PORT: A Fund (2026-06-30)"),
        ("2025-12-31", "trade", 99.0, "F", "FINRA TRACE (finra.org)"),  # more than 200 days old
    ]
    for i, (day, kind, px, tag, name) in enumerate(rows):
        committed.execute("""INSERT INTO price_obs (instrument_id, obs_date, kind, price, tag, source_name, dedupe_key)
                             VALUES ('T-LOAN', %s, %s, %s, %s, %s, %s)""", (day, kind, px, tag, name, f"t{i}"))
    q = market_quotes(committed, TODAY, ["T-LOAN"])["T-LOAN"]
    assert (q.source, q.price, q.tag, q.day) == ("dealer", 90.0, "R", date(2026, 9, 18))
    # Before the dealer quote, the fund mark (public 60 days after its period end, 2026-08-29).
    q = market_quotes(committed, date(2026, 9, 1), ["T-LOAN"])["T-LOAN"]
    assert (q.source, q.price, q.words) == ("nport", 94.25, "fund mark")
    assert "T-LOAN" not in market_quotes(committed, date(2026, 8, 20), ["T-LOAN"])  # TRACE too old, mark not public


def events_ok(link: dict) -> None:
    from marden.core import events

    events.validate({"type": "status", "siteSlug": "test-site", "chain": [link]})
