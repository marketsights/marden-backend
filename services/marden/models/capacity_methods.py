"""The facility-MW methods of the capacity model (spec 27 step 1), each a low / base / high range with its
evidence, read point in time. Every input is a fact Marden holds; a method without inputs is not shown.

- Filed: the campus's own `facility_mw` fact (tag F filed; a reported R figure is shown as a check and
  never replaces a filing);
- Utility load: the large-load request MW (`large_load_request` {mw}) and the grid service MW (`grid_mw`);
- Prime generation: the plants serving the campus (plant_serves; not denied or withdrawn), from their
  unit rows (count x rating each, diesel units excluded: those are backup) and their `capacity_mw`; the
  range runs from the smaller figure less one unit (N+1) to the larger;
- Backup generators: the generator count on the campus (`backup_generator_count`) x a standby rating;
  no permit Marden holds states the rating, so the range is n/d and says so;
- Footprint: the building area (`sqft_total`, or a building's footprint polygon) x the power density
  (facility MW per sq ft) of the same developer's other campuses that disclose both, else of every campus
  that does (quartiles); the weakest method, filling gaps only;
- Disclosed GPU count: the campus's disclosed chips x kW per GPU / GPU share of IT x PUE (an implied
  facility MW; the chip count itself is the GPU cross-check).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from marden.core.db import Row
from marden.models.sourced import num

SQFT_PER_M2 = 10.7639
DEAD_PLANTS = ("Denied", "Withdrawn")
_MW = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(GW|MW)\b", re.I)


@dataclass
class Method:
    method: str
    low: float | None
    base: float | None
    high: float | None
    assumptions: str
    rows: list[Row] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.base is not None


def mw_of(value: Any) -> float | None:
    """A MW value from a number, {mw: n} or text ('1,400 MW', '2.45 GW')."""
    if isinstance(value, dict):
        return mw_of(value.get("mw") if value.get("mw") is not None else value.get("value"))
    if (n := num(value)) is not None:
        return n
    m = _MW.search(str(value or ""))
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v * 1000 if m.group(2).upper() == "GW" else v


def filed(cur: dict[str, Row]) -> Method | None:
    row = cur.get("facility_mw")
    v = mw_of(row["value"]) if row else None
    if v is None:
        return None
    tag = row["tag"].strip()  # type: ignore[index]
    note = "facility MW stated in a filing" if tag == "F" else "facility MW as reported (press / third party), not filed"
    return Method("Filed", v, v, v, note, [row])  # type: ignore[list-item]


def utility_load(cur: dict[str, Row]) -> Method | None:
    vals = [(mw_of(cur[f]["value"]), cur[f]) for f in ("large_load_request", "grid_mw") if f in cur]
    vals = [(v, r) for v, r in vals if v]
    if not vals:
        return None
    lo, hi = min(v for v, _ in vals), max(v for v, _ in vals)
    return Method("Utility load", lo, hi, hi, "large-load request / grid service MW: the facility draw ceiling",
                  [r for _, r in vals])


def prime_generation(plants: list[dict[str, Any]]) -> Method | None:
    figures, rows, largest = [], [], 0.0
    for p in plants:
        if p["status"] in DEAD_PLANTS:
            continue
        unit_mw = 0.0
        for u in p["units"]:
            if u["fuel"] == "diesel" or u["count"] is None or u["rating"] is None:
                continue
            unit_mw += u["count"] * u["rating"]
            largest = max(largest, u["rating"])
            rows.extend(u["rows"])
        cap = p["capacity_mw"]
        got = [x for x in (unit_mw or None, cap) if x]
        if got:
            figures.append((min(got), max(got)))
        if p["capacity_row"] is not None:
            rows.append(p["capacity_row"])
    if not figures:
        return None
    lo, hi = sum(f[0] for f in figures), sum(f[1] for f in figures)
    lo = max(lo - largest, 0.0)
    return Method("Prime generation", lo, (lo + hi) / 2, hi,
                  "serving plants' prime units (count x rating, diesel excluded) and stated capacity; low less one "
                  "unit for N+1 redundancy", rows)


def backup(cur: dict[str, Row]) -> Method | None:
    row = cur.get("backup_generator_count")
    if row is None or num(row["value"]) is None:
        return None
    return Method("Backup generators", None, None, None,
                  f"{int(num(row['value']))} generators seen on site; no permit Marden holds states the standby "
                  "rating per unit or the redundancy design, so no MW is derived (2N to N+1 once rated)", [row])


def densities(campus_rows: dict[str, dict[str, Row]]) -> dict[str, list[tuple[str, float]]]:
    """developer key -> [(campus id, facility MW per sq ft)] for campuses that state both."""
    out: dict[str, list[tuple[str, float]]] = {}
    for cid, cur in campus_rows.items():
        mw = mw_of(cur["facility_mw"]["value"]) if "facility_mw" in cur else None
        sq = num(cur["sqft_total"]["value"]) if "sqft_total" in cur else None
        if mw and sq:
            out.setdefault(developer_key(cur), []).append((cid, mw / sq))
            out.setdefault("*", []).append((cid, mw / sq))
    return out


def developer_key(cur: dict[str, Row]) -> str:
    text = str((cur.get("developer") or {}).get("value") or "").lower()
    return re.split(r"[(/,;]| with | and ", text)[0].strip() or "?"


def footprint(sqft: float | None, row: Row | None, campus_id: str, dev: str,
              dens: dict[str, list[tuple[str, float]]], what: str) -> Method | None:
    if not sqft:
        return None
    pool = [d for c, d in dens.get(dev, []) if c != campus_id]
    basis = f"density of {dev}'s other campuses ({len(pool)})"
    if not pool:
        pool = [d for c, d in dens.get("*", []) if c != campus_id]
        basis = f"density of every campus stating facility MW and floor area ({len(pool)}): the developer discloses none"
    if len(pool) < 1:
        return None
    if len(pool) >= 4:
        q = statistics.quantiles(pool, n=4)
        lo, mid, hi = q[0], statistics.median(pool), q[2]
    else:
        lo, mid, hi = min(pool), statistics.median(pool), max(pool)
    return Method("Footprint", sqft * lo, sqft * mid, sqft * hi, f"{what} {sqft:,.0f} sq ft x {basis}",
                  [row] if row is not None else [])


def gpu_implied(chips: float | None, row: Row | None, kw: float | None, share: tuple[float, float, float] | None,
                pue: tuple[float, float, float] | None) -> Method | None:
    if not chips or row is None or kw is None or share is None or pue is None:
        return None
    lo = chips * kw / 1000 / share[2] * pue[0]
    mid = chips * kw / 1000 / share[1] * pue[1]
    hi = chips * kw / 1000 / share[0] * pue[2]
    return Method("Disclosed GPU count", lo, mid, hi,
                  f"{chips:,.0f} disclosed GPUs x {kw:.2f} kW / GPU share of IT x PUE", [row])


RANK = ("Filed", "Utility load", "Prime generation", "Disclosed GPU count", "Footprint")


def _rank(m: Method) -> int:
    return RANK.index(m.method) if m.method in RANK else len(RANK)


def combine(methods: list[Method]) -> tuple[Method | None, bool, str]:
    """(the answer range, disagreement, confidence). A filed (F) figure is the answer (High). Otherwise the
    two strongest methods with figures (a reported facility MW, utility load, prime generation, the GPU
    count, the footprint: spec 27's order): their overlap when they overlap, else the span of the two
    centred on the stronger; Medium when their figures are within 25%, else Low. Disagreement: any two methods' figures differ by more
    than 25%."""
    known = [m for m in methods if m.known]
    bases = [m.base for m in known if m.base]
    disagree = len(bases) >= 2 and max(bases) / min(bases) - 1 > 0.25
    hard = [m for m in known if m.method == "Filed" and m.rows and m.rows[0]["tag"].strip() == "F"]
    if hard:
        return hard[0], disagree, "High"
    ranked = sorted(known, key=_rank)
    if not ranked:
        return None, False, "Low"
    first = ranked[0]
    if len(ranked) == 1:
        return first, disagree, "Low"
    second = ranked[1]
    close = max(first.base, second.base) / min(first.base, second.base) - 1 <= 0.25  # type: ignore[type-var,operator]
    lo, hi = max(first.low, second.low), min(first.high, second.high)  # type: ignore[type-var]
    if lo <= hi:
        base = min(max(statistics.median([first.base, second.base]), lo), hi)  # type: ignore[type-var]
    else:  # no overlap: the span of the two, centred on the stronger
        lo, hi = min(first.low, second.low), max(first.high, second.high)  # type: ignore[type-var]
        base = first.base
    return Method("combined", lo, base, hi, ""), disagree, "Medium" if close else "Low"
