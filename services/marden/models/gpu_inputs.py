"""GPU market inputs (spec 19), read point in time for the GPU models (gpu_index, gpu_model,
gpu_collateral, gpu_deployment, and recovery for GPU-backed loans).

Series facts (seed loader marden/seed/cases_gpu.py; later the public price-page collectors):
- `gpu_list_price:<provider>:<model>` usd_per_gpu_hr + label: one published rental price per provider,
  model and capture day (valid_from). All captured prices are on-demand list prices (the labels say so;
  commitment discounts are only described in text), region not stated.
- `gpu_model:<key>` name, first_volume_ship ('YYYY-MM', 'YYYY-Qn' or 'YYYY'), tdp_w, new_price_usd
  {low, high, basis}, fp8_dense_pflops.
- `gpu_resale:<key>` usd_per_gpu + condition (secondary-market observations; cross-check only).
- `gpu_residual:<key>` residual_pct_of_new (third-party residual bands, tag M; cross-check only).
- `issue_comp:<deal>` deal_terms (type 'GPU loan') + issue_spread_bp: the GPU-loan tier spread.
- `ust_par_<tenor>` value: the Treasury par curve (discounting).
- `data_center_pue` pue_range.
Entity facts: `gpu_useful_life_years` (depreciation policy per company: 10-K/10-Q, or the American
Compute schedule citing it). Rows from unlicensed sources are dropped when MARDEN_SHOW_UNLICENSED_PRICES
is false (marden.models.pit).
"""

from __future__ import annotations

import itertools
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit
from marden.models.sourced import num

CHIP_KEYS = ("GB300", "GB200", "B300", "B200", "H200", "H100", "A100", "VR200", "MI300X", "MI325X", "MI355X")
_CHIP = re.compile(r"\b(" + "|".join(CHIP_KEYS) + r")\b", re.I)
_SHIP = re.compile(r"^(\d{4})(?:-(?:(\d{2})|Q([1-4])))?$")
UST_TENORS = {"1m": 1 / 12, "2m": 2 / 12, "3m": 0.25, "4m": 4 / 12, "6m": 0.5, "1y": 1, "2y": 2, "3y": 3, "5y": 5,
              "7y": 7, "10y": 10, "20y": 20, "30y": 30}
GPU_LOAN_TYPE = "GPU loan"
LIST_FIELDS = ("usd_per_gpu_hr", "label")
CHIP_FIELDS = ("name", "first_volume_ship", "tdp_w", "new_price_usd", "fp8_dense_pflops")
SERIES_FIELDS = (*LIST_FIELDS, *CHIP_FIELDS, "usd_per_gpu", "condition", "residual_pct_of_new", "deal_terms",
                 "issue_spread_bp", "pue_range")
ENTITY_FIELDS = ("gpu_useful_life_years",)
SERIES_PREFIXES = ("gpu_list_price:", "gpu_model:", "gpu_resale:", "gpu_residual:", "issue_comp:", "ust_par_",
                   "data_center_pue")


def chip_key(text: str | None) -> str | None:
    """'GB200 NVL72' -> 'GB200'; 'H100 SXM' -> 'H100'; None when no known model is named."""
    m = _CHIP.search(text or "")
    return m.group(1).upper() if m else None


def ship_date(text: Any) -> date | None:
    """First volume shipment as a day: the 15th of a month, the middle month of a quarter, Jul 1 of a year."""
    m = _SHIP.match(str(text or "").strip())
    if not m:
        return None
    year = int(m.group(1))
    if m.group(2):
        return date(year, int(m.group(2)), 15)
    if m.group(3):
        return date(year, int(m.group(3)) * 3 - 1, 15)
    return date(year, 7, 1)


def provider_name(label: str | None, slug: str) -> str:
    """The provider as its price page names it: the label's words before the model or plan name."""
    words: list[str] = []
    for w in (label or "").split():
        if any(ch.isdigit() for ch in w) or w.startswith("(") or w.upper() in ("HGX", "DGX", "GB200", "SXM"):
            break
        words.append(w)
    return " ".join(words) or slug.replace("-", " ").title()


@dataclass
class ListPrice:
    provider: str
    model: str
    usd_hr: float
    captured: date
    row: Row
    label: str | None


@dataclass
class Chip:
    key: str
    rows: dict[str, Row] = field(default_factory=dict)

    @property
    def launch(self) -> date | None:
        row = self.rows.get("first_volume_ship")
        return ship_date(row["value"]) if row else None

    @property
    def new_price(self) -> float | None:
        """Midpoint of the published new-unit price range (USD per GPU)."""
        row = self.rows.get("new_price_usd")
        v = row["value"] if row else None
        if not isinstance(v, dict):
            return None
        lo, hi = num(v.get("low")), num(v.get("high"))
        vals = [x for x in (lo, hi) if x]
        return sum(vals) / len(vals) if vals else None

    @property
    def tdp_kw(self) -> float | None:
        row = self.rows.get("tdp_w")
        w = num(row["value"]) if row else None
        return w / 1000.0 if w else None


@dataclass
class Market:
    as_of: date
    prices: list[ListPrice]
    chips: dict[str, Chip]
    resale: dict[str, list[Row]]
    residual: dict[str, Row]
    lives: dict[str, Row]  # entity id -> its newest useful-life fact (filed over reported on the same day)
    gpu_loan_spreads: list[tuple[date, float, str]]
    ust: dict[float, tuple[float, date]]  # tenor years -> (par yield %, observation day)
    pue: Row | None
    origins: list[int] = field(default_factory=list)

    def latest_prices(self) -> list[ListPrice]:
        """Each provider and model's newest captured price."""
        best: dict[tuple[str, str], ListPrice] = {}
        for p in self.prices:
            key = (p.provider, p.model)
            if key not in best or p.captured > best[key].captured:
                best[key] = p
        return sorted(best.values(), key=lambda p: (p.model, p.provider))

    def median_life(self) -> float | None:
        values = [v for r in self.lives.values() if (v := num(r["value"])) is not None]
        return statistics.median(values) if values else None

    def life_of(self, entity_ids: list[str]) -> Row | None:
        return next((self.lives[e] for e in entity_ids if e in self.lives and num(self.lives[e]["value"])), None)

    def gpu_loan_spread_bp(self) -> float | None:
        values = [bp for _, bp, _ in self.gpu_loan_spreads]
        return statistics.median(values) if values else None

    def ust_yield(self, years: float) -> float | None:
        """Par yield (%) at `years`, linear between the published tenors (flat beyond the ends)."""
        if not self.ust:
            return None
        tenors = sorted(self.ust)
        if years <= tenors[0]:
            return self.ust[tenors[0]][0]
        for lo, hi in itertools.pairwise(tenors):
            if years <= hi:
                w = (years - lo) / (hi - lo)
                return self.ust[lo][0] * (1 - w) + self.ust[hi][0] * w
        return self.ust[tenors[-1]][0]


def _series(conn: Conn, day: date) -> list[Row]:
    rows = pit.fact_rows(conn, day, "series", None, list(SERIES_FIELDS))
    return [r for r in rows if r["subject_id"].startswith(SERIES_PREFIXES)]


def _ust(conn: Conn, day: date) -> dict[float, tuple[float, date]]:
    ids = [f"ust_par_{t}" for t in UST_TENORS]
    out: dict[float, tuple[float, date]] = {}
    for r in conn.execute(
        """SELECT DISTINCT ON (subject_id) subject_id, value, valid_from, source_name FROM fact
            WHERE subject_type = 'series' AND subject_id = ANY(%s) AND field = 'value' AND valid_from <= %s
              AND status IN ('published', 'superseded') ORDER BY subject_id, valid_from DESC, id DESC""",
        (ids, day),
    ).fetchall():
        v = num(r["value"])
        if v is not None:
            out[UST_TENORS[r["subject_id"].removeprefix("ust_par_")]] = (v, r["valid_from"])
    return out


def _spread(conn: Conn, day: date, row: Row, terms: Row | None) -> float | None:
    """The comp's spread at issue: as stated (F/R), else computed in code from its terms (issue_spreads)."""
    from marden.models.issue_spreads import issue_spread
    from marden.models.price_curve import load_curves

    t = terms["value"] if terms is not None and isinstance(terms["value"], dict) else {}
    curve = None
    if row["tag"].strip() == "M" and t.get("benchmark") != "SOFR":
        curve = load_curves(conn, day, since=row["valid_from"]).curve(row["valid_from"])
    bp, _ = issue_spread(num(row["value"]), row["tag"], rate_text=str(t.get("rate") or ""),
                         coupon_pct=num(t.get("coupon_pct")), issue_price=num(t.get("issue_price")),
                         tenor_years=num(t.get("tenor_years")), curve=curve)
    return bp


def load(conn: Conn, day: date) -> Market:
    rows = _series(conn, day)
    by_subject: dict[str, dict[str, list[Row]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_subject[r["subject_id"]][r["field"]].append(r)
    prices: list[ListPrice] = []
    chips: dict[str, Chip] = {}
    resale: dict[str, list[Row]] = defaultdict(list)
    residual: dict[str, Row] = {}
    spreads: list[tuple[date, float, str]] = []
    pue = None
    for sid, fields in by_subject.items():
        if sid.startswith("gpu_list_price:"):
            _, slug, raw_model = sid.split(":", 2)
            model = chip_key(raw_model)
            labels = {r["valid_from"]: r["value"] for r in fields.get("label", [])}
            for r in fields.get("usd_per_gpu_hr", []):
                if model and (v := num(r["value"])) and v > 0:
                    label = labels.get(r["valid_from"])
                    prices.append(ListPrice(provider_name(label, slug), model, v, r["valid_from"], r, label))
        elif sid.startswith("gpu_model:"):
            chip = chips.setdefault(sid.removeprefix("gpu_model:"), Chip(sid.removeprefix("gpu_model:")))
            for f, rs in fields.items():
                if (best := pit.current_of(rs)) is not None:
                    chip.rows[f] = best
        elif sid.startswith("gpu_resale:"):
            resale[sid.removeprefix("gpu_resale:")] = sorted(fields.get("usd_per_gpu", []),
                                                             key=lambda r: r["valid_from"])
        elif sid.startswith("gpu_residual:") and (best := pit.current_of(fields.get("residual_pct_of_new", []))):
            residual[sid.removeprefix("gpu_residual:")] = best
        elif sid.startswith("issue_comp:"):
            terms = pit.current_of(fields.get("deal_terms", []))
            spread = pit.current_of(fields.get("issue_spread_bp", []))
            kind = terms["value"].get("type") if terms is not None and isinstance(terms["value"], dict) else None
            gpu_loan = kind == GPU_LOAN_TYPE
            if gpu_loan and spread is not None and (bp := _spread(conn, day, spread, terms)) is not None:
                spreads.append((spread["valid_from"], bp, sid))
        elif sid == "data_center_pue":
            pue = pit.current_of(fields.get("pue_range", []))
    lives: dict[str, Row] = {}
    for r in pit.fact_rows(conn, day, "entity", None, list(ENTITY_FIELDS)):
        if num(r["value"]) is None:
            continue  # ranges such as '5-6' are not a single policy
        have = lives.get(r["subject_id"])
        if have is None or (r["tag"], -r["valid_from"].toordinal()) < (have["tag"], -have["valid_from"].toordinal()):
            lives[r["subject_id"]] = r
    origins = sorted(int(r["id"]) for r in rows) + sorted(int(r["id"]) for r in lives.values())
    return Market(day, prices, chips, dict(resale), residual, lives, sorted(spreads), _ust(conn, day), pue, origins)
