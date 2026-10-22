"""The Debt tab's "Recovery if <tenant> walks" card (spec 04 "Recovery inputs", spec 22 "Recovery"): the
`recovery` model's own number for the site's main instrument and the arithmetic that produces it, one
plain line each, read from the model's basis (marden.models.recovery and recovery_shell).

- range: the model's cents per dollar ('100c', a '62-74c' range with an en dash). When the model lacks an
  input the range is n/d, `reason` names the input in plain words, and `assumed` shows the price model's
  recovery for what it is, a default ('assumed 55c (price model v0.1 default)'), never as the range.
- inputs, powered shell: comparable value per MW x gross MW x share finished = shell value; less the
  claims ranking ahead; over the claims at the loan's rank = cents, capped at 100c. GPU pool: units x
  value per unit x (1 - haircut) = collateral value; over the pool balance = cents. What model v0 does
  not count (equipment on site, re-letting time) is n/d with its reason; so is any line the model could
  not fill (one plain line, never bracketed text). Every line carries the recovery output's source.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.views.instrument_inputs import latest_output as instrument_output
from marden.readmodels.views.instrument_inputs import method_version, model_sourced, num
from marden.readmodels.views.instrument_text import DASH
from marden.readmodels.views.recovery_text import cents_text
from marden.readmodels.views.sites_common import Doc, is_sourced, latest_output, msourced, nd, output_field, party_label

TIMES, MINUS, OVER = "\u00d7", "\u2212", "\u00f7"  # the arithmetic signs
EQUIPMENT = "not counted in model v0; no equipment invoices or liens on file"
RELETTING = "not counted in model v0; no re-letting times by market on file"
COMPLETION = {"nothing delivered and no stated completion": "nothing delivered yet and no completion share stated",
              "no capacity": "no capacity on file to measure delivered MW against",
              "no site facts": "no completion or delivery facts on file for the site"}


def money_b(value: Any) -> str | None:
    """$B as text: '$10.9B', '$750mm', '$0'."""
    if not isinstance(value, int | float):
        return None
    if value == 0:
        return "$0"
    return f"${value:,.1f}B" if abs(value) >= 1 else f"${value * 1000:,.0f}mm"


def _mm(usd: float) -> str:
    return f"${usd / 1e6:,.1f}mm"


def _value(out: Row | None, text: str | None) -> Doc:
    """A line of the card as Sourced<string>, carrying the recovery output's own source and method."""
    meta = output_field(out, "recovery")
    return {**meta, "value": text} if is_sourced(meta) else msourced(out, text)


def _line(out: Row | None, label: str, text: str | None, reason: str) -> Doc:
    item: Doc = {"label": label, "value": _value(out, text)}
    if text is None:
        item["reason"] = reason
    return item


def _triple(v: Any) -> list[float] | None:
    return [float(x) for x in v] if isinstance(v, list) and len(v) == 3 and all(num(x) is not None for x in v) else None


def _claims_reason(missing: list[str]) -> str:
    for m in missing:
        if m.startswith("the instrument's rank"):
            return "the loan's rank in the site's debt (seniority) is not on file"
        if m.startswith(("seniority of other claims", "amounts of claims")):
            return _plain(m, {}) + " is not on file"
    return "no claims on the site on file"


def _plain(m: str, basis: dict[str, Any]) -> str:
    """One `missing` entry of the recovery model in plain words."""
    if m.startswith("at least"):
        return f"enough comparable sites with a filed cost and gross MW ({len(basis.get('comps') or [])} on file)"
    if m.startswith("site gross MW"):
        return "the site's gross or critical IT MW"
    if m.startswith("completion of the works"):
        why = str(basis.get("completionBasis") or "")
        return f"the share of the works finished ({COMPLETION.get(why, why)})" if why else "the share finished"
    if m.startswith("the instrument's rank"):
        return "the loan's rank in the site's debt (seniority)"
    n = len(m.split(": ", 1)[-1].split(", "))
    debts = f"{n} debt{'s' if n > 1 else ''}"
    if m.startswith("seniority of other claims"):
        return f"the rank of {n} other {debts.split(' ', 1)[1]} on the site"
    if m.startswith("amounts of claims"):
        return f"the size of {debts} at or ahead of the loan"
    return m


def _recovery_line(value: float | None, ahead: float | None, pari: float | None,
                   cover: list[float] | None) -> str | None:
    if value is None or ahead is None or not pari:
        return None
    base = round(cover[1]) if cover else round((value - ahead) / pari * 100)
    text = f"({money_b(value)} {MINUS} {money_b(ahead)}) {OVER} {money_b(pari)} = {base}c"
    text += ", capped at 100c" if base > 100 else ", floored at 0c" if base < 0 else ""
    if cover:
        lo, hi = (max(0, min(100, round(c))) for c in (cover[0], cover[2]))
        text += f" ({lo}{DASH}{hi}c on the 25th{DASH}75th comps)" if lo != hi else ""
    return text


def shell_lines(out: Row, instrument_id: str) -> list[Doc]:
    b = output_field(out, "basis") or {}
    missing = [str(m) for m in output_field(out, "missing") or []]
    comps = [c for c in b.get("comps") or [] if isinstance(c, dict)]
    q, vals, cover = _triple(b.get("compQuartilesUsdPerMw")), _triple(b.get("valueUsd")), _triple(b.get("coverCents"))
    gross, share = num(b.get("grossMw")), num(b.get("completion"))
    value = num(output_field(out, "valueB", "value"))
    ahead, pari = num(b.get("seniorClaimsB")), num(b.get("pariClaimsB"))
    per_mw = (f"{_mm(q[1])} per gross MW, median of {len(comps)} sites (25th{DASH}75th: "
              f"{_mm(q[0])}{DASH}{_mm(q[2])})") if q else None
    gross_text = None
    if gross is not None:
        how = str(b.get("grossMwBasis") or "")
        gross_text = f"{gross:,.0f} gross MW" + (f" (critical IT MW {TIMES} PUE {how.rsplit(' ', 1)[-1]})"
                                                 if how.startswith("critical_it_mw") else "")
    why = str(b.get("completionBasis") or "")
    how_share = " (stated completion)" if why == "pct_complete" else (
        " (delivered MW over capacity)" if "delivered" in why else "")
    share_text = None if share is None else f"{share:.0%}{how_share}"
    shell = None
    if value is not None and q and gross is not None and share is not None:
        shell = f"{_mm(q[1])} {TIMES} {gross:,.0f} MW {TIMES} {share:.0%} = {money_b(value)}"
        if vals and round(vals[0] / 1e8) != round(vals[2] / 1e8):
            shell += f" (25th{DASH}75th comps: {money_b(vals[0] / 1e9)}{DASH}{money_b(vals[2] / 1e9)})"
    lacking = [n for n, ok in (("comparable value per MW", q is not None), ("site capacity", gross is not None),
                               ("share finished", share is not None)) if not ok]
    claims = [c for c in b.get("claims") or [] if isinstance(c, dict)]
    own = next((c.get("rank") for c in claims if c.get("instrumentId") == instrument_id), None)
    peers = sum(1 for c in claims if own is not None and c.get("rank") == own)
    who = "this loan only" if peers <= 1 else f"{peers} loans, pari passu"
    pari_text = None if pari is None else f"{money_b(pari)} ({who})"
    claims_why = _claims_reason(missing)
    return [
        _line(out, "Comparable value per MW", per_mw,
              f"too few comparable sites with a filed cost and gross MW ({len(comps)} on file)"),
        _line(out, "Site capacity", gross_text, "no gross or critical IT MW on file"),
        _line(out, "Share finished", share_text, COMPLETION.get(why, why or "no completion on file")),
        _line(out, "Powered shell value", shell,
              "needs the " + " and ".join(lacking) if lacking else "not in the model output"),
        _line(out, "Claims ahead of the loan", money_b(ahead), claims_why),
        _line(out, "Claims at the loan's rank", pari_text, claims_why),
        _line(out, "Recovery", _recovery_line(value, ahead, pari, cover),
              "needs the powered shell value" if value is None else "needs the claims at and ahead of the loan"),
        _line(out, "Equipment on site", None, EQUIPMENT),
        _line(out, "Re-letting time", None, RELETTING),
    ]


def gpu_lines(out: Row) -> list[Doc]:
    b = output_field(out, "basis") or {}
    missing = "; ".join(str(m) for m in output_field(out, "missing") or []) or "not in the model output"
    units, per_unit, pool = num(b.get("units")), num(b.get("valuePerUnitUsd")), num(b.get("poolBalanceB"))
    value, haircut = num(output_field(out, "valueB", "value")), num(b.get("haircut"))
    coll = None
    if value is not None and units is not None and per_unit is not None:
        cut = f" {TIMES} (1 {MINUS} {haircut:.0%} haircut)" if haircut is not None else ", less the resale haircut"
        coll = f"{units:,.0f} {TIMES} ${per_unit:,.0f}{cut} = {money_b(value)}"
    rec = None
    if value is not None and pool:
        today = round(value / pool * 100)
        rec = f"{money_b(value)} {OVER} {money_b(pool)} = {today}c today" + (", capped at 100c" if today > 100 else "")
        if span := cents_text(output_field(out, "recovery", "value")):
            rec += f"; {span} if the customer walks between today and a year out"
    pool_name = f" ({b['pool']})" if isinstance(b.get("pool"), str) and b["pool"] else ""
    return [
        _line(out, "GPUs financed", None if units is None else f"{units:,.0f}", missing),
        _line(out, "Value per GPU",
              None if per_unit is None else f"${per_unit:,.0f} a unit (rental index, depreciation)", missing),
        _line(out, "Collateral value", coll, missing),
        _line(out, "Claims ahead of the loan", money_b(b.get("seniorClaimsB")), "facility terms not on file"),
        _line(out, "Pool balance at the loan's rank", None if pool is None else f"{money_b(pool)}{pool_name}", missing),
        _line(out, "Recovery", rec, "needs the collateral value and the pool balance"),
    ]


def reason_nd(out: Row | None) -> str:
    """Why the recovery model gives no number, in plain words."""
    if out is None:
        return "Not derived: the recovery model has not run for this loan yet."
    if output_field(out, "method") is None:
        return "Not derived: unsecured or unsited debt needs the issuer's balance-sheet waterfall, not in model v0."
    b = output_field(out, "basis") or {}
    needs = [_plain(str(m), b) for m in output_field(out, "missing") or []]
    return "Not derived: the recovery model needs " + "; ".join(needs) + "." if needs else "Not derived by the model."


def price_assumption(conn: Conn, instrument_id: str) -> Doc | None:
    """The price model's recovery, a default it assumes: 'assumed 55c (price model v0.1 default)'."""
    out = instrument_output(conn, "price", instrument_id, "instrument")
    n = num(output_field(out, "recovery"))
    if out is None or n is None:
        return None
    label = method_version(out).replace(f"{out['model']} ", f"{out['model']} model ", 1)
    return model_sourced(out, f"assumed {round(n * 100 if n <= 1 else n)}c ({label} default)")


def recovery_card(conn: Conn, parties: dict[str, Row], main: Doc | None) -> Doc:
    out = latest_output(conn, "recovery", "instrument", main["id"]) if main else None
    method = output_field(out, "method")
    inputs = shell_lines(out, main["id"]) if out and main and method == "powered_shell" else (
        gpu_lines(out) if out and method == "gpu_collateral" else [])
    card: Doc = {"ifWalks": party_label(parties, "tenant") or "n/d",
                 "range": _value(out, cents_text(output_field(out, "recovery", "value"))) if out else nd("M"),
                 "inputs": inputs}
    if card["range"]["value"] is None:
        card["reason"] = reason_nd(out) if main else "No loan on file needs this site finished."
        if main and (assumed := price_assumption(conn, main["id"])) is not None:
            card["assumed"] = assumed
    return card
