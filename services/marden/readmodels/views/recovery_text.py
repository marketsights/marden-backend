"""Recovery if default as display text, cents per dollar: from the research archive's recovery facts
(waterfall, project, GPU-only) and from model outputs.

The `recovery` model derives its range from the collateral ('62-74c', en dash; a bare '100' from the
model gets its 'c'). The price model's recovery is a default it assumes (marden.models.price_chain BUILDING_RECOVERY
and GPU_RECOVERY, 40% on bonds), never derived from the collateral, so it is shown as one: 'assumed 55c'.
"""

from __future__ import annotations

import re
from typing import Any

from marden.core.db import Row
from marden.readmodels.views.instrument_inputs import fact_sourced, num
from marden.readmodels.views.instrument_text import DASH

_CENTS = re.compile(rf"\d+(?:{DASH}\d+)?")  # the recovery model's range text: '100', '62-74'


def _pct(v: Any) -> int | None:
    n = num(v)
    return None if n is None else round(n)


def _band(base: int | None, low: int | None, high: int | None) -> str | None:
    if base is None and low is None and high is None:
        return None
    if base is None:
        return f"{low}{DASH}{high}c" if low is not None and high is not None else f"{low if low is not None else high}c"
    if low is not None and high is not None and (low, high) != (base, base):
        return f"{base}c ({low}{DASH}{high}c)"
    return f"{base}c"


def recovery_from_facts(facts: dict[str, Row]) -> dict[str, Any] | None:
    """Recovery if default, cents per dollar, from the recovery facts (waterfall, project, GPU)."""
    row = facts.get("recovery_waterfall")
    if row and isinstance(row["value"], dict):
        v = row["value"]
        pick = {
            k: _pct((v.get(k) or {}).get("total_recovery_pct", (v.get(k) or {}).get("recovery_pct")))
            for k in ("low", "base", "high")
        }
        text = _band(pick["base"], pick["low"], pick["high"])
        if text:
            return fact_sourced(row, text)
    row = facts.get("recovery_project")
    if row and isinstance(row["value"], dict):
        v = row["value"]
        text = _band(_pct(v.get("base_pct")), _pct(v.get("low_pct")), _pct(v.get("high_pct")))
        if text:
            return fact_sourced(row, text)
    row = facts.get("recovery_gpu")
    if row and isinstance(row["value"], dict):
        text = gpu_recovery_text(row["value"])
        if text:
            return fact_sourced(row, text)
    return None


def gpu_recovery_text(value: dict[str, Any]) -> str | None:
    """'91-100c' (en dash) from the conservative and moderate GPU-only recovery."""
    low, high = _pct(value.get("recovery_conservative_pct")), _pct(value.get("recovery_moderate_pct"))
    if low is None and high is None:
        return None
    if low is None or high is None or low == high:
        return f"{low if low is not None else high}c"
    return f"{min(low, high)}{DASH}{max(low, high)}c"


def cents_text(value: Any) -> str | None:
    """A model's recovery as text: a number (cents or a 0..1 share), a string ('62-74' -> '62-74c'), or
    {base, low, high}."""
    if isinstance(value, str):
        return f"{value}c" if _CENTS.fullmatch(value) else value
    n = num(value)
    if n is not None:
        return f"{round(n * 100 if n <= 1 else n)}c"
    if isinstance(value, dict):

        def cents(k: str) -> int | None:
            x = num(value.get(k))
            return None if x is None else round(x * 100 if x <= 1 else x)

        return _band(cents("base") if value.get("base") is not None else cents("value"), cents("low"), cents("high"))
    return None


def recovery_from_output(value: Any, *, assumed: bool = False) -> str | None:
    """`cents_text`, marked 'assumed 55c' when the value is a model's default, not derived (the price model)."""
    text = cents_text(value)
    return f"assumed {text}" if text and assumed else text
