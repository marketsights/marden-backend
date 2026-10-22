"""What a contract walk does to the debt its rent pays (stress v0.2, spec 09 "End user misses its funding plan").

When an end user cuts `share` of its contracts, the rent on the sites (and GPU contracts) it pays stops for that share
at the walk date, and lenders are repaid from one of two paths, each priced by the price engine's own inputs:

- re-lease (odds RELEASE_ODDS): a new tenant signs after RELEASE_MONTHS without rent, at a rent RENT_HAIRCUT lower;
  rent coverage is not disclosed, so the haircut is passed to the debt in full: value = price now x (1 - haircut),
  discounted over the months without rent on the day's Treasury curve, and never below the no-re-lease value (the
  debt stays secured by the same collateral);
- no re-lease: a guarantee filed for the debt (a `lease_guarantee` site fact or a `parent_guarantee_scope`
  agreement term, tag F) keeps the debt paid unless the guarantor defaults: the debt re-priced on the guarantor's
  intensity (shocked when the scenario shocks it) with the same recovery and spread; without a filed guarantee the
  lenders take the collateral: the recovery the price engine uses for the instrument (the recovery model's
  waterfall: land and powered shell x the share of the works finished, GPU resale on its depreciation path less
  the resale haircut, after claims ahead; else the engine's default assumption) at the walk date, paid
  WORKOUT_MONTHS later (discounted on the day's Treasury curve).
- a filed debt service reserve (`reserves` site fact naming a "Debt Service Reserve" amount) is added to the
  no-re-lease value, up to par.

price after = (1 - share) x the engine's re-price under the scenario's other shocks + share x walk value, where walk
value = odds x re-lease value + (1 - odds) x no-re-lease value, never above the price now. Every input is in the
row's `walk` (the hover): the assumptions tagged M, the guarantee and reserve with their filed sources. The
assumptions are uncalibrated ('model v0'): there is no history of AI data-center leases being walked to fit them on.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.models import price_chain, price_facility
from marden.models.price_bonds import RECOVERY as BOND_RECOVERY
from marden.models.price_bonds import clean_price
from marden.models.stress_price import Book, Shock

RELEASE_ODDS = 0.35  # odds a new tenant signs for the walked space (assumption, model v0)
RELEASE_MONTHS = 12  # months without rent before it does (assumption, model v0)
RENT_HAIRCUT = 0.30  # the new rent is this much lower (assumption, model v0)
WORKOUT_MONTHS = 18  # months from the walk until lenders are paid from the collateral (assumption, model v0)
_RESERVE = re.compile(r"Debt Service Reserve[^$]*\$\s*([\d.]+)\s*(m|mm|million|bn|billion)\b", re.I)


@dataclass
class Walk:
    party: str
    party_name: str
    share: float  # share of its contracts the party walks from, 0..1
    sites: set[str] = field(default_factory=set)  # sites whose rent the party pays (end user or tenant)


def _recovery_doc(ch: Any, rec0: float) -> dict[str, Any]:
    """How the price engine describes the recovery it used (price_chain.recovery_doc, price v0.2: the recovery
    model's waterfall); an engine without that description gives the number only."""
    describe = getattr(price_chain, "recovery_doc", None)
    version = getattr(price_facility, "_version", None)
    if describe is None or version is None:
        return {"cents": round(rec0 * 100, 1)}
    return dict(describe(ch, rec0, version(), BOND_RECOVERY))


def _m(value: Any, day: date, label: str) -> dict[str, Any]:
    return {
        "value": value,
        "tag": "M",
        "asOf": day.isoformat(),
        "sourceName": "Marden stress model",
        "methodVersion": "stress v0.2 (model v0)",
        "label": label,
    }


def assumptions(day: date) -> dict[str, Any]:
    """The walk assumptions, each a Sourced value tagged M."""
    return {
        "walkDate": _m(day.isoformat(), day, "Rent stops on the run day"),
        "reLeaseOdds": _m(RELEASE_ODDS, day, "Odds a new tenant signs"),
        "reLeaseMonths": _m(RELEASE_MONTHS, day, "Months without rent before a new lease"),
        "rentHaircutPct": _m(round(RENT_HAIRCUT * 100), day, "New rent below the old, %"),
        "workoutMonths": _m(WORKOUT_MONTHS, day, "Months until lenders are paid from the collateral"),
    }


def _fact(conn: Conn, day: date, subject_type: str, subject_id: str | None, field_: str) -> dict[str, Any] | None:
    if not subject_id:
        return None
    row = conn.execute(
        """SELECT value, tag, source_name, source_url, valid_from FROM fact WHERE status = 'published'
             AND subject_type = %s AND subject_id = %s AND field = %s AND tag = 'F' AND valid_from <= %s
             AND (valid_to IS NULL OR valid_to > %s) ORDER BY valid_from DESC, id DESC LIMIT 1""",
        (subject_type, subject_id, field_, day, day),
    ).fetchone()
    return dict(row) if row else None


def _entity_named(conn: Conn, name: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM entity WHERE lower(name) = lower(%s) OR lower(name) = lower(%s) ORDER BY id LIMIT 1",
        (name, re.sub(r",?\s+(Inc|Corp|Corporation|Ltd|LLC)\.?$", "", name, flags=re.I)),
    ).fetchone()
    return row["id"] if row else None


def filed_guarantee(conn: Conn, day: date, iid: str, site: str | None, obligor: str | None) -> dict[str, Any] | None:
    """A guarantee filed for the debt (tag F only; a reported backstop does not count): who and where it is filed.
    The instrument's own agreement term (with its section) first, else the site's `lease_guarantee`."""
    row = conn.execute(
        """SELECT value, tag, source_name, source_url, valid_from FROM fact WHERE status = 'published'
             AND subject_type = 'instrument' AND subject_id = %s AND field = 'agreement_term' AND tag = 'F'
             AND value->>'type' = 'parent_guarantee_scope' AND valid_from <= %s ORDER BY valid_from DESC LIMIT 1""",
        (iid, day),
    ).fetchone()
    site_src = _fact(conn, day, "site", site, "lease_guarantee")
    src = dict(row) if row else site_src
    if src is None:
        return None
    named = site_src["value"] if site_src and isinstance(site_src["value"], str) else None
    guarantor = (_entity_named(conn, named) if named else None) or obligor
    name_row = conn.execute("SELECT name FROM entity WHERE id = %s", (guarantor,)).fetchone() if guarantor else None
    term = src["value"] if row else {}
    section = f"{term.get('agreement', '')} {term.get('section', '')}".strip() or None
    return {
        "guarantor": guarantor,
        "name": named or (name_row["name"] if name_row else guarantor),
        "section": section,
        "tag": "F",
        "sourceName": src["source_name"],
        "sourceUrl": src["source_url"],
        "asOf": src["valid_from"].isoformat(),
    }


def filed_reserve(conn: Conn, day: date, site: str | None) -> dict[str, Any] | None:
    """A filed debt service reserve with a stated amount ($), else None."""
    src = _fact(conn, day, "site", site, "reserves")
    got = _RESERVE.search(src["value"]) if src and isinstance(src["value"], str) else None
    if src is None or got is None:
        return None
    usd = float(got[1]) * (1e9 if got[2].lower().startswith("b") else 1e6)
    return {
        "usd": usd,
        "text": src["value"],
        "tag": "F",
        "sourceName": src["source_name"],
        "sourceUrl": src["source_url"],
        "asOf": src["valid_from"].isoformat(),
    }


def walk_value(
    conn: Conn, book: Book, iid: str, walk: Walk, shock: Shock, size_usd: float | None, after_engine: float | None
) -> tuple[float | None, dict[str, Any]]:
    """(price after, waterfall) for one instrument whose rent the walking party pays."""
    day, st = book.day, book.now_state(iid)
    doc: dict[str, Any] = {"party": walk.party_name, "sharePct": round(walk.share * 100), **assumptions(day)}
    now = st["now"]
    curve = book.ctx.book.curve(day)
    if now is None or curve is None or math.isnan(shock.spread_bp):
        doc["missing"] = "price now or the severe spread n/d: the walk cannot be priced"
        return None, doc
    ch = book.chains.get(iid)
    spread = st["spread"] + shock.spread_bp / 1e4
    relet = now * (1 - RENT_HAIRCUT) * curve.df(RELEASE_MONTHS / 12)
    rec0 = st["recoveryNow"]
    rdoc = _recovery_doc(ch, rec0)
    no_relet = rec0 * 100 * curve.df(WORKOUT_MONTHS / 12)  # paid from the collateral after the workout
    doc["recovery"] = {
        "cents": round(rec0 * 100, 1),
        "afterWorkoutCents": round(no_relet, 1),
        "label": rdoc.get("label"),
        "method": rdoc.get("method"),
        "basis": rdoc.get("basis"),
        "why": rdoc.get("why"),
        "source": rdoc.get("source"),
        **({"pathCents": rdoc["pathCents"][:6]} if rdoc.get("pathCents") else {}),
        **({"gpuHalfLifeYears": rdoc["gpuHalfLifeYears"]} if rdoc.get("gpuHalfLifeYears") else {}),
        **({"inputs": rdoc["inputs"]} if rdoc.get("inputs") else {}),
    }
    inst = book.insts.get(iid)
    site = inst.site_slug if inst else None
    guar = filed_guarantee(conn, day, iid, site, ch.obligor if ch else None)
    if guar is not None:
        g = guar["guarantor"]
        lam = book.hazard(g) if g else None
        if lam is None:
            guar["missing"] = f"{guar['name']}: no bond-implied default intensity, so the guarantee is not priced"
        else:
            mult = shock.hazard_mult.get(g, 1.0)
            guar["shocked"] = g in shock.hazard_mult
            if not guar["shocked"]:
                guar["note"] = f"priced at {guar['name']}'s credit today: this scenario does not move it"
            guar["hazard"] = round(lam * mult, 5)
            guar["valueCents"] = round(clean_price(st["f_now"], curve, lam * mult, st["recovery"], spread), 1)
            no_relet = max(no_relet, guar["valueCents"])
        doc["guarantee"] = guar
    reserve = filed_reserve(conn, day, site)
    if reserve is not None and size_usd:
        reserve["cents"] = round(min(100.0, reserve["usd"] / size_usd * 100), 1)
        no_relet = min(100.0, no_relet + reserve["cents"])
        doc["reserve"] = reserve
    relet = max(relet, no_relet)  # the debt stays secured: with a new tenant lenders still hold the collateral
    # A walk never leaves the debt worth more than today (a collateral worth par is still paid out later).
    value = min(RELEASE_ODDS * relet + (1 - RELEASE_ODDS) * no_relet, now)
    base = after_engine if after_engine is not None else now
    after = (1 - walk.share) * base + walk.share * value
    doc.update(
        {
            "priceNow": now,
            "reLeaseCents": round(relet, 1),
            "noReLeaseCents": round(no_relet, 1),
            "walkCents": round(value, 1),
            "otherShareCents": round(base, 1),
            "priceAfter": round(after, 3),
        }
    )
    return round(after, 3), doc
