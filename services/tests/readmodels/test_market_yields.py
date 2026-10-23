"""Yields worked from a market price when there is no Marden price (a convertible): the instrument page's
yields keep the market price's tag and the convertible's note, and a traded Moves window's yield change takes
the trade's tag (readmodels/views/instrument_yields.py, readmodels/views/moves.py)."""

from __future__ import annotations

from datetime import date

from marden.readmodels.views.instrument_yields import yields_doc
from marden.readmodels.views.moves import windows_of

TRACE = "FINRA TRACE (finra.org)"
NOTE = "Yield on the trade price, ignoring the conversion option."


def _trade(day: str, price: float) -> dict:
    return {"date": day, "price": price, "tag": "F", "sourceName": TRACE, "basis": "trade", "trades": 1,
            "sourceUrl": "https://www.finra.org/x", "ytm": None, "ytw": None, "dmBp": None}


def _window(key: str) -> dict:
    return {"key": key, "nominalStart": "2026-09-22", "start": "2026-09-22", "end": "2026-10-20", "partial": False,
            "basis": "trade", "from": _trade("2026-09-22", 109.275), "to": _trade("2026-10-20", 83.05),
            "changePts": -26.225, "yieldFrom": 1.392, "yieldTo": 6.076, "yieldChangeBp": 468.4, "dmChangeBp": None,
            "why": None, "note": None, "events": [], "rates": None, "driver": {"kind": "trade", "pts": -26.225}}


def test_a_convertibles_yields_keep_the_trade_tag_and_the_note():
    value = {"value": 6.076, "tag": "F", "sourceName": TRACE, "asOf": "2026-10-20", "methodVersion": "yield v0.1",
             "sourceUrl": "https://www.finra.org/x"}
    out = {"model": "yield", "version": "0.1", "as_of": date(2026, 10, 22), "output": {
        "ytw": value, "ytm": value, "dmBp": None, "couponType": "fixed", "note": NOTE, "why": None,
        "basis": {"price": 83.05, "date": "2026-10-20", "kind": "trade", "tag": "F", "sourceName": TRACE},
        "ytwTo": {"kind": "put", "date": "2029-06-01", "price": 100, "label": "Put by holders at par on Jun 1, 2029",
                  "yieldPct": 6.076, "dmBp": None, "counted": True},
        "workouts": [], "assumptions": ["A convertible note, worked as a straight bond."]}}
    doc = yields_doc(out)
    assert doc["ytw"]["value"] == 6.076 and doc["ytw"]["tag"] == "F" and doc["ytw"]["sourceName"] == TRACE
    assert doc["note"] == NOTE and doc["why"] is None and doc["ytwTo"]["kind"] == "put"
    assert yields_doc(None)["note"] is None


def test_a_traded_windows_yield_change_takes_the_trade_tag_only_without_a_marden_price():
    market = windows_of({"measure": "YTM", "modelFrom": None, "windows": {"1M": _window("1M")}})["1M"]
    assert market["yieldChangeBp"]["value"] == 468.4 and market["yieldChangeBp"]["tag"] == "F"
    assert market["yieldChangeBp"]["sourceName"] == TRACE and market["yieldChangeBp"]["asOf"] == "2026-10-20"
    priced = windows_of({"measure": "YTM", "modelFrom": "2026-07-01", "windows": {"1M": _window("1M")}})["1M"]
    assert priced["yieldChangeBp"]["tag"] == "M" and priced["yieldChangeBp"]["value"] == 468.4
