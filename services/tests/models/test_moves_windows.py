"""Moves model windows (docs/methods/moves-v0.1.md): window starts, which two prices a window compares, partial and
stale windows said in words, and the main driver of a move."""

from __future__ import annotations

from datetime import date

from marden.models.moves_windows import Point, main_driver, nominal_start, pick, window

DAY = date(2026, 10, 22)  # a Thursday


def _m(d: date, px: float) -> Point:
    return Point(d, px, "model", "M", "Marden model price")


def _t(d: date, px: float) -> Point:
    return Point(d, px, "trade", "F", "FINRA TRACE (finra.org)")


def test_nominal_starts():
    assert nominal_start("1D", DAY) == date(2026, 10, 21)
    assert nominal_start("1D", date(2026, 10, 19)) == date(2026, 10, 16)  # Monday: the Friday before
    assert nominal_start("1W", DAY) == date(2026, 10, 15)
    assert nominal_start("1M", DAY) == date(2026, 9, 22)
    assert nominal_start("QTD", DAY) == date(2026, 9, 30)
    assert nominal_start("QTD", date(2026, 3, 5)) == date(2025, 12, 31)
    assert nominal_start("YTD", DAY) == date(2025, 12, 31)


def test_pick_uses_the_last_price_on_or_before_the_start():
    pts = [_m(date(2026, 10, 14), 87.2), _m(date(2026, 10, 21), 85.9), _m(DAY, 85.8)]
    frm, to, partial = pick(pts, date(2026, 10, 15), DAY)
    assert (frm.day, to.day, partial) == (date(2026, 10, 14), DAY, False)


def test_a_history_that_starts_inside_the_window_is_partial_and_says_so():
    model = [_m(date(2026, 7, 1), 99.5), _m(date(2026, 9, 30), 87.7), _m(DAY, 85.9)]
    w = window("YTD", DAY, None, model, [])
    assert w.partial and w.frm.day == date(2026, 7, 1)
    assert w.change == round(85.9 - 99.5, 3)
    assert w.note.startswith("Starts Jul 1: the first Marden price in this window")


def test_a_start_price_far_before_the_window_start_is_called_out():
    model = [_m(date(2026, 8, 17), 99.4), _m(DAY, 85.9)]
    w = window("1M", DAY, None, model, [])
    assert not w.partial and w.note == "Starts Aug 17: no Marden price between then and Sep 22"


def test_no_price_inside_the_window_is_no_move_not_a_zero():
    w = window("1D", DAY, None, [_m(date(2026, 10, 14), 87.2)], [])
    assert w.change is None and w.frm is None
    assert w.why == "No Marden price since Oct 14, before this window starts (Oct 21)."
    one = window("1W", DAY, None, [_m(DAY, 85.9)], [])
    assert one.change is None and one.why.startswith("Only one Marden price in this window")


def test_a_trade_inside_the_window_makes_it_a_traded_move():
    model = [_m(date(2026, 10, 14), 90.0), _m(DAY, 89.0)]
    trades = [_t(date(2026, 10, 14), 88.0), _t(date(2026, 10, 20), 86.5)]
    w = window("1W", DAY, None, model, trades)
    assert w.basis == "trade" and (w.frm.price, w.to.price) == (88.0, 86.5) and w.change == -1.5
    no_trade = window("1D", DAY, None, [model[0], _m(date(2026, 10, 21), 89.5), model[1]], trades)
    assert no_trade.basis == "model" and no_trade.change == -0.5


def test_main_driver_is_the_largest_part():
    events = [{"key": "completion:2026-10-21", "pts": -1.44}]
    assert main_driver(-1.34, events, None, "model") == {"kind": "event", "pts": -1.44, "key": "completion:2026-10-21"}
    assert main_driver(-0.8, [], -0.6, "model")["kind"] == "rates"
    spreads = main_driver(-2.0, [{"key": "q", "pts": -0.3}], -0.2, "model")
    assert spreads == {"kind": "spreads", "pts": -1.5}
    assert main_driver(-2.0, events, None, "trade") == {"kind": "trade", "pts": -2.0}
    assert main_driver(0.01, [], None, "model")["kind"] == "none"
    assert main_driver(None, [], None, None) == {"kind": "none", "pts": None}
