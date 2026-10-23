"""The recovery behind the Marden price, in plain words (InstrumentDetail.recovery / recoveryDetail; the site
debt card's DebtBasis.recovery lines and sensitivity).

Read from the price output (price v0.2, marden.models.price_chain / price_facility): `recoveryBasis`
('site_waterfall' when the price used the `recovery` model's central estimate, 'default_assumption'
otherwise), `recoveryInputs` (method, key inputs, the recovery output it came from) and
`recoverySensitivity` (price change per 10c of recovery). Without a price recovery (not priced, or read
off the tier curve) the tile falls back to the recovery model's own number, then to the research facts.

- tile: '52c (site collateral model)' / '48c (issuer balance-sheet waterfall)' / '55c assumed (no site
  collateral model yet)', sourced to the recovery model (M) when derived, else to the price model (M).
- detail: {basis, headline, method: Sourced<string> (method page), lines: [{label, value: Sourced<string>}],
  sensitivity: string | null}: building or GPU value, debt ahead, claims at the rank, lien (the seniority
  fact's own source when filed), the range, and '±10c recovery moves the price by ±X points'.
- issuer waterfall (corporate notes, converts, issuer-level loans; recovery v0.3): `waterfall`, the short
  waterfall of the issuer's balance sheet (assets after haircuts, less secured claims, left for unsecured,
  the recovery), each step tagged M with its haircuts in the note, plus the filing it is read from (F) and
  how the issuer curve is calibrated. No model version appears in the text (the source tags carry it).
  An issuer without a balance sheet on file says so and why the stated 40% applies.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from marden.core.db import Conn, Row
from marden.models.recovery_issuer import money
from marden.readmodels.context import known_facts
from marden.readmodels.views.instrument_inputs import fact_sourced, method_version, model_sourced, num, output_field
from marden.readmodels.views.instrument_text import DASH
from marden.readmodels.views.recovery_text import ISSUER_MODEL, NO_NOTE_MODEL, SITE_MODEL, price_recovery_text
from marden.readmodels.views.sites_recovery import money_b, reason_nd

Doc = dict[str, Any]
RECOVERY_DOC = "docs/methods/recovery-v0.1.md"
PRICING_DOC = "docs/methods/pricing-v0.1.md"
TIMES = "\u00d7"
METHOD_NAME = {"powered_shell": "Powered-shell waterfall", "gpu_collateral": "GPU collateral waterfall",
               "issuer_waterfall": "Issuer balance-sheet waterfall"}
ISSUER = "issuer_waterfall"


def _priced(price_out: Row | None) -> dict[str, Any] | None:
    body = price_out["output"] if price_out is not None and isinstance(price_out["output"], dict) else None
    return body if body is not None and num(body.get("recovery")) is not None else None


def _rec_meta(rec_out: Row | None, source: dict[str, Any] | None = None) -> Doc:
    """Sourced metadata of the recovery model's value (the price's copy of it first)."""
    if source and source.get("asOf"):
        return {k: source[k] for k in ("tag", "asOf", "sourceName", "sourceUrl", "methodVersion") if source.get(k)} | {
            "tag": source.get("tag") or "M"}
    meta = output_field(rec_out, "recovery") or {}
    got = model_sourced(rec_out, {**meta, "value": None}) if rec_out is not None else None
    return {k: v for k, v in (got or {"tag": "M"}).items() if k != "value"}


def _price_meta(price_out: Row) -> Doc:
    got = model_sourced(price_out, "x") or {}
    return {k: v for k, v in got.items() if k != "value"}


def _rec_number(rec_out: Row | None) -> str | None:
    """The recovery model's own central number ('100c', '62-74c' when it gives only a range)."""
    basis = output_field(rec_out, "basis")
    base = num(basis.get("baseCents")) if isinstance(basis, dict) else None
    if base is not None:
        return f"{round(base)}c"
    text = (output_field(rec_out, "recovery") or {}).get("value")
    return f"{text}c" if isinstance(text, str) and text else None


def tile(price_out: Row | None, rec_out: Row | None) -> Doc | None:
    """The Recovery tile: the recovery the price used, else the recovery model's number."""
    body = _priced(price_out)
    if body is not None and price_out is not None:
        text = price_recovery_text(body)
        if body.get("recoveryBasis") in ("site_waterfall", ISSUER):
            return {"value": text, **_rec_meta(rec_out, (body.get("recoveryInputs") or {}).get("source"))}
        return {"value": text, **_price_meta(price_out)}
    number = _rec_number(rec_out)
    method = output_field(rec_out, "method")
    if number is not None:
        return {"value": f"{number} ({ISSUER_MODEL if method == ISSUER else SITE_MODEL})", **_rec_meta(rec_out)}
    basis = output_field(rec_out, "basis")
    assumed = num(basis.get("assumedCents")) if isinstance(basis, dict) else None
    if assumed is not None:  # issuer-level debt of an issuer without a balance sheet on file
        return {"value": f"{round(assumed)}c assumed ({NO_NOTE_MODEL})", **_rec_meta(rec_out)}
    return None


def _pts(x: float) -> str:
    return f"{abs(x):.2f}" if abs(x) < 1 else f"{abs(x):.1f}"


def sensitivity_text(body: dict[str, Any]) -> str | None:
    """'±10c recovery moves the price by ±0.13 points', or why recovery does not move it."""
    s = body.get("recoverySensitivity") or {}
    basis = body.get("basis") or {}
    if basis.get("mode") == "tier curve":
        return "Recovery does not enter this price: it is read off the tier curve."
    hold, full = num(s.get("holdingSpreadPer10cPts")), num(s.get("per10cPts"))
    if basis.get("mode") == "issuer bond":
        if hold is None:
            return None
        return (f"±10c recovery moves the price by ±{_pts(hold)} points at today's default odds; the issuer's "
                f"curve is solved from its own bond prices at the same recovery, so those prices set this one.")
    if full is None:
        return None
    if basis.get("defaultOddsToMaturity") is None:
        return ("Recovery does not move this price: no tenant or obligor default odds are priced for it (its "
                "credit risk sits in the spread fitted at issue).")
    sign = "±" if full >= 0 else "∓"
    text = f"±10c recovery moves the price by {sign}{_pts(full)} points"
    if hold is not None and abs(hold - full) >= 0.1:
        text += (f" (at today's spread alone it would be {_pts(hold)}: the model re-fits its spread to the issue "
                 "price with the same recovery)")
    return text + "."


def _line(label: str, value: str | None, meta: Doc) -> Doc | None:
    return None if value is None else {"label": label, "value": {"value": value, **meta}}


def _mm(usd: float) -> str:
    return f"${usd / 1e6:,.1f}mm"


def _path_text(path: list[dict[str, Any]]) -> str | None:
    pts = [(float(p["years"]), float(p["cents"])) for p in path if num(p.get("years")) is not None]
    if len(pts) < 2:
        return None
    ys, cs = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
    marks = [0.0, *[float(y) for y in range(1, int(ys[-1]) + 1)]][:6]
    parts = [f"{round(float(np.interp(y, ys, cs)))}c " + ("today" if y == 0 else f"in {y:.0f} year" + ("s" * (y > 1)))
             for y in marks]
    return ", ".join(parts)


def _lien(conn: Conn, instrument_id: str, lien: str | None, meta: Doc) -> Doc | None:
    if lien is None:
        return None
    row = known_facts(conn, "instrument", instrument_id, ["seniority"]).get("seniority")
    if row is not None and isinstance(row["value"], str) and row["value"].strip():
        got = fact_sourced(row, f"{lien} (filed: {row['value'].strip()[:80]})")
        if got is not None:
            return {"label": "Lien", "value": got}
    return _line("Lien", lien, meta)


def _rank_lien(rank: Any) -> str | None:
    return {"unsecured": "unsecured", "parent": "secured at the parent", "junior": "junior / subordinated"}.get(
        str(rank)) if rank else None


def _site_lines(conn: Conn, instrument_id: str, ri: dict[str, Any], meta: Doc) -> list[Doc | None]:
    inp = ri.get("inputs") or {}
    value, ahead, pari = num(inp.get("valueB")), num(inp.get("seniorClaimsB")), num(inp.get("pariClaimsB"))
    span = ri.get("rangeCents")
    lo_hi = f"{span[0]}{DASH}{span[1]}c" if isinstance(span, list) and len(span) == 2 and span[0] != span[1] else (
        f"{span[0]}c" if isinstance(span, list) and span else None)
    lines: list[Doc | None] = []
    if ri.get("method") == "powered_shell":
        q = inp.get("compQuartilesUsdPerMw")
        per = num(q[1]) if isinstance(q, list) and len(q) == 3 else num(inp.get("compMedianUsdPerMw"))
        gross, share, comps = num(inp.get("grossMw")), num(inp.get("completion")), inp.get("comps")
        works = inp.get("worksUsd")
        works_b = num(works[1]) / 1e9 if isinstance(works, list) and len(works) == 3 else value  # type: ignore[operator]
        energized, cut = num(inp.get("energizedShare")) or 0.0, num(inp.get("unfinishedHaircut"))
        unspent = num(inp.get("unspentProceedsUsd"))
        how = None
        if works_b is not None and per is not None and gross is not None and share is not None:
            part = (f"{share:.0%} finished ({energized:.0%} energized; the unfinished part less a {cut:.0%} haircut)"
                    if cut and share - energized > 1e-4 else f"{share:.0%} finished")
            how = (f"{money_b(works_b)}: {gross:,.0f} gross MW {TIMES} {_mm(per)} per MW"
                   + (f" (median of {comps} comparable sites)" if comps else "") + f" {TIMES} {part}")
        est = [e for e in inp.get("completionEstimators") or [] if isinstance(e, dict) and num(e.get("share")) is not None]
        share_text = None
        if share is not None and inp.get("completionEstimated") and est:
            share_text = f"{share:.0%}, estimated (no source states it): " + "; ".join(
                f"{e.get('label')} {float(e['share']):.0%} ({e.get('detail')})" for e in est if e.get("detail"))
        elif share is not None:
            share_text = f"{share:.0%} ({'stated' if inp.get('completionBasis') == 'pct_complete' else 'delivered MW'})"
        lines += [_line("Share finished", share_text, meta),
                  _line("Building value", how or money_b(works_b), meta),
                  _line("Unspent proceeds", None if not unspent else
                        f"{money_b(unspent / 1e9)} still in the project accounts (funded notes)", meta),
                  _line("Not counted", "; ".join(str(x) for x in inp.get("notInV0") or []) or None, meta),
                  _line("Debt ahead", money_b(ahead), meta),
                  _line("Claims at its rank", money_b(pari), meta),
                  _lien(conn, instrument_id, inp.get("lien"), meta),
                  _line("Range", f"{lo_hi} on the 25th{DASH}75th percentile comps" if lo_hi else None, meta)]
    else:
        units, per_unit, cut = num(inp.get("units")), num(inp.get("valuePerUnitUsd")), num(inp.get("haircut"))
        chip = f" {inp['valuedModel']}" if inp.get("valuedModel") else ""
        how = None
        if value is not None and units is not None and per_unit is not None:
            how = (f"{money_b(value)}: {units:,.0f}{chip} GPUs {TIMES} ${per_unit:,.0f}"
                   + (f" less a {cut:.0%} resale haircut" if cut is not None else ""))
        lines += [_line("GPU collateral value", how or money_b(value), meta),
                  _line("Debt ahead", "$0 (no claims known ahead of the first lien)" if not ahead else money_b(ahead),
                        meta),
                  _line("Pool balance at its rank", money_b(num(inp.get("poolBalanceB"))), meta),
                  _lien(conn, instrument_id, inp.get("lien"), meta),
                  _line("Over the loan's life", _path_text(ri.get("pathCents") or []), meta),
                  _line("Range", f"{lo_hi} if the customer walks between today and a year out" if lo_hi else None,
                        meta)]
    default = ri.get("defaultCents")
    lines.append(_line("Replaces", f"the {default}c default that price v0.1 assumed for every facility"
                       if default is not None else None, meta))
    return lines


def _date(iso_day: Any) -> str | None:
    from datetime import date

    try:
        d = date.fromisoformat(str(iso_day)[:10])
    except ValueError:
        return None
    return f"{d:%b} {d.day}, {d.year}"


def _cents(x: Any) -> str | None:
    n = num(x)
    return None if n is None else f"{round(n)}c"


def _waterfall_steps(lines: list[Any], meta: Doc) -> list[Doc]:
    """The issuer waterfall's four steps as the hover shows them (each M, its haircuts or claims in the note)."""
    out: list[Doc] = []
    for x in lines:
        if not isinstance(x, dict) or not x.get("label"):
            continue
        usd = num(x.get("usd"))
        value = money(usd) if usd is not None else _cents(x.get("cents"))
        if value is None:
            continue
        out.append({"label": str(x["label"]), "value": {"value": value, **meta}, "op": x.get("op"),
                    "note": x.get("note")})
    return out


def _filing_line(filing: Any, name: str | None) -> Doc | None:
    if not isinstance(filing, dict) or not filing.get("periodEnd"):
        return None
    period, filed = _date(filing.get("periodEnd")), _date(filing.get("filed"))
    text = f"{filing.get('form') or 'Filing'} for {period}" + (f", filed {filed}" if filed else "")
    src = {"tag": "F", "asOf": str(filing["periodEnd"]), "sourceName": filing.get("sourceName") or "SEC EDGAR",
           **({"sourceUrl": filing["sourceUrl"]} if filing.get("sourceUrl") else {})}
    return {"label": "Balance sheet" + (f" ({name})" if name else ""), "value": {"value": text, **src}}


def _issuer_detail(conn: Conn, instrument_id: str, inputs: dict[str, Any], meta: Doc, cents: int,
                   span: Any, sensitivity: str | None, priced: bool, calibration: str | None,
                   price_meta: Doc | None = None) -> Doc:
    name = inputs.get("issuerName") or inputs.get("name")
    filing = inputs.get("filing") or {}
    period = _date(filing.get("periodEnd")) if isinstance(filing, dict) else None
    lo_hi = (f"{span[0]}{DASH}{span[1]}c" if isinstance(span, list) and len(span) == 2 and None not in span
             and span[0] != span[1] else None)
    lines = [_filing_line(filing, None),
             _lien(conn, instrument_id, inputs.get("lien"), meta),
             _line("Range", f"{lo_hi} with every non-cash asset 20% lower or higher" if lo_hi else None, meta),
             _line("Issuer curve", calibration, price_meta or meta) if calibration else None,
             _line("Replaces", "the flat 40c corporate notes were priced at", meta)]
    where = f"{name}'s balance sheet" + (f" at {period}" if period else "") if name else "the issuer's balance sheet"
    return {"basis": ISSUER,
            "headline": f"{cents}c from {where}" + (", used in the Marden price" if priced else " (not priced)"),
            "method": {"value": METHOD_NAME[ISSUER], **meta, "sourceUrl": RECOVERY_DOC},
            "lines": [x for x in lines if x is not None],
            "waterfall": _waterfall_steps(inputs.get("lines") or [], meta), "sensitivity": sensitivity}


def detail(conn: Conn, instrument_id: str, price_out: Row | None, rec_out: Row | None) -> Doc | None:
    """The tile's expand: which recovery the price used, from what, its sources and how much it matters."""
    body = _priced(price_out)
    if body is None or price_out is None:
        number = _rec_number(rec_out)
        method = output_field(rec_out, "method")
        basis = output_field(rec_out, "basis") or {}
        if number is None:
            assumed = num(basis.get("assumedCents")) if isinstance(basis, dict) else None
            if assumed is None or rec_out is None:
                return None
            meta = _rec_meta(rec_out)
            return {"basis": "default_assumption", "headline": f"{round(assumed)}c assumed ({NO_NOTE_MODEL})",
                    "method": {"value": "Stated assumption", **meta, "sourceUrl": RECOVERY_DOC},
                    "lines": [x for x in [_line("Assumed", f"{round(assumed)}% of par, the senior unsecured "
                                                           "convention", meta),
                                          _line("No waterfall because", str(basis.get("why") or "").rstrip(".")
                                                or None, meta)] if x is not None],
                    "sensitivity": None}
        meta = _rec_meta(rec_out)
        if method == ISSUER and isinstance(basis, dict):
            return _issuer_detail(conn, instrument_id, {**basis, "lien": _rank_lien(basis.get("rank"))}, meta,
                                  round(num(basis.get("baseCents")) or 0), basis.get("rangeCents"), None, False, None)
        return {"basis": "site_waterfall", "headline": f"{number} from the site collateral model (not priced yet)",
                "method": {"value": f"{METHOD_NAME.get(str(method), 'Recovery model')} ({meta.get('methodVersion')})",
                           **meta, "sourceUrl": RECOVERY_DOC},
                "lines": [], "sensitivity": None}
    ri = body.get("recoveryInputs") or {}
    cents = round(float(body["recovery"]) * 100)
    sensitivity = sensitivity_text(body)
    if body.get("recoveryBasis") == ISSUER:
        meta = _rec_meta(rec_out, ri.get("source"))
        inputs = {**(ri.get("inputs") or {})}
        inputs.setdefault("lien", _rank_lien(inputs.get("rank")))
        return _issuer_detail(conn, instrument_id, inputs, meta, cents, ri.get("rangeCents"), sensitivity, True,
                              ri.get("calibration"), _price_meta(price_out))
    if body.get("recoveryBasis") == "site_waterfall":
        meta = _rec_meta(rec_out, ri.get("source"))
        name = METHOD_NAME.get(str(ri.get("method")), "Recovery model")
        lines = _site_lines(conn, instrument_id, ri, meta)
        return {"basis": "site_waterfall",
                "headline": f"{cents}c from the site collateral model, used in the Marden price",
                "method": {"value": f"{name} ({meta.get('methodVersion') or 'recovery v0.1'})", **meta,
                           "sourceUrl": RECOVERY_DOC},
                "lines": [x for x in lines if x is not None], "sensitivity": sensitivity}
    meta = _price_meta(price_out)
    text = price_recovery_text(body) or f"{cents}c assumed"
    why = reason_nd(rec_out).removeprefix("Not derived: ") if rec_out is not None or ri.get("why") is None else None
    if ri.get("why") and ("price chain" in str(ri["why"]) or "days old" in str(ri["why"])):
        why = str(ri["why"])
    assumed = ri.get("label") or f"{cents}% of par recovered at default"
    because = "No issuer waterfall because" if ri.get("method") == "bond_convention" else "No site model because"
    if ri.get("method") == "bond_convention":
        why = str(ri.get("why") or "") or why
    lines = [_line("Assumed", str(assumed), meta),
             _line(because, (why or str(ri.get("why") or "")).rstrip(".") or None, meta)]
    return {"basis": "default_assumption", "headline": text,
            "method": {"value": f"Default recovery assumption ({method_version(price_out)})", **meta,
                       "sourceUrl": PRICING_DOC},
            "lines": [x for x in lines if x is not None], "sensitivity": sensitivity}
