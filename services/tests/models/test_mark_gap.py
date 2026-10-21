"""The `mark_gap` model on real seeded rows (model_rows.copy_model_rows): CoreWeave 9.625% 2032 (N-PORT
holders), CoreWeave DDTL 1.0 (BDC holders), The Barn project bonds, the CoreWeave 8.50% EUR notes (non-USD
lines) and Jupiter (no fund files it). Expectations are recomputed from the copied filings; the Marden
price is whatever the price model stored (until it runs, gaps are null and say so). Formula checks use
tiny hand-made numbers only.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from model_rows import copy_model_rows, rows

from marden.models import mark_gap, triggers
from marden.models.mark_gap_inputs import Holder, Price, line_public_date
from marden.readmodels.context import business_today

CRWV32, DDTL1, BARN, EUR, JUPITER = "EX-CRWV-104", "EX-CRWV-091", "EX-SALINE-308", "EX-CRWV-105", "EX-JUPITER-190"
IDS = [CRWV32, DDTL1, BARN, EUR, JUPITER]


@pytest.fixture
def copied(test_database):
    return copy_model_rows(IDS)


def _expected_holders(iid: str, day: date) -> dict[str, date]:
    """Fund -> period of its latest usable filing listing the instrument, kept when it is also the fund's newest
    usable filing (the rule, recomputed in Python from the copied rows)."""
    lines = rows("SELECT fund_entity_id, instrument_id, period_end, filing_type, filed_at FROM holding")
    usable = [r for r in lines if line_public_date(r["filing_type"], r["period_end"], r["filed_at"]) <= day]
    newest: dict[str, date] = {}
    for r in usable:
        newest[r["fund_entity_id"]] = max(newest.get(r["fund_entity_id"], r["period_end"]), r["period_end"])
    mine: dict[str, date] = {}
    for r in usable:
        if r["instrument_id"] == iid:
            mine[r["fund_entity_id"]] = max(mine.get(r["fund_entity_id"], r["period_end"]), r["period_end"])
    return {f: d for f, d in mine.items() if newest[f] == d}


def test_today_lists_the_funds_whose_latest_public_filing_holds_it(copied):
    today = business_today()
    result = mark_gap.run(instruments=IDS, trigger="test")
    out = result["outputs"]
    for iid in (CRWV32, DDTL1, BARN, EUR):
        expected = _expected_holders(iid, today)
        assert out[iid]["count"] == len(expected) > 0
        for h in out[iid]["holders"]:
            assert date.fromisoformat(h["filingDate"]) + timedelta(days=60 if h["filing"] == "N-PORT" else 0) <= today
            assert h["filing"] in ("N-PORT", "10-Q") and h["filingUrl"].startswith("https://www.sec.gov/")
    assert out[JUPITER]["markGap"] == "no fund" and out[JUPITER]["count"] == 0 and out[JUPITER]["holders"] == []


def test_last_mark_is_the_filed_value_over_par(copied):
    out = mark_gap.run(instruments=[CRWV32], trigger="test")["outputs"][CRWV32]
    funds = rows("""SELECT e.name, h.period_end, sum(h.par) par, sum(h.value_usd) val FROM holding h
                      JOIN entity e ON e.id = h.fund_entity_id WHERE h.instrument_id = %s AND h.mark_price IS NOT NULL
                     GROUP BY 1, 2""", (CRWV32,))
    by = {(r["name"], r["period_end"].isoformat()): 100 * float(r["val"]) / float(r["par"]) for r in funds}
    checked = 0
    for h in out["holders"]:
        key = (h["name"], h["filingDate"])
        if key in by and h["lastMark"] is not None:
            assert h["lastMark"] == pytest.approx(by[key], abs=1e-3)
            checked += 1
    assert checked >= 10
    marks = [h["lastMark"] for h in out["holders"] if h["filingDate"] == out["fundMarks"]["date"]]
    assert out["fundMarks"]["low"] == pytest.approx(min(marks), abs=1e-4)
    assert out["fundMarks"]["high"] == pytest.approx(max(marks), abs=1e-4)


def test_non_usd_lines_use_their_own_mark(copied):
    out = mark_gap.run(instruments=[EUR], trigger="test")["outputs"][EUR]
    marked = [h for h in out["holders"] if h["lastMark"] is not None]
    assert marked and all(80 < h["lastMark"] < 100 for h in marked)  # EUR notes filed at ~89-90


def test_gap_fields_follow_the_marden_price(copied):
    """Gaps exist only with a Marden price; without one they are null and named in `missing`."""
    out = mark_gap.run(instruments=[CRWV32], trigger="test")["outputs"][CRWV32]
    price = out["mardenPrice"]
    if price is None:
        assert out["markGap"]["value"] is None and any("Marden price" in m for m in out["missing"])
        assert all(h["gapPts"] is None and h["navHitMm"] is None for h in out["holders"])
        return
    for h in out["holders"]:
        if h["lastMark"] is not None:
            assert h["gapPts"] == pytest.approx(price["value"] - h["lastMark"], abs=1e-3)
            assert h["navHitMm"] == pytest.approx(h["parMm"] * h["gapPts"] / 100, rel=1e-3, abs=1e-3)
    hits = [h for h in out["holders"] if h["navHitMm"] is not None]
    total = sum(h["navHitMm"] for h in hits) / sum(h["parMm"] for h in hits) * 100
    assert out["markGap"]["value"] == pytest.approx(total, abs=1e-3)


def test_backtest_uses_only_marks_public_that_day(copied):
    day = date(2026, 9, 30)  # July N-PORT reports public from Sep 29; August ones from Oct 30
    out = mark_gap.run(day, [CRWV32, BARN], trigger="test")["outputs"]
    for iid in (CRWV32, BARN):
        assert out[iid]["count"] == len(_expected_holders(iid, day))
        for h in out[iid]["holders"]:
            if h["filing"] == "N-PORT":  # the 60-day rule; a BDC schedule is public on its filing date
                assert date.fromisoformat(h["filingDate"]) + timedelta(days=60) <= day
        assert any(h["filing"] == "N-PORT" for h in out[iid]["holders"])
        assert out[iid]["nextMarksPublic"] == (date(2026, 8, 31) + timedelta(days=60)).isoformat()
    early = mark_gap.run(date(2026, 8, 17), [CRWV32], trigger="test")["outputs"][CRWV32]
    assert all(h["filing"] == "10-Q" for h in early["holders"])  # no N-PORT report of it public by Aug 17


def test_holder_formula():
    h = Holder("f", "Fund", "N-PORT", "https://www.sec.gov/x", date(2026, 6, 30), None, par_usd=2e6,
               marked_par_usd=2e6, marked_value_usd=1.96e6, net_assets=4e8)
    row = mark_gap.holder_row(h, Price(80.0, date(2026, 10, 1), "price v0.1", None, "test"))
    assert row["lastMark"] == 98.0 and row["gapPts"] == -18.0
    assert row["navHitMm"] == pytest.approx(-0.36) and row["shareOfNav"] == pytest.approx(-0.0009)
    assert mark_gap.holder_row(h, None)["gapPts"] is None


def test_public_dates():
    assert line_public_date("N-PORT", date(2026, 7, 31), date(2026, 9, 25)) == date(2026, 9, 29)
    assert line_public_date("N-PORT", date(2026, 7, 31), date(2026, 10, 2)) == date(2026, 10, 2)
    assert line_public_date("10-Q", date(2026, 6, 30), date(2026, 8, 5)) == date(2026, 8, 5)
    assert line_public_date("10-Q", date(2026, 6, 30), None) == date(2026, 8, 14)
    assert mark_gap.next_public(date(2026, 10, 21), {"N-PORT"}) == date(2026, 10, 30)
    assert mark_gap.next_public(date(2026, 10, 21), {"10-Q"}) == date(2026, 11, 14)
    assert mark_gap.next_public(date(2026, 10, 21), set()) is None


def test_a_run_for_today_marks_the_instrument_views_and_a_new_line_marks_the_model(copied, committed):
    result = mark_gap.run(instruments=[CRWV32], trigger="test")
    run = committed.execute("SELECT ok, model, version FROM model_run WHERE id = %s", (result["run_id"],)).fetchone()
    assert run == {"ok": True, "model": "mark_gap", "version": "0.1"}
    dirty = {(r["view"], r["key"]) for r in committed.execute("SELECT view, key FROM read_model_dirty")}
    assert ("instrument_detail", CRWV32) in dirty and ("instruments", "all") in dirty
    line = committed.execute("SELECT id FROM holding WHERE instrument_id = %s LIMIT 1", (CRWV32,)).fetchone()
    targets = triggers.model_targets(committed, "holding", str(line["id"]), "@holding")
    assert ("mark_gap", CRWV32) in targets
    assert ("mark_gap", CRWV32) in triggers.model_targets(committed, "instrument", CRWV32, "@model:price")
