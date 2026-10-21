"""Read model `curve` ('all'): GET /api/curve, CurveTier[] (spec 06 curve card).

The tiers, their instrument points and the Nelson-Siegel fit are the 'curve' model's output for
subject 'all' (marden.models), dated on or before the build date. The builder only checks the shape:
points need a maturity in years, a yield and an instrument; fit points a maturity and a yield. Until
the model has run the curve is empty (no tier is drawn from placeholder yields).
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn
from marden.readmodels.registry import builder
from marden.readmodels.views.instrument_inputs import latest_output, num


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
        out.append(point)
    return sorted(out, key=lambda q: q["years"])


def curve_tiers(output: Any) -> list[dict[str, Any]]:
    """CurveTier[] from the model output (a list of tiers, or {tiers: [...]})."""
    raw = output.get("tiers") if isinstance(output, dict) else output
    tiers: list[dict[str, Any]] = []
    for t in raw if isinstance(raw, list) else []:
        if not isinstance(t, dict) or not isinstance(t.get("tier"), str) or not t["tier"]:
            continue
        tier = {
            "tier": t["tier"],
            "points": _points(t.get("points"), with_instrument=True),
            "fit": _points(t.get("fit"), with_instrument=False),
        }
        if tier["points"] or tier["fit"]:
            tiers.append(tier)
    return tiers


@builder("curve", many=True)
def build_curve(conn: Conn):
    out = latest_output(conn, "curve", "all")
    yield "all", curve_tiers(out["output"]) if out is not None else []
