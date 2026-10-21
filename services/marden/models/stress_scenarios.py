"""The stress scenarios of spec 09 as shocks to the price engine's inputs (stress v0.1, 'model v0').

Every scenario turns into a `stress_price.Shock` (intensity multipliers per entity, completion moves per
site) plus the sites it touches with the status rule each one trips (00-global "Status rules"):

- end_user_funding_miss {endUser, cutPct}: the end user cuts cutPct% of its contracts. Each payee of its
  payment edges (the tenants on its chains) loses cutPct% of the remaining contract: the edge's remaining
  schedule discounted at the 10-year Treasury par yield (marden.models.graph), or its stated total when it
  has no schedule. That loss comes off the payee's equity value (each dollar of revenue lost is a dollar of
  value: the leases and GPU costs behind it stay; v0 upper bound) and CreditGrades (the price engine's
  structural model, price_equity) turns the lower share price into the payee's intensity multiplier. The
  end-user link of every site it uses turns red (E2, contract cut).
- tenant_downgrade {tenant, rating}: the tenant is cut to `rating`. Multiplier = exp(beta x notches), beta
  the slope of log bond-implied intensity on rating notch across the issuers Marden prices (the price
  engine's curves on the day, their ratings from credit_inputs); at least two issuers with different
  ratings, else n/d. The tenant link turns amber (T1: BBB- or lower), red (T2) where the status model
  already has it red.
- power_delay {months}: every V1 site still waiting on a power milestone (utility agreement, gas or grid,
  on-site generation not done) has those milestones moved `months` later and the cushion model re-run
  in-process (same inputs and seed as its stored run, so the base equals the stored cushion); the rent
  start P50 moves by the result and the price engine reads the later completion. M2 when the cushion
  crosses below zero, M1 when the P50 slips 3+ months.

Severe: spreads widen by the peak-2022 option-adjusted spread of the tenant's rating index over today's
(ICE BofA index via FRED, `ice_bofa_us_<bucket>_oas`); while that series is not loaded the severe prices
are n/d and say so.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import numpy as np

from marden.core.db import Conn
from marden.core.jsonutil import canonical_json, hash_parts
from marden.models import graph, pit, price_equity
from marden.models import cushion_output as cout
from marden.models.credit_inputs import AgencyRating, notch, ratings
from marden.models.cushion import MODEL as CUSHION_MODEL
from marden.models.cushion import VERSION as CUSHION_VERSION
from marden.models.cushion_history import load_history
from marden.models.cushion_inputs import load_sites
from marden.models.cushion_power import build as build_milestones
from marden.models.cushion_sim import simulate
from marden.models.stress_price import Book, Shock

POWER_KEYS = ("utility_agreement", "gas_or_grid", "onsite_generation")
BUCKETS = ((3, "aa"), (6, "a"), (9, "bbb"), (12, "bb"), (15, "b"))  # notch ceiling -> index bucket
DEFAULTS: dict[str, dict[str, Any]] = {
    "end_user_funding_miss": {"endUser": "openai", "cutPct": 30},
    "tenant_downgrade": {"tenant": "oracle", "rating": "BB+"},
    "power_delay": {"months": 6},
}


@dataclass
class SiteHit:
    slug: str
    rule: str | None
    why: str
    severity: str  # LinkStatus
    cushion: tuple[float | None, float | None] | None = None


@dataclass
class Scenario:
    name: str
    label: str
    shock: Shock = field(default_factory=Shock)
    sites: dict[str, SiteHit] = field(default_factory=dict)
    tenants: list[str] = field(default_factory=list)  # whose own notes are the shaded last row
    missing: list[str] = field(default_factory=list)
    basis: dict[str, Any] = field(default_factory=dict)


def _r5(book: Book) -> float:
    curve = book.ctx.book.curve(book.day)
    return curve.par_yield(5.0) / 100.0 if curve else 0.04


def equity_multiplier(conn: Conn, book: Book, entity_id: str, loss_usd: float) -> tuple[float | None, dict, list]:
    """CreditGrades intensity multiplier of an equity-value loss (share price less loss / shares)."""
    row = conn.execute("SELECT ticker FROM entity WHERE id = %s", (entity_id,)).fetchone()
    eq, missing = price_equity.load(conn, book.day, entity_id, row["ticker"] if row else None)
    shares = pit.current_of(pit.fact_rows(conn, book.day, "entity", [entity_id], ["shares_outstanding"]))
    if eq is None or shares is None or not shares["value"]:
        return None, {}, missing + ([f"entity {entity_id}: shares_outstanding"] if shares is None else [])
    days = [d for d in eq.days() if d <= book.day]
    d = days[-1] if days else None
    sigma, dps = (eq.vol(d), eq.d_on(d)) if d else (None, None)
    if d is None or sigma is None or dps is None:
        return None, {}, missing + [f"entity {entity_id}: equity close, volatility or debt per share on {book.day}"]
    s = eq.closes[d]
    s_after = max(s - loss_usd / float(shares["value"]), 0.01 * s)
    r = _r5(book)
    before, after = (price_equity.creditgrades_spread(x, dps[0], sigma, r) for x in (s, s_after))
    if not (before > 0 and after > 0) or math.isnan(before) or math.isnan(after):
        return None, {}, missing + [f"entity {entity_id}: CreditGrades spread undefined"]
    return after / before, {"sharePrice": round(s, 2), "sharePriceAfter": round(s_after, 2), "closeDate": d.isoformat(),
                            "equityVol": round(sigma, 3), "lossB": round(loss_usd / 1e9, 3)}, missing


def _rating_of(rs: dict[str, AgencyRating]) -> AgencyRating | None:
    return rs.get("S&P") or rs.get("Fitch") or rs.get("Moody's")


def rating_beta(conn: Conn, book: Book) -> tuple[float | None, list[dict[str, Any]]]:
    pts = []
    for e in sorted(book.curves):
        lam = book.hazard(e)
        ar = _rating_of(ratings(conn, book.day, e))
        n = notch(ar.rating) if ar else None
        if lam and n is not None:
            pts.append({"entity": e, "rating": ar.rating, "notch": n, "hazard": round(lam, 5)})  # type: ignore[union-attr]
    if len({p["notch"] for p in pts}) < 2:
        return None, pts
    slope = float(np.polyfit([p["notch"] for p in pts], [math.log(p["hazard"]) for p in pts], 1)[0])
    return slope, pts


def severe_spread_bp(conn: Conn, day: date, rating_notch: int | None) -> tuple[float | None, str | None]:
    if rating_notch is None:
        return None, None
    bucket = next((b for ceiling, b in BUCKETS if rating_notch <= ceiling), "ccc")
    sid = f"ice_bofa_us_{bucket}_oas"
    rows = pit.fact_rows(conn, day, "series", [sid], ["value"])
    peak = [float(r["value"]) for r in rows if r["valid_from"].year == 2022 and isinstance(r["value"], int | float)]
    now = [r for r in rows if isinstance(r["value"], int | float)]
    if not peak or not now:
        return None, sid
    return round((max(peak) - float(now[0]["value"])) * 100.0, 1), sid  # percent -> bp


def _sites_of(conn: Conn, entity_id: str, roles: tuple[str, ...]) -> list[str]:
    rows = conn.execute("SELECT site_slug FROM chain_party WHERE entity_id = %s AND role = ANY(%s) ORDER BY 1",
                        (entity_id, list(roles))).fetchall()
    return [r["site_slug"] for r in rows]


def _pv(edge: graph.Edge, day: date, rate: float) -> tuple[float | None, str]:
    sched = [(y, usd) for y, usd, _ in edge.schedule() if y >= day.year]
    if sched:
        return sum(usd / (1 + rate) ** max(y - day.year, 0) for y, usd in sched), "remaining schedule, PV"
    if edge.total is not None:
        return edge.total.usd, "stated total (no schedule), undiscounted"
    return None, "unsized"


def end_user_funding_miss(conn: Conn, book: Book, params: dict[str, Any]) -> Scenario:
    eu, cut = str(params["endUser"]), float(params["cutPct"]) / 100.0
    sc = Scenario("end_user_funding_miss", f"{eu} cuts {params['cutPct']:g}% of its contracts")
    rate = book.ctx.book.curve(book.day).par_yield(10.0) / 100.0  # type: ignore[union-attr]
    payees: dict[str, list[dict[str, Any]]] = {}
    for e in graph.load_edges(conn, book.day):
        if e.payer != eu or not e.pays:
            continue
        pv, how = _pv(e, book.day, rate)
        if pv is None:
            sc.missing.append(f"obligation {e.id}: no amount to cut ({', '.join(e.missing)})")
            continue
        payees.setdefault(e.payee, []).append({"edge": e.id, "lossB": round(cut * pv / 1e9, 3), "how": how})
    for payee, items in sorted(payees.items()):
        mult, info, miss = equity_multiplier(conn, book, payee, sum(i["lossB"] for i in items) * 1e9)
        sc.missing += miss
        if mult is not None:
            sc.shock.hazard_mult[payee] = mult
            sc.tenants.append(payee)
        sc.basis.setdefault("payees", {})[payee] = {"edges": items, **info,
                                                    "hazardMultiplier": None if mult is None else round(mult, 4)}
    for slug in sorted(set(_sites_of(conn, eu, ("end_user", "tenant")))):
        sc.sites[slug] = SiteHit(slug, "E2", f"End user {eu} cuts {params['cutPct']:g}% of its contracts", "red")
    for t in sc.tenants:
        for slug in _sites_of(conn, t, ("tenant",)):
            sc.sites.setdefault(slug, SiteHit(slug, None, f"Tenant {t} loses revenue from {eu}", "amber"))
    sc.basis["discountRate"] = round(rate, 5)
    return sc


def tenant_downgrade(conn: Conn, book: Book, params: dict[str, Any]) -> Scenario:
    tenant, target = str(params["tenant"]), str(params["rating"])
    sc = Scenario("tenant_downgrade", f"{tenant} cut to {target}")
    now = _rating_of(ratings(conn, book.day, tenant))
    beta, pts = rating_beta(conn, book)
    sc.basis.update({"ratingNow": now.rating if now else None, "target": target, "beta": beta, "points": pts})
    n_now, n_target = notch(now.rating) if now else None, notch(target)
    if n_now is None:
        sc.missing.append(f"entity {tenant}: current agency rating")
    if n_target is None:
        sc.missing.append(f"rating {target!r} is not on the S&P / Moody's scale")
    if beta is None:
        sc.missing.append("rating-to-intensity slope: fewer than two priced issuers with different ratings")
    if None not in (n_now, n_target, beta):
        sc.shock.hazard_mult[tenant] = math.exp(beta * (n_target - n_now))  # type: ignore[operator]
        sc.basis["hazardMultiplier"] = round(sc.shock.hazard_mult[tenant], 4)
    sc.tenants.append(tenant)
    for slug in _sites_of(conn, tenant, ("tenant",)):
        sc.sites[slug] = SiteHit(slug, "T1", f"Tenant {tenant} cut to {target}", "amber")
    return sc


def power_delay(conn: Conn, book: Book, params: dict[str, Any]) -> Scenario:
    months = int(round(float(params["months"])))
    sc = Scenario("power_delay", f"Power delayed {months} months")
    day = book.day
    slugs = [r["slug"] for r in conn.execute("SELECT slug FROM site WHERE in_v1 ORDER BY slug").fetchall()]
    history = load_history(conn, day)
    history_hash = hash_parts(canonical_json([r.doc() for r in history.records]))
    for site in load_sites(conn, day, slugs):
        milestones, _ = build_milestones(site)
        waiting = [m for m in milestones if m.key in POWER_KEYS and m.applicable and not m.is_done]
        if not waiting:
            continue
        seed = int(hash_parts(CUSHION_MODEL, CUSHION_VERSION, canonical_json(site.fingerprint()), history_hash)[:15], 16)
        shifted = copy.deepcopy(milestones)
        moved = []
        for m in shifted:
            if m.key in POWER_KEYS and m.applicable and not m.is_done and m.planned is not None:
                m.planned = replace(m.planned, lo=m.planned.lo + months, hi=m.planned.hi + months)
                moved.append(m.key)
        if not moved:
            sc.missing.append(f"site {site.slug}: power milestones not dated (cannot move)")
            continue
        before = cout.projection_doc(site, simulate(milestones, day, history, site.slug, seed), None)
        after = cout.projection_doc(site, simulate(shifted, day, history, site.slug, seed), None)
        c0, c1 = ((p.get("cushionMonths") or {}).get("value") for p in (before, after))
        slip = _months_between(before.get("p50"), after.get("p50"))
        if slip is None:
            sc.missing.append(f"site {site.slug}: no rent-start projection")
            continue
        sc.shock.completion_months[site.slug] = slip
        rule = "M2" if (c0 is not None and c1 is not None and c0 >= 0 > c1) else ("M1" if slip >= 3 else None)
        sev = "red" if rule == "M2" else ("amber" if slip >= 3 else ("green" if slip else "none"))
        sc.sites[site.slug] = SiteHit(site.slug, rule, f"Power {', '.join(moved)} {months} months later: rent start "
                                      f"P50 {before.get('p50')} -> {after.get('p50')}", sev, (c0, c1))
    return sc


def _months_between(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


BUILDERS = {"end_user_funding_miss": end_user_funding_miss, "tenant_downgrade": tenant_downgrade,
            "power_delay": power_delay}


def build(conn: Conn, book: Book, scenario: str, params: dict[str, Any], severity: str) -> Scenario:
    full = {**DEFAULTS[scenario], **{k: v for k, v in params.items() if v not in (None, "")}}
    sc = BUILDERS[scenario](conn, book, full)
    sc.basis["params"] = full
    if severity == "severe":
        ref = sc.tenants[0] if sc.tenants else None
        ar = _rating_of(ratings(conn, book.day, ref)) if ref else None
        widen, sid = severe_spread_bp(conn, book.day, notch(ar.rating) if ar else None)
        sc.basis["severeSeries"] = sid
        if widen is None:
            sc.missing.append(f"severe: series {sid or 'ice_bofa_us_<rating>_oas'} 2022 peak (not loaded)")
            sc.shock.spread_bp = float("nan")
        else:
            sc.shock.spread_bp = widen
            sc.basis["severeSpreadBp"] = widen
    return sc
