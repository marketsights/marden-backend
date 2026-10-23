"""InstrumentDetail.yields and the yields on InstrumentDetail.priceHistory (model 'yield', marden.models.yields).

- yields: the yield to worst and to maturity of the latest Marden price (tag M), else of the latest market price
  (a TRACE trade, a BondTerminal or Börse Frankfurt quote; tagged with that price's tag, F or R, and source), the
  discount margin of a floating-rate loan, the dates worked (a holder put among them), the price they are worked
  from, the yields at the latest market price, `note` (a convertible: "Yield on the trade price, ignoring the
  conversion option.") and the method's assumptions in plain words. Without an output, or when the model could
  not work a yield, the values are n/d (null) and `why` says why.
- priceHistory: each Marden daily price carries `ytm` and `ytw` from the output's point for that day, when the
  point was worked from that same price (a price re-run since then shows no yield until the model re-runs).

An observed price from a source without a commercial licence is dropped when MARDEN_SHOW_UNLICENSED_PRICES is
false, as the builders drop its price_obs rows.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Row
from marden.core.licensing import show_unlicensed_prices, unlicensed_source_names
from marden.readmodels.views.instrument_inputs import iso, model_sourced, nd, num, output_field

NOT_RUN = "No yield yet: the yield model has not run on this instrument."
WORKOUT_KINDS = ("maturity", "extended", "call", "prepay", "put")
PRICE_MATCH = 0.0005


def _sourced(out: Row, key: str) -> dict[str, Any]:
    got = model_sourced(out, output_field(out, key))
    if got is None or (got["value"] is not None and num(got["value"]) is None):
        return nd("M")
    got["value"] = None if got["value"] is None else num(got["value"])
    return got


def _workout(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("kind") not in WORKOUT_KINDS or not isinstance(raw.get("date"), str):
        return None
    price = num(raw.get("price"))
    if price is None:
        return None
    return {"kind": raw["kind"], "date": raw["date"], "price": price, "label": str(raw.get("label") or ""),
            "yieldPct": num(raw.get("yieldPct")), "dmBp": num(raw.get("dmBp")), "counted": bool(raw.get("counted"))}


def _licensed(name: Any) -> bool:
    return show_unlicensed_prices() or name not in unlicensed_source_names()


def _basis(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or num(raw.get("price")) is None or not isinstance(raw.get("date"), str):
        return None
    if not _licensed(raw.get("sourceName")):
        return None
    out = {"price": num(raw["price"]), "date": raw["date"], "kind": str(raw.get("kind") or ""),
           "tag": str(raw.get("tag") or "M"), "sourceName": str(raw.get("sourceName") or "")}
    for key in ("sourceUrl", "methodVersion"):
        if isinstance(raw.get(key), str) and raw[key]:
            out[key] = raw[key]
    return out


def _observed(raw: Any) -> dict[str, Any] | None:
    base = _basis(raw)
    if base is None:
        return None
    return {**base, "ytm": num(raw.get("ytm")), "ytw": num(raw.get("ytw")),
            "reportedYieldPct": num(raw.get("reportedYieldPct"))}


def yields_doc(out: Row | None) -> dict[str, Any]:
    """InstrumentYields (types.ts) from the 'yield' output; n/d with the reason without one."""
    if out is None or not isinstance(out["output"], dict):
        return {"ytw": nd("M"), "ytm": nd("M"), "dmBp": None, "ytwTo": None, "ytmTo": None, "basis": None,
                "couponType": None, "workouts": [], "observed": None, "assumptions": [], "why": NOT_RUN, "note": None}
    basis = _basis(output_field(out, "basis"))
    hidden = basis is None and output_field(out, "basis") is not None  # worked from an unlicensed price
    dm = output_field(out, "dmBp")
    coupon = output_field(out, "couponType")
    doc = {
        "ytw": _sourced(out, "ytw"),
        "ytm": _sourced(out, "ytm"),
        "dmBp": _sourced(out, "dmBp") if isinstance(dm, dict) else None,
        "ytwTo": _workout(output_field(out, "ytwTo")),
        "ytmTo": _workout(output_field(out, "ytmTo")),
        "basis": basis,
        "couponType": coupon if coupon in ("fixed", "floating") else None,
        "workouts": [w for w in (_workout(r) for r in output_field(out, "workouts") or []) if w is not None],
        "observed": _observed(output_field(out, "observed")),
        "assumptions": [a for a in output_field(out, "assumptions") or [] if isinstance(a, str)],
        "why": output_field(out, "why") if isinstance(output_field(out, "why"), str) else None,
        "note": output_field(out, "note") if isinstance(output_field(out, "note"), str) else None,
    }
    if hidden:  # the yields of a price the screen may not show are not shown either
        doc.update({"ytw": nd("M"), "ytm": nd("M"), "dmBp": None, "ytwTo": None, "ytmTo": None, "workouts": [],
                    "note": None, "why": "No yield: the only price is from a source not licensed for display."})
    elif doc["ytw"]["value"] is None and doc["why"] is None:
        doc["why"] = "No yield: the model could not work one from the price."
    return doc


def price_history(series: list[Row], out: Row | None) -> list[dict[str, Any]]:
    """PricePoint[]: each Marden daily price with the yields worked from it (null when none was)."""
    points = output_field(out, "points") if out is not None else None
    by_day = {p["date"]: p for p in points or [] if isinstance(p, dict) and isinstance(p.get("date"), str)}
    rows = []
    for r in series:
        price = round(float(r["price"]), 4)
        p = by_day.get(iso(r["obs_date"]))
        same = p is not None and num(p.get("price")) is not None and abs(float(p["price"]) - price) < PRICE_MATCH
        rows.append({"date": iso(r["obs_date"]), "price": price, "ytm": num(p.get("ytm")) if same and p else None,
                     "ytw": num(p.get("ytw")) if same and p else None})
    return rows
