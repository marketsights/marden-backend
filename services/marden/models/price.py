"""Marden price engine (docs/methods/pricing-v0.1.md): bond-implied CDS, tier curves, facility prices.

    cd services && uv run python -m marden model price [--as-of YYYY-MM-DD] [--instrument ID ...]

One run as of a day (default today, US Eastern; a past day is an honest backtest: every input is what
was public that day, marden.models.pit / price_marks) writes, under model_run 'price' v0.1:

- model 'cds', subject entity: the 'Marden bond-implied CDS' of every issuer with straight bonds
  (price_issuer): {label, spreadBp, upfrontPct (500bp coupon), hazard, maturity, years, band, raw (the
  latest bond cross-section, unfiltered), equity {ticker, loading, pairs}, missing, methodVersion};
- model 'price', subject instrument (every V1 instrument and every bond of the V1 chains' issuers):
  {price, mardenPrice (InstrumentRow.mardenPrice: Sourced<number>), spreadBp, yieldPct, recovery, change1w,
  inputs: [...], band: {low, high}, missing: [...],
  basis} (price_facility); plus price_obs kind 'marden', tag M, method_version 'price v0.1';
- model 'curve', subject series 'all': CurveTier[] (types.ts) from the tier fit (price_tiers) with the
  priced instruments as points.

A missing input gives null fields and a 'missing' entry, never a guess. Ranges and bands are not yet
calibrated and carry the 'model v0' label (spec 00 "Model labels").
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from marden.core.db import Conn
from marden.core.jsonutil import canonical_json, hash_parts
from marden.models import pit, price_marks, price_signals, price_tiers, runs
from marden.models.price_chain import Chain, gpu_half_lives, resolve
from marden.models.price_curve import CurveBook, load_curves, year_frac
from marden.models.price_facility import Ctx, price
from marden.models.price_issuer import IssuerCurve, build, quote, raw_quote, straight_bonds
from marden.models.price_terms import Instrument, attach, load_comps, load_instruments, note_terms
from marden.models.price_tiers import tier_of
from marden.readmodels.context import business_today

log = logging.getLogger(__name__)

MODEL, VERSION = "price", "price v0.1"
METHOD_URL = "docs/methods/pricing-v0.1.md"
CDS_LABEL = "Marden bond-implied CDS"
PRICE_LABEL = "Marden model price"
HISTORY_START = date(2025, 1, 1)  # the Treasury curve archive starts 2025-01-02


TARGETS_SQL = """WITH v1 AS (SELECT id, issuer_entity_id, site_slug FROM instrument WHERE in_v1),
        ents AS (SELECT issuer_entity_id e FROM v1 UNION SELECT cp.entity_id FROM chain_party cp
                 JOIN site s ON s.slug = cp.site_slug WHERE s.in_v1 AND cp.role = 'tenant'),
        t AS (SELECT id FROM v1 UNION
              SELECT i.id FROM instrument i JOIN ents ON ents.e = i.issuer_entity_id WHERE i.kind = 'bond')"""


def targets(conn: Conn) -> list[str]:
    """V1 instruments plus every bond of their issuers and of the V1 sites' tenants."""
    return [r["id"] for r in conn.execute(TARGETS_SQL + " SELECT id FROM t ORDER BY 1").fetchall()]


def is_target(conn: Conn, instrument_id: str) -> bool:
    return conn.execute(TARGETS_SQL + " SELECT 1 FROM t WHERE id = %s", (instrument_id,)).fetchone() is not None


def instrument_tier(inst: Instrument, ch: Chain | None) -> str | None:
    """The methodology tier an instrument plots in (its issue comp's tier, else by kind and chain)."""
    if inst.comp is not None:
        got = tier_of(inst.comp["type_code"], inst.comp["tier"], inst.comp["rating"],
                      f"{inst.comp.get('name') or ''} {inst.comp['rate']}")
        if got:
            return got
    if inst.kind == "construction_loan":
        return "Construction loan"
    if inst.kind in ("ddtl", "term_loan", "lease"):
        return "Contract-backed loan" if isinstance(inst.value("tenants"), str) else "Junior chip paper"
    if inst.kind == "bond" and ch is not None:
        if ch.guarantor or (ch.tenant_label and "(" in ch.tenant_label):
            return "Backstopped senior"
        rating = inst.value("rating")
        notch = price_tiers.rating_notch(rating if isinstance(rating, str) else None)
        if notch is not None and notch <= price_tiers.BBB_MINUS:
            return "IG project bond"
        return "Neocloud bond"
    return None


def cds_issuers(conn: Conn, by_issuer: dict[str, list[Instrument]], curves: dict[str, IssuerCurve]) -> set[str]:
    """Issuers whose curve is a corporate CDS (a listed company, or two or more straight bonds)."""
    listed = {r["id"] for r in conn.execute("SELECT id FROM entity WHERE ticker IS NOT NULL").fetchall()}
    return {e for e, bonds in by_issuer.items() if curves[e].sections and (e in listed or len(bonds) >= 2)}


def cds_doc(cur: IssuerCurve, book: CurveBook, as_of: date) -> dict[str, Any]:
    q = quote(cur, as_of, book)
    sec = cur.latest_section(as_of)
    raw = raw_quote(sec[0], sec[1], book) if sec else None
    out: dict[str, Any] = {
        "label": CDS_LABEL, "methodVersion": VERSION, "sourceUrl": METHOD_URL,
        "spreadBp": None, "upfrontPct": None, "hazard": None, "maturity": None, "years": None, "band": None,
        "raw": raw, "bonds": cur.bonds,
        "equity": {"ticker": cur.ticker, "loading": round(cur.loading, 3), "pairs": cur.loading_pairs,
                   "prior": None if cur.loading_prior is None else round(cur.loading_prior, 3),
                   "model": "CreditGrades (L=0.5, lambda=0.3, R=0.4); loading prior = 0.018 per unit equity log "
                            "return at the issuer's CreditGrades elasticity"},
        "assumptions": ["recovery 40%", "flat intensity per bond on the Treasury par curve",
                        "interpolated by maturity to the standard CDS maturity, never averaged",
                        "Treasury discounting (SOFR swaps would add ~30-35bp)",
                        "equity (CreditGrades) moves only between bond prints"],
        "missing": list(cur.missing),
    }
    if q is not None:
        out.update({"spreadBp": round(q["spreadBp"], 1), "upfrontPct": round(q["upfrontPct"], 3),
                    "hazard": round(q["hazard"], 6), "maturity": q["maturity"], "years": round(q["years"], 3),
                    "band": {"low": round(q["band"]["low"], 1), "high": round(q["band"]["high"], 1),
                             "level": 0.8, "label": "model v0"}})
        if raw is not None:
            out["rawAgeDays"] = (as_of - date.fromisoformat(raw["date"])).days
    else:
        out["missing"].append(f"entity {cur.entity_id}: no bond-implied level public by {as_of}")
    return out


def uncovered(conn: Conn, corp: set[str], straight: set[str]) -> dict[str, str]:
    """Issuers the engine is asked about but cannot give a bond-implied level: convert-only issuers and the
    V1 sites' tenants without straight bonds in the instrument table."""
    out: dict[str, str] = {}
    with_bonds = {r["e"] for r in conn.execute(
        "SELECT DISTINCT issuer_entity_id e FROM instrument WHERE kind = 'bond'").fetchall()}
    for r in conn.execute("SELECT DISTINCT issuer_entity_id e FROM instrument WHERE kind = 'convert' "
                          "AND issuer_entity_id IS NOT NULL").fetchall():
        if r["e"] not in corp and r["e"] not in straight and r["e"] not in with_bonds:
            out[r["e"]] = "convertible notes only: the equity option is not modelled, so no bond-implied level"
    for r in conn.execute("""SELECT DISTINCT cp.entity_id e FROM chain_party cp JOIN site s ON s.slug = cp.site_slug
                              WHERE s.in_v1 AND cp.role = 'tenant' AND cp.entity_id IS NOT NULL""").fetchall():
        if r["e"] not in corp:
            out.setdefault(r["e"], "no straight bonds of this entity in the instrument table")
    return out


def null_cds(entity: str, why: str) -> dict[str, Any]:
    return {"label": CDS_LABEL, "methodVersion": VERSION, "sourceUrl": METHOD_URL, "spreadBp": None,
            "upfrontPct": None, "hazard": None, "maturity": None, "years": None, "band": None, "raw": None,
            "bonds": [], "equity": None, "missing": [f"entity {entity}: {why}"]}


def _inputs(inst: Instrument, out: dict[str, Any], ch: Chain | None, curves: dict[str, IssuerCurve],
            cds: dict[str, dict[str, Any]], book: CurveBook, as_of: date) -> list[dict[str, Any]]:
    """The price output's inputs list (each a sourced value the read models can show)."""
    items: list[dict[str, Any]] = []

    def add(key: str, label: str, value: Any, unit: str, tag: str = "M", source: str | None = None) -> None:
        if value is not None:
            items.append({"key": key, "label": label, "value": value, "unit": unit, "tag": tag,
                          "asOf": as_of.isoformat(), **({"methodVersion": VERSION} if tag == "M" else {}),
                          **({"sourceName": source} if source else {})})

    issuer = inst.issuer if inst.issuer in cds else None
    if issuer:
        add("issuerCdsBp", f"{issuer} bond-implied CDS", cds[issuer]["spreadBp"], "bp", source=CDS_LABEL)
    if ch and ch.tenant and ch.tenant in cds:
        add("tenantCdsBp", f"{ch.tenant} bond-implied CDS", cds[ch.tenant]["spreadBp"], "bp", source=CDS_LABEL)
    if ch and ch.obligor and ch.obligor in cds and ch.obligor != issuer:
        add("obligorCdsBp", f"{ch.obligor} bond-implied CDS", cds[ch.obligor]["spreadBp"], "bp", source=CDS_LABEL)
    basis = out.get("basis") or {}
    today = basis.get("today") or {}
    if today.get("denials"):
        add("permitDenials", "Power-permit denials (x1.75 each, cap 6x)", today["denials"], "count")
    if today.get("delays"):
        add("powerDelays", "Confirmed power delays of 3+ months (+0.25 FM odds each)", today["delays"], "count")
    cal = basis.get("calibration") or {}
    add("liquidityPremiumBp", "Liquidity premium (solved at calibration, held)", cal.get("lpBp"), "bp")
    add("residualBp", "Residual spread from observations (Kalman)", basis.get("residualBp"), "bp")
    curve = book.curve(as_of)
    if curve is not None and inst.terms is not None:
        years = year_frac(as_of, inst.terms.maturity)
        add("treasuryYieldPct", f"Treasury par yield at {years:.1f}y", round(curve.par_yield(years), 3), "%", "F",
            "U.S. Treasury")
    if inst.terms is not None and inst.terms.floating:
        add("sofrPct", "SOFR", book.sofr_on(as_of), "%", "F", "Federal Reserve Bank of New York")
    odds = basis.get("defaultOddsToMaturity")
    if odds is not None and inst.terms is not None:
        t = max(year_frac(as_of, inst.terms.maturity), 0.0)
        add("impliedDefaultOdds", f"Default odds to maturity ({t:.1f}y, risk-neutral, model v0)", odds, "share")
    return items


def _null(missing: list[str]) -> dict[str, Any]:
    return {"price": None, "spreadBp": None, "yieldPct": None, "recovery": None, "change1w": None, "band": None,
            "inputs": [], "missing": missing}


def _unsupported(inst: Instrument) -> str | None:
    if inst.kind == "convert":
        return f"instrument {inst.id}: convertible note (the equity option is not modelled in v0.1)"
    if not inst.usd:
        return f"instrument {inst.id}: non-USD note (no foreign-currency discount curve in v0.1)"
    return None


def run_day(conn: Conn, day: date, ids: Sequence[str] | None = None, *,
            exclude_dealer: bool = False) -> dict[str, Any]:
    """Compute every output of one day (no writes)."""
    book = load_curves(conn, day, since=HISTORY_START)
    if book.curve(day) is None:
        raise ValueError(f"no Treasury curve public on or within a week before {day}")
    insts = load_instruments(conn, targets(conn))  # the whole universe: issuer curves need every bond
    with pit.unaudited():  # issue comps are dated their issue day; the ones used are noted by note_terms
        attach(insts, load_comps(conn, business_today()))
    priced = set(ids) if ids else set(insts)
    by_issuer = straight_bonds(insts)
    obs = price_marks.load(conn, day, list(insts))
    tickers = {r["id"]: r["ticker"] for r in conn.execute("SELECT id, ticker FROM entity").fetchall()}
    curves = {e: build(conn, day, e, bonds, obs, book, tickers.get(e)) for e, bonds in sorted(by_issuer.items())}
    corp = cds_issuers(conn, by_issuer, curves)
    tiers = price_tiers.fit(conn, day, load_comps(conn, day), book)
    slugs = sorted({i.site_slug for i in insts.values() if i.site_slug})
    ctx = Ctx(conn, day, book, {e: c for e, c in curves.items() if e in corp}, tiers,
              {s: price_signals.from_records(conn, day, s) for s in slugs}, exclude_dealer)
    cds = {e: cds_doc(curves[e], book, day) for e in sorted(corp)}
    for e, why in uncovered(conn, corp, set(by_issuer)).items():
        cds.setdefault(e, null_cds(e, why))
    halves = gpu_half_lives(conn, day)
    prices: dict[str, dict[str, Any]] = {}
    points: dict[str, list[dict[str, Any]]] = {}
    for iid, inst in sorted(insts.items()):
        if iid not in priced:
            continue
        issuer_bond = inst.issuer in corp and any(b.id == iid for b in by_issuer.get(inst.issuer or "", []))
        ch = None if issuer_bond else resolve(conn, inst, day, lambda e: e in corp, halves)
        unsupported = _unsupported(inst)
        try:
            out = _null([unsupported]) if unsupported else price(ctx, inst, ch, obs.get(iid, []))
        except (ValueError, ZeroDivisionError) as exc:  # one instrument's bad terms never stop the run
            out = _null([f"instrument {iid}: {type(exc).__name__}: {exc}"])
        out.pop("_week_ago_price", None)
        out["inputs"] = _inputs(inst, out, ch, curves, cds, book, day)
        out.update({"label": PRICE_LABEL, "methodVersion": VERSION, "sourceUrl": METHOD_URL,
                    "tier": None if issuer_bond else instrument_tier(inst, ch)})
        # InstrumentRow.mardenPrice (types.ts), ready for the builders to drop in (null = n/d)
        out["mardenPrice"] = {"value": out["price"], "tag": "M", "asOf": day.isoformat(), "sourceName": PRICE_LABEL,
                              "sourceUrl": METHOD_URL, "methodVersion": VERSION}
        prices[iid] = out
        if out["price"] is not None and out["tier"] and out["yieldPct"] is not None and inst.terms:
            points.setdefault(out["tier"], []).append({"years": round(year_frac(day, inst.terms.maturity), 2),
                                                       "yieldPct": out["yieldPct"], "instrumentId": iid})
    for iid, inst in insts.items():  # the static terms behind every price and issuer curve (input audit)
        seen = obs.get(iid) or []
        if (iid in prices and prices[iid].get("price") is not None) or (inst.issuer in corp and seen):
            note_terms(inst, day, min((o.public for o in seen), default=None))
    curve_tiers = price_tiers.curve_doc(tiers, book, points)
    return {"as_of": day, "cds": cds, "prices": prices, "curve": curve_tiers, "fit": price_tiers.fit_doc(tiers),
            "fit_missing": tiers.missing, "book": book, "curves": curves}


def week_change(conn: Conn, iid: str, day: date, px: float | None) -> float | None:
    """change1w against the stored output exactly 7 days earlier (like the cushion model's deltaWeek)."""
    if px is None:
        return None
    prior = runs.output_on(conn, MODEL, "instrument", iid, day - timedelta(days=7))
    old = prior["output"].get("price") if prior is not None and isinstance(prior["output"], dict) else None
    return None if not isinstance(old, int | float) else round(px - float(old), 3)


def inputs_hash(result: dict[str, Any]) -> str:
    return hash_parts(MODEL, VERSION, canonical_json({k: v.get("price") for k, v in result["prices"].items()}),
                      canonical_json({k: v.get("spreadBp") for k, v in result["cds"].items()}))

