"""Read model `curve` ('all'): GET /api/curve, CurveTier[] (spec 06 curve card).

The Treasury benchmark, the tiers, their instrument points and each tier's fitted curve are the 'curve'
model's output for subject 'all' (marden.models.price_curve_card), dated on or before the build date. The
builder only checks the shape: points need a maturity in years, a yield and an instrument; fit points a
maturity and a yield. The optional fields (spread, sources, the latest observation, why a dot is left out
of the fit, the fit's range and note) are passed on when well formed and dropped otherwise; an older
output without them still builds. Until the model has run the curve is empty (no tier is drawn from
placeholder yields).
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn
from marden.readmodels.registry import builder
from marden.readmodels.views.instrument_inputs import latest_output, num

TAGS = ("F", "R", "M")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _source(raw: Any) -> dict[str, Any] | None:
    """CurveSource: a tag, an as-of date and a source name (plus an optional URL and method version)."""
    if not isinstance(raw, dict) or raw.get("tag") not in TAGS or not _text(raw.get("asOf")):
        return None
    if not _text(raw.get("sourceName")):
        return None
    out = {"tag": raw["tag"], "asOf": raw["asOf"], "sourceName": raw["sourceName"]}
    for key in ("sourceUrl", "methodVersion"):
        if _text(raw.get(key)):
            out[key] = raw[key]
    if out["tag"] == "M" and "methodVersion" not in out:
        return None
    return out


def _observation(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("tag") not in TAGS or num(raw.get("price")) is None:
        return None
    if not (_text(raw.get("date")) and _text(raw.get("kind")) and _text(raw.get("sourceName"))):
        return None
    out = {"date": raw["date"], "price": num(raw["price"]), "tag": raw["tag"], "kind": raw["kind"],
           "sourceName": raw["sourceName"]}
    if _text(raw.get("sourceUrl")):
        out["sourceUrl"] = raw["sourceUrl"]
    return out


def _points(raw: Any, *, with_instrument: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict):
            continue
        years, yld = num(p.get("years")), num(p.get("yieldPct"))
        if years is None or yld is None:
            continue
        point: dict[str, Any] = {"years": years, "yieldPct": yld}
        if with_instrument:
            if not isinstance(p.get("instrumentId"), str) or not p["instrumentId"]:
                continue
            point["instrumentId"] = p["instrumentId"]
            if "spreadBp" in p:
                point["spreadBp"] = num(p.get("spreadBp"))
            source = _source(p.get("source"))
            if source is not None:
                point["source"] = source
            if "lastObservation" in p:
                point["lastObservation"] = _observation(p.get("lastObservation"))
            if "excluded" in p:
                point["excluded"] = _text(p.get("excluded"))
        elif num(p.get("spreadBp")) is not None:
            point["spreadBp"] = num(p["spreadBp"])
        out.append(point)
    return sorted(out, key=lambda q: q["years"])


def _range(raw: Any) -> list[float] | None:
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    lo, hi = num(raw[0]), num(raw[1])
    return [lo, hi] if lo is not None and hi is not None and 0 < lo <= hi else None


def curve_tiers(output: Any) -> list[dict[str, Any]]:
    """CurveTier[] from the model output (a list of tiers, or {tiers: [...]})."""
    raw = output.get("tiers") if isinstance(output, dict) else output
    tiers: list[dict[str, Any]] = []
    for t in raw if isinstance(raw, list) else []:
        if not isinstance(t, dict) or not isinstance(t.get("tier"), str) or not t["tier"]:
            continue
        tier: dict[str, Any] = {
            "tier": t["tier"],
            "points": _points(t.get("points"), with_instrument=True),
            "fit": _points(t.get("fit"), with_instrument=False),
        }
        if t.get("kind") in ("tier", "benchmark"):
            tier["kind"] = t["kind"]
        if "range" in t:
            tier["range"] = _range(t.get("range"))
        if "robustSdBp" in t:
            tier["robustSdBp"] = num(t.get("robustSdBp"))
        if "note" in t:
            tier["note"] = _text(t.get("note"))
        source = _source(t.get("source"))
        if source is not None:
            tier["source"] = source
        if tier["points"] or tier["fit"] or tier.get("note"):
            tiers.append(tier)
    return tiers


@builder("curve", many=True)
def build_curve(conn: Conn):
    out = latest_output(conn, "curve", "all")
    yield "all", curve_tiers(out["output"]) if out is not None else []
