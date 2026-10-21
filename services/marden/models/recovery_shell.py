"""Powered-shell recovery for site-backed debt (spec 22 "Recovery"; spec 04 "Recovery inputs"): what the
finished, powered buildings are worth if the tenant walks, and how much of that reaches each loan.

Inputs, read point in time (marden.models.pit), filed or reported values only (tag F or R; the archive's
model values, tag M, are not comps):
- comps: every site with a total project cost (`total_project_cost_usd`) and gross MW (`gross_mw`): cost
  per gross MW, a replacement-cost value per MW of finished powered capacity. A site whose cost basis is
  GPU capex is not a building comp. At least MIN_COMPS sites; the 25th / 50th / 75th percentiles.
- the site: gross MW (else critical IT MW x the midpoint of the published data-center PUE range, flagged
  in the basis) and its completion share: `pct_complete`, else delivered MW over critical IT MW (or
  gross MW when delivered exceeds IT); with nothing delivered and no stated completion the value of
  unfinished works is not known (n/d), never assumed.
- claims: the site's instruments other than GPU facilities (their collateral is GPUs), each at its filed
  drawn amount, else its amount, ranked by `seniority` (secured 1, junior / subordinated 2, unsecured 3;
  guarantees and leases are not claims on the buildings).
Shell value = gross MW x completion x comp value per MW; recovery = (value - claims ranking ahead) /
claims at the instrument's rank, between 0 and 100c; low / base / high from the 25th / 50th / 75th comp.
The basis carries the arithmetic (shell value low / base / high, `valueUsd`; cover before the cap, `coverCents`).
Not in v0 (named in `missing` / basis): re-letting time and carry, unspent proceeds held in project
accounts, third-party support (residual value guarantees, backstops).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from marden.core.db import Conn, Row
from marden.models import pit
from marden.models.sourced import num

MIN_COMPS = 3
SITE_FIELDS = ("total_project_cost_usd", "gross_mw", "critical_it_mw", "pct_complete", "delivered_mw", "cost_basis")
INSTRUMENT_FIELDS = ("seniority", "drawn_usd", "amount_usd")
SOURCED_TAGS = ("F", "R")


def rank_of(seniority: Any) -> int | None:
    text = str(seniority or "").lower()
    if not text or "guarantee" in text or "lease" in text or "tbd" in text or "per filing" in text:
        return None
    if any(w in text for w in ("junior", "subordinat", "second", "mezz")):
        return 2
    if any(w in text for w in ("unsecured", "convertible", "preferred")):
        return 3
    return 1 if ("secured" in text or "mortgage" in text or "private credit" in text) else None


def _best(rows: list[Row]) -> Row | None:
    """Filed or reported value (filed first, newest)."""
    usable = [r for r in rows if str(r["tag"]).strip() in SOURCED_TAGS and num(r["value"]) is not None]
    return pit.current_of(usable)


@dataclass
class Comps:
    per_mw: list[tuple[str, float]] = field(default_factory=list)  # (site, USD per gross MW)

    def quantiles(self) -> tuple[float, float, float] | None:
        if len(self.per_mw) < MIN_COMPS:
            return None
        v = np.array([x for _, x in self.per_mw])
        return float(np.percentile(v, 25)), float(np.percentile(v, 50)), float(np.percentile(v, 75))


@dataclass
class SiteFacts:
    slug: str
    rows: dict[str, list[Row]]

    def best(self, fld: str) -> Row | None:
        return _best(self.rows.get(fld, []))

    def completion(self) -> tuple[float | None, str]:
        pct = self.best("pct_complete")
        if pct is not None:
            return max(0.0, min(1.0, float(pct["value"]) / 100.0)), "pct_complete"
        delivered, it, gross = self.best("delivered_mw"), self.best("critical_it_mw"), self.best("gross_mw")
        d = num(delivered["value"]) if delivered else None
        if not d or d <= 0:
            return None, "nothing delivered and no stated completion"
        cap = num(it["value"]) if it else None
        if cap is None or d > cap:
            cap = num(gross["value"]) if gross else None
        return (min(1.0, d / cap), "delivered_mw / capacity") if cap else (None, "no capacity")


def pue_mid(conn: Conn, day: date) -> tuple[float | None, Row | None]:
    """Midpoint of the published data-center PUE range (series `data_center_pue`)."""
    row = pit.current_of(pit.fact_rows(conn, day, "series", ["data_center_pue"], ["pue_range"]))
    v = row["value"] if row is not None and isinstance(row["value"], dict) else {}
    lo, hi = num(v.get("low")), num(v.get("high"))
    return ((lo + hi) / 2, row) if lo and hi else (None, row)


def load_sites(conn: Conn, day: date) -> dict[str, SiteFacts]:
    out: dict[str, SiteFacts] = {}
    for r in pit.fact_rows(conn, day, "site", None, list(SITE_FIELDS)):
        out.setdefault(r["subject_id"], SiteFacts(r["subject_id"], {})).rows.setdefault(r["field"], []).append(r)
    return out


def comps(sites: dict[str, SiteFacts]) -> Comps:
    c = Comps()
    for slug, s in sorted(sites.items()):
        cost, gross = s.best("total_project_cost_usd"), s.best("gross_mw")
        basis = pit.current_of(s.rows.get("cost_basis", []))
        if basis is not None and "gpu" in str(basis["value"]).lower():
            continue
        if cost and gross and num(gross["value"]):
            c.per_mw.append((slug, float(cost["value"]) / float(gross["value"])))
    return c


@dataclass
class Claim:
    instrument_id: str
    rank: int | None
    amount: float | None
    basis: str


def site_claims(conn: Conn, day: date, slug: str, exclude: set[str]) -> list[Claim]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM instrument WHERE site_slug = %s ORDER BY id", (slug,))]
    ids = [i for i in ids if i not in exclude]
    rows = pit.fact_rows(conn, day, "instrument", ids, list(INSTRUMENT_FIELDS)) if ids else []
    by: dict[str, dict[str, list[Row]]] = {}
    for r in rows:
        by.setdefault(r["subject_id"], {}).setdefault(r["field"], []).append(r)
    out = []
    for iid in ids:
        f = by.get(iid, {})
        sen = pit.current_of(f.get("seniority", []))
        drawn, amount = pit.current_of(f.get("drawn_usd", [])), pit.current_of(f.get("amount_usd", []))
        pick = drawn if drawn is not None and num(drawn["value"]) else amount
        out.append(Claim(iid, rank_of(sen["value"] if sen else None), num(pick["value"]) if pick else None,
                         pick["field"] if pick else "none"))
    return out


@dataclass
class ShellResult:
    value: tuple[float, float, float] | None  # USD at the comp quartiles
    low: float | None
    base: float | None
    high: float | None
    ahead: float | None
    pari: float | None
    missing: list[str]
    basis: dict[str, Any]


def shell_recovery(site: SiteFacts | None, comp: Comps, claims: list[Claim], instrument_id: str,
                   pue: float | None = None) -> ShellResult:
    missing: list[str] = []
    q = comp.quantiles()
    if q is None:
        missing.append(f"at least {MIN_COMPS} cost-per-MW comps (total project cost and gross MW)")
    gross_row = site.best("gross_mw") if site else None
    it_row = site.best("critical_it_mw") if site else None
    gross_mw, gross_basis = (float(gross_row["value"]), "gross_mw") if gross_row else (None, None)
    if gross_mw is None and it_row is not None and pue:
        gross_mw, gross_basis = float(it_row["value"]) * pue, f"critical_it_mw x PUE {pue:.2f}"
    if gross_mw is None:
        missing.append("site gross MW (or critical IT MW)")
    share, share_basis = site.completion() if site else (None, "no site facts")
    if share is None:
        missing.append(f"completion of the works ({share_basis}); value of unfinished works and unspent proceeds")
    own = next((c for c in claims if c.instrument_id == instrument_id), None)
    if own is None or own.rank is None:
        missing.append("the instrument's rank in the site's debt (seniority)")
    claims_known = [c for c in claims if c.rank is not None]
    unranked = sorted(c.instrument_id for c in claims if c.rank is None and c is not own)
    if unranked:  # a claim of unknown seniority may rank ahead: the waterfall is n/d, not 'nothing ahead'
        missing.append("seniority of other claims on the site: " + ", ".join(unranked))
    ahead = pari = None
    if own is not None and own.rank is not None and not unranked:
        senior = [c for c in claims_known if (c.rank or 0) < own.rank]
        same = [c for c in claims_known if c.rank == own.rank]
        if any(c.amount is None for c in senior + same):
            missing.append("amounts of claims at or ahead of the instrument: " + ", ".join(
                sorted(c.instrument_id for c in senior + same if c.amount is None)))
        else:
            ahead, pari = sum(c.amount or 0 for c in senior), sum(c.amount or 0 for c in same)
    value = None
    rec: list[float | None] = [None, None, None]
    if q and gross_mw and share is not None:
        value = (gross_mw * share * q[0], gross_mw * share * q[1], gross_mw * share * q[2])
        if ahead is not None and pari:
            rec = [max(0.0, min(1.0, (v - ahead) / pari)) for v in value]
    basis = {
        "comps": [{"site": s, "usdPerGrossMw": round(v)} for s, v in comp.per_mw],
        "compQuartilesUsdPerMw": None if q is None else [round(x) for x in q],
        "compMedianUsdPerMw": None if not comp.per_mw else round(statistics.median(v for _, v in comp.per_mw)),
        "grossMw": None if gross_mw is None else round(gross_mw, 1), "grossMwBasis": gross_basis,
        "completion": None if share is None else round(share, 4), "completionBasis": share_basis,
        "valueUsd": None if value is None else [round(v) for v in value],
        "coverCents": [round((v - ahead) / pari * 100) for v in value] if value and ahead is not None and pari
        else None,
        "claims": [{"instrumentId": c.instrument_id, "rank": c.rank, "amountUsd": c.amount, "basis": c.basis}
                   for c in claims],
        "notInV0": ["re-letting time and carry", "unspent proceeds in project accounts",
                    "third-party support (residual value guarantees, backstops)"],
    }
    return ShellResult(value, rec[0], rec[1], rec[2], ahead, pari, missing, basis)
