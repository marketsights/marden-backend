"""The archive's legacy Monte Carlo credit model per case (field table: marden/seed/cases.py).

``aiwc/out/<CASE>.json`` (outputs and sensitivities), ``aiwc/params/<CASE>.json`` (inputs) and the
case's ``buildup`` in ``aiwcredit.json`` become one ``model_output`` row of model
``legacy_credit_sim``, version ``legacy@2026-10-20``, on the site and on each modeled instrument
(memo ``instruments[].modeled`` with a ``debt_row``). They are legacy model outputs, not Marden
prices: nothing goes to ``price_obs``.

Removed before writing (``quarantined`` in the output): the input price and every value computed
at it (yield, spread, spread after expected loss), because 16 of the 28 input prices are
finra.org TRACE last sales and the rest are unverified legacy marks; the tenant / sponsor hazard
rates and party spreads read off legacy bond spreads (TRACE-derived G-spreads); and buildup inputs
whose basis is 'market' (the same bond spreads). Outputs that do not depend on the market price
are kept: p_loss, recovery and its quantiles, expected loss, causes, break-even and +200 bp prices.
"""

from __future__ import annotations

from typing import Any

from marden.seed.cases_archive import AIWCREDIT, MEMO, MODEL_RUN_AS_OF, OUT, PARAMS, Archive
from marden.seed.cases_quarantine import PARAM_KEYS, SIM_KEYS, scrub_all, scrub_text, strip
from marden.seed.cases_sink import FactSink, ModelSink, usd_m

SIM_MODEL = "legacy_credit_sim"


def modeled_instruments(arc: Archive, case: str) -> list[str]:
    rows = arc.get(MEMO, case).get("instruments") or []
    return arc.known([r.get("debt_row") for r in rows if r.get("modeled")])


def _present(obj: Any, keys: frozenset[str], prefix: str) -> list[str]:
    """Dotted paths of the quarantined keys present in `obj` (to report what was removed)."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and v is not None:
                out.append(f"{prefix}{k}")
            elif isinstance(v, dict):
                out += _present(v, keys, f"{prefix}{k}.")
    return out


def _inputs(params: dict[str, Any]) -> dict[str, Any]:
    clean = strip(params, PARAM_KEYS)
    for key in ("notes", "kv", "support_text"):
        if key in clean:
            clean[key] = scrub_all(clean[key])
    return clean


def _buildup(buildup: dict[str, Any] | None) -> tuple[dict[str, Any] | None, int]:
    if not buildup:
        return None, 0
    inputs = buildup.get("inputs") or []
    kept = [{"input": i[0], "value": scrub_text(i[1]), "basis": i[2]} for i in inputs
            if isinstance(i, list) and len(i) >= 3 and i[2] != "market"]
    out = {k: v for k, v in buildup.items() if k != "inputs"}
    out["paths"] = [dict(p, how=scrub_text(p.get("how"))) for p in out.get("paths") or []]
    out["inputs"] = kept
    return out, len(inputs) - len(kept)


def write_model(models: ModelSink, arc: Archive, case: str, slug: str, stats: dict[str, Any]) -> None:
    out, params = arc.get(OUT, case), arc.get(PARAMS, case)
    if not out:
        stats["skipped"].setdefault("no legacy model output (aiwc/out)", []).append(case)
        return
    credit = arc.credit_cases.get(case) or {}
    buildup, market_inputs = _buildup(credit.get("buildup"))
    quarantined = _present(out, SIM_KEYS, "out.") + _present(params, PARAM_KEYS, "params.")
    if market_inputs:
        quarantined.append(f"buildup.inputs: {market_inputs} with basis 'market' (legacy bond spreads)")
    ids = modeled_instruments(arc, case)
    output = {
        "case": case, "site_slug": slug, "instrument_ids": ids, "instrument": (params.get("debt") or {}).get("inst"),
        "paths": arc.data[AIWCREDIT].get("paths"),
        "outputs": strip({k: v for k, v in out.items() if k != "sens"}, SIM_KEYS),
        "sensitivities": out.get("sens"), "buildup": buildup, "inputs": _inputs(params) if params else None,
        "quarantined": quarantined, "not_a_price": True,
        "source_files": [OUT.format(case), PARAMS.format(case), AIWCREDIT],
    }
    models.add(SIM_MODEL, "site", slug, MODEL_RUN_AS_OF, output)
    for iid in ids:
        models.add(SIM_MODEL, "instrument", iid, MODEL_RUN_AS_OF, output)
    stats["quarantined_model_fields"] = stats.get("quarantined_model_fields", 0) + len(quarantined)


def write_case_notes(sink: FactSink, arc: Archive, case: str, slug: str) -> None:
    """The case record's model summary, verdict, notes and holder list (aiwcredit.json cases[])."""
    credit = arc.credit_cases.get(case)
    if not credit:
        return
    src = arc.src(AIWCREDIT)
    sink.add("site", slug, "model_summary", scrub_all(credit.get("kv") or {}), src=src, judged=True)
    sink.add("site", slug, "model_verdict", scrub_text(credit.get("verdict")), src=src, judged=True)
    sink.add("site", slug, "research_notes", scrub_all(credit.get("notes") or []), src=src, judged=True)
    holders = credit.get("holders") or {}
    value = {"funds": [{"fund": f[0], "par_usd": usd_m(f[1]) if len(f) > 1 else None}
                       for f in holders.get("funds") or [] if isinstance(f, list) and f],
             "bdcs": holders.get("bdc"), "banks": holders.get("banks")}
    sink.add("site", slug, "holders_text", value, src=src)
