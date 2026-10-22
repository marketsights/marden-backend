"""Model `stress` (spec 09, 18, 22 "Stress"): what a shock does across every chain, priced by the price engine.

    python -m marden model stress [--as-of YYYY-MM-DD] [--scenario NAME --severity base|severe --param k=v ...]

This module computes; marden.models.stress_run stores runs, schedules them and registers the CLI.
A run builds the price engine's book for the day once (stress_price.Book), turns each scenario into shocked
inputs (stress_scenarios; contagion: marden.models.contagion) and re-prices the debt they reach. Outputs
(one model_run, model 'stress', version 'stress v0.2'):

- subject ('scenario', '<scenario>:<severity>') for the saved V1 scenarios (end_user_funding_miss,
  tenant_downgrade, power_delay x base/severe, default parameters) = StressResult (types.ts) + `request`
  (the full parameters), `missing`, `basis`; subject ('scenario', 'contagion:<company>:<severity>') =
  ContagionResult for the V1 obligation-map companies at payPct 50;
- subject ('instrument', id) = {rows: [{scenario, priceAfter, change, holderNavHitMm}]}: every saved base
  scenario that moves the instrument (InstrumentDetail.stress);
- subject ('entity', id) = {stressSummary: {scenario, siteDebtLossB, notesLossB}} for the scenario's tenant
  or end user (Payer.stressSummary);
- a request with `params.instruments` (a list of instrument ids; V1 scenarios) applies the scenario's shock to
  those instruments only (`selected_result`): one row per chosen instrument, kpis over them;
- custom requests (POST /api/stress/run with other parameters): the SQL function `stress_run(jsonb)`
  (db/migrations/20261021210000_stress_run.sql) queues `models.stress_custom`, which writes subject
  ('scenario', 'custom:<md5 of the request>') for the day; the API polls the same function for it.
- saved stress runs (`stress_saved`, org-owned): the nightly run fills `result` and `ran_at`.

StressResult: kpis.exposedB = the site debt on the scenario's sites ($B face, sized instruments);
valueLostB = sum of size x (price now - after) / 100 over them, valueLostPct of exposedB; tenantNotesLossB
the same over the tenant's own notes (one shaded last row); loansBelow95 = names that fall from 95 or above
to below 95; fundsHit = funds (mark-gap holder lines) holding a row that loses value. flow = one entry per
site (its debt, size-weighted price change, severity of the rule it trips). rows worst first. priceNow is
the price engine's price on the day (equal to the stored price output), priceAfter the shocked re-price.
Rent coverage is omitted: no filing discloses rent. Ranges are uncalibrated ('model v0').

Runs: nightly 06:17 UTC (02:17 ET; spec 22 "Saved scenarios nightly"), and within a minute of a new price
or cushion output (marden.models.triggers). A past --as-of is a backtest: the book and every input are as
public on that day.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.models import contagion as contagion_model
from marden.models import stress_inputs
from marden.models.sourced import Batch
from marden.models.stress_price import Book, remember
from marden.models.stress_scenarios import DEFAULTS, Scenario, build, display_name
from marden.models.stress_walk import walk_value
from marden.readmodels.context import building, business_today

log = logging.getLogger(__name__)

MODEL, VERSION = "stress", "0.2"  # v0.2 (2026-10-22): a walked contract's rent stops (stress_walk)
METHOD = "stress v0.2"
CRON_UTC = "17 6 * * *"
SEVERITIES = ("base", "severe")
CONTAGION_COMPANIES = ("openai", "anthropic", "oracle", "microsoft", "coreweave", "nvidia")
CONTAGION_PAY_PCT = 50
SCENARIO_LABEL = {
    "end_user_funding_miss": "End user misses its funding plan",
    "tenant_downgrade": "Tenant downgrade",
    "power_delay": "Power delay",
}


def _num(x: Any) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def _weighted(rows: list[tuple[float, float]]) -> float | None:
    """Size-weighted mean; equal weights when no size is known; None (n/d) for no rows, never 0."""
    if not rows:
        return None
    total = sum(w for w, _ in rows)
    if not total:
        return round(sum(c for _, c in rows) / len(rows), 3)
    return round(sum(w * c for w, c in rows) / total, 3)


def _notes_loss(conn: Conn, party: str, out: dict[str, Any]) -> float | None:
    """The payer's own notes loss: the scenario's figure; 0 only when it has no notes at all; else n/d."""
    if out["kpis"].get("tenantNotesLossB") is not None:
        return out["kpis"]["tenantNotesLossB"]
    return 0.0 if not stress_inputs.issued_by(conn, [party]).get(party) else None


def stress_result(conn: Conn, book: Book, sc: Scenario) -> dict[str, Any]:
    """StressResult for one scenario (plus missing and basis)."""
    day = book.day
    site_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM instrument WHERE site_slug = ANY(%s) ORDER BY id", (sorted(sc.sites),)
        ).fetchall()
    ]
    notes = {
        iid: t
        for t, ids in stress_inputs.issued_by(conn, sc.tenants).items()
        for iid in ids
        if iid in book.insts and iid not in site_ids
    }
    info = stress_inputs.load(conn, day, [*site_ids, *notes])
    missing = list(sc.missing)
    priced: dict[str, dict[str, Any]] = {}
    for iid in [*site_ids, *notes]:
        if iid not in book.insts:
            missing.append(f"instrument {iid}: not priced by Marden's price model yet")
            continue
        rp = book.reprice(iid, sc.shock)
        now, after = _num(rp["now"]), _num(rp["after"])
        if now is None:
            missing.append(f"instrument {iid}: {'; '.join(rp['missing'][:2]) or 'not priced'}")
            continue
        priced[iid] = {"now": now, "after": None if after is None else round(after, 3)}
        _apply_walk(conn, book, sc, iid, info.get(iid), priced[iid])
    rows, flow_parts, funds = [], defaultdict(list), set()
    flow_debt: dict[str, float] = defaultdict(float)
    exposed = lost = 0.0
    below = []
    for iid in site_ids:
        if iid not in priced:
            continue
        inst, p = info[iid], priced[iid]
        hit = sc.sites[inst.site]  # type: ignore[index]
        rows.append(
            {
                "instrumentId": iid,
                "name": inst.name,
                "sizeB": inst.size_b(day),
                "why": hit.why,
                **({"ruleCode": hit.rule} if hit.rule else {}),
                "priceNow": p["now"],
                "priceAfter": p["after"],
                "holders": inst.holders_text(),
                **({"walk": p["walk"]} if "walk" in p else {}),
            }
        )
        size = inst.size_usd
        if size is None:  # n/d size: counted in the rows, left out of the dollar totals, and listed
            missing.append(f"instrument {iid}: size n/d (left out of exposed, value lost and flow debt)")
            size = 0.0
        exposed += size
        flow_debt[inst.site] += size
        if p["after"] is not None:
            change = p["after"] - p["now"]
            flow_parts[inst.site].append((size, change))
            lost += size * -change / 100.0
            if p["now"] >= 95 > p["after"]:
                below.append(inst.name)
            if change < 0:
                funds.update(h["name"] for h in inst.holders if isinstance(h.get("name"), str))
    notes_loss = None
    for tenant in sc.tenants:
        ids = [i for i, t in notes.items() if t == tenant and i in priced and priced[i]["after"] is not None]
        if not ids:
            continue
        unsized_notes = [i for i in ids if info[i].size_usd is None]
        if unsized_notes:
            missing.append(f"{tenant} notes: {len(unsized_notes)} with size n/d (left out of the notes loss)")
        sized = [(info[i].size_usd or 0.0, priced[i]) for i in ids]
        face = sum(s for s, _ in sized)
        loss = sum(s * (p["now"] - p["after"]) / 100.0 for s, p in sized)
        notes_loss = (notes_loss or 0.0) + loss
        w = [(s, p["now"]) for s, p in sized]
        wa = [(s, p["after"]) for s, p in sized]
        tenant_name = display_name(conn, tenant)
        rows.append(
            {
                "instrumentId": f"notes:{tenant}",
                "name": f"{tenant_name} notes ({len(ids)} issues)",
                "sizeB": {
                    "value": round(face / 1e9, 3),
                    "tag": "F",
                    "asOf": day.isoformat(),
                    "sourceName": "Marden instrument facts (sum)",
                },
                "why": f"{tenant_name}'s own notes, priced at {tenant_name}'s weaker credit after the shock",
                "priceNow": _weighted(w),
                "priceAfter": _weighted(wa),
                "holders": f"{sum(info[i].holder_count for i in ids)} fund lines",
            }
        )
        for i in ids:
            if priced[i]["after"] < priced[i]["now"]:
                funds.update(h["name"] for h in info[i].holders if isinstance(h.get("name"), str))
    rows.sort(
        key=lambda r: (
            r["instrumentId"].startswith("notes:"),
            (r["priceAfter"] - r["priceNow"]) if r["priceAfter"] is not None else 0.0,
        )
    )
    names = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM site").fetchall()}
    flow = []
    for slug, hit in sorted(sc.sites.items()):
        if slug not in flow_debt:
            continue
        parts = flow_parts.get(slug)
        flow.append(
            {
                "site": names.get(slug, slug),
                "debtB": round(flow_debt[slug] / 1e9, 3),
                "priceChange": _weighted(parts) if parts else None,
                "severity": hit.severity,
            }
        )
    flow.sort(key=lambda f: (f["priceChange"] is None, f["priceChange"] or 0.0))
    severe_nd = isinstance(sc.shock.spread_bp, float) and math.isnan(sc.shock.spread_bp)
    kpis = {
        "exposedB": round(exposed / 1e9, 3),
        "valueLostB": None if severe_nd else round(lost / 1e9, 3),
        "valueLostPct": None if severe_nd or not exposed else round(100 * lost / exposed, 2),
        "loansBelow95": sorted(below),
        "fundsHit": None if severe_nd else len(funds),  # prices after the shock n/d: who loses is n/d too
    }
    if severe_nd:
        missing.append("severe: prices after the shock n/d, so fundsHit is n/d and loansBelow95 lists none")
    if notes_loss is not None:  # optional in types.ts: present only when the scenario has a tenant with notes
        kpis["tenantNotesLossB"] = round(notes_loss / 1e9, 3)
    return {
        "kpis": kpis,
        "flow": flow,
        "rows": rows,
        "missing": sorted(set(missing)),
        "basis": stress_result_basis(sc, severe_nd),
    }


def _apply_walk(conn: Conn, book: Book, sc: Scenario, iid: str, inst: Any, p: dict[str, Any]) -> None:
    """Debt on a site whose rent the walking end user pays: price after = the walk waterfall (stress_walk)."""
    if sc.walk is None or inst is None or inst.site not in sc.walk.sites:
        return
    p["after"], p["walk"] = walk_value(conn, book, iid, sc.walk, sc.shock, inst.size_usd, p["after"])


def chosen_instruments(raw: Any) -> list[str]:
    """`params.instruments`: the instrument ids a run is limited to (a list, or one comma-separated string), sorted,
    duplicates and blanks dropped; [] for none (the whole scenario)."""
    items = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return sorted({str(i).strip() for i in items if isinstance(i, str | int) and str(i).strip()})


def _change(p: dict[str, Any]) -> float | None:
    return None if p["after"] is None else round(p["after"] - p["now"], 3)


def selected_result(conn: Conn, book: Book, sc: Scenario, chosen: list[str]) -> dict[str, Any]:
    """StressResult for the scenario's shock applied to the chosen instruments only (`params.instruments`).

    Every chosen instrument the price engine prices is re-priced under the same shock, whether or not the
    scenario reaches it, and is one row (tenant notes one row each, not summed): `why` is the rule its site
    trips, the tenant's own notes, a chain party the shock moves, or "not reached" (price after = price now,
    the engine's own re-price). kpis are over those rows only: exposedB their face, valueLostB their loss,
    loansBelow95 and fundsHit as in stress_result; tenantNotesLossB is left out (the notes are rows).
    flow: the chosen instruments' sites that the scenario hits. Chosen ids the engine cannot price are listed
    in `missing`, never shown as unchanged."""
    day = book.day
    tenant_of = {iid: t for t, ids in stress_inputs.issued_by(conn, sc.tenants).items() for iid in ids}
    info = stress_inputs.load(conn, day, chosen)
    missing = list(sc.missing)
    names: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    funds: set[str] = set()
    below: list[str] = []
    flow_parts: dict[str, list[tuple[float, float]]] = defaultdict(list)
    flow_debt: dict[str, float] = defaultdict(float)
    exposed = lost = 0.0
    for iid in chosen:
        inst = info.get(iid)
        if inst is None:
            missing.append(f"instrument {iid}: not in Marden's instrument list")
            continue
        if iid not in book.insts:
            missing.append(f"instrument {iid}: not priced by Marden's price model yet")
            continue
        rp = book.reprice(iid, sc.shock)
        now, after = _num(rp["now"]), _num(rp["after"])
        if now is None:
            missing.append(f"instrument {iid}: {'; '.join(rp['missing'][:2]) or 'not priced'}")
            continue
        p = {"now": now, "after": None if after is None else round(after, 3)}
        _apply_walk(conn, book, sc, iid, inst, p)
        change = _change(p)
        hit = sc.sites.get(inst.site) if inst.site else None
        if hit is not None:
            why = hit.why
        elif iid in tenant_of:
            t = tenant_of[iid]
            names.setdefault(t, display_name(conn, t))
            why = f"{names[t]}'s own notes, priced at {names[t]}'s weaker credit after the shock"
        elif change:
            why = "A party behind it is hit by this shock"
        elif change == 0:
            why = "Not reached by this shock: its price does not move"
        else:
            why = "Not on a site this shock reaches"
        rows.append(
            {
                "instrumentId": iid,
                "name": inst.name,
                "sizeB": inst.size_b(day),
                "why": why,
                **({"ruleCode": hit.rule} if hit is not None and hit.rule else {}),
                "priceNow": p["now"],
                "priceAfter": p["after"],
                "holders": inst.holders_text(),
                **({"walk": p["walk"]} if "walk" in p else {}),
            }
        )
        size = inst.size_usd
        if size is None:
            missing.append(f"instrument {iid}: size n/d (left out of exposed, value lost and flow debt)")
            size = 0.0
        exposed += size
        if hit is not None:
            flow_debt[inst.site] += size  # type: ignore[index]
        if change is not None:
            if hit is not None:
                flow_parts[inst.site].append((size, change))  # type: ignore[index]
            lost += size * -change / 100.0
            if p["now"] >= 95 > p["after"]:  # type: ignore[operator]
                below.append(inst.name)
            if change < 0:
                funds.update(h["name"] for h in inst.holders if isinstance(h.get("name"), str))
    rows.sort(key=lambda r: (r["priceAfter"] - r["priceNow"]) if r["priceAfter"] is not None else 0.0)
    site_names = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM site").fetchall()}
    flow = [
        {
            "site": site_names.get(slug, slug),
            "debtB": round(flow_debt[slug] / 1e9, 3),
            "priceChange": _weighted(flow_parts[slug]) if flow_parts.get(slug) else None,
            "severity": sc.sites[slug].severity,
        }
        for slug in sorted(flow_debt)
    ]
    flow.sort(key=lambda f: (f["priceChange"] is None, f["priceChange"] or 0.0))
    severe_nd = isinstance(sc.shock.spread_bp, float) and math.isnan(sc.shock.spread_bp)
    if severe_nd:
        missing.append("severe: prices after the shock n/d, so fundsHit is n/d and loansBelow95 lists none")
    basis = {**stress_result_basis(sc, severe_nd), "instruments": chosen}
    return {
        "kpis": {
            "exposedB": round(exposed / 1e9, 3),
            "valueLostB": None if severe_nd else round(lost / 1e9, 3),
            "valueLostPct": None if severe_nd or not exposed else round(100 * lost / exposed, 2),
            "loansBelow95": sorted(below),
            "fundsHit": None if severe_nd else len(funds),
        },
        "flow": flow,
        "rows": rows,
        "missing": sorted(set(missing)),
        "basis": basis,
    }


def stress_result_basis(sc: Scenario, severe_nd: bool) -> dict[str, Any]:
    """The `basis` block of a stress result: method, label, the scenario's own basis, the shock, the sites."""
    return {
        "methodVersion": f"{METHOD} (model v0)",
        "label": sc.label,
        **sc.basis,
        "shock": {
            "hazardMultiplier": {k: round(v, 4) for k, v in sc.shock.hazard_mult.items()},
            "completionMonths": sc.shock.completion_months,
            "spreadBp": None if severe_nd else sc.shock.spread_bp,
        },
        "sites": {
            s: {"rule": h.rule, "severity": h.severity, "cushion": h.cushion} for s, h in sorted(sc.sites.items())
        },
    }


def run_request(conn: Conn, book: Book, request: dict[str, Any]) -> dict[str, Any]:
    """One request. `params.instruments` (V1 scenarios) limits the run to those instruments (selected_result)."""
    scenario, severity = request["scenario"], request.get("severity") or "base"
    params = dict(request.get("params") or {})
    with building(book.day):
        if scenario == "contagion":
            company, pay = str(params.get("company") or ""), float(params.get("payPct", CONTAGION_PAY_PCT))
            out = contagion_model.contagion(conn, book.day, company, pay, severity, book=book)
            full = {"company": company, "payPct": pay}
        else:
            chosen = chosen_instruments(params.pop("instruments", None))
            sc = build(conn, book, scenario, params, severity)
            out = selected_result(conn, book, sc, chosen) if chosen else stress_result(conn, book, sc)
            full = {**sc.basis["params"], **({"instruments": chosen} if chosen else {})}
    return {**out, "request": {"scenario": scenario, "params": full, "severity": severity}}


def saved_requests() -> list[dict[str, Any]]:
    reqs = [{"scenario": s, "params": dict(p), "severity": sev} for s, p in DEFAULTS.items() for sev in SEVERITIES]
    reqs += [
        {"scenario": "contagion", "params": {"company": c, "payPct": CONTAGION_PAY_PCT}, "severity": "base"}
        for c in CONTAGION_COMPANIES
    ]
    return reqs


def subject_of(request: dict[str, Any]) -> str:
    if request["scenario"] == "contagion":
        return f"contagion:{request['params']['company']}:{request['severity']}"
    return f"{request['scenario']}:{request['severity']}"


def _label(conn: Conn, request: dict[str, Any], out: dict[str, Any]) -> str:
    if request["scenario"] == "contagion":
        company = display_name(conn, request["params"]["company"])
        return f"{company} pays {request['params']['payPct']:g}%"
    return f"{out['basis'].get('label') or SCENARIO_LABEL[request['scenario']]} ({request['severity']})"


def compute(conn: Conn, day: date) -> Batch:
    book = remember(Book.build(conn, day)) if day == business_today() else Book.build(conn, day)
    batch = Batch()
    by_inst: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for req in saved_requests():
        out = run_request(conn, book, req)
        batch.add("scenario", subject_of(req), out)
        if req["scenario"] == "contagion" or req["severity"] != "base":
            continue
        label = _label(conn, req, out)
        info = stress_inputs.load(conn, day, [r["instrumentId"] for r in out["rows"]])
        for r in out["rows"]:
            inst = info.get(r["instrumentId"])
            if inst is None or r["priceAfter"] is None:
                continue
            change = round(r["priceAfter"] - r["priceNow"], 3)
            by_inst[r["instrumentId"]].append(
                {
                    "scenario": label,
                    "priceAfter": r["priceAfter"],
                    "change": change,
                    "holderNavHitMm": inst.holder_nav_hit_mm(change),
                }
            )
        party = req["params"].get("tenant") or req["params"].get("endUser")
        if party and out["kpis"]["valueLostB"] is not None:
            batch.add(
                "entity",
                party,
                {
                    "stressSummary": {
                        "scenario": label,
                        "siteDebtLossB": out["kpis"]["valueLostB"],
                        "notesLossB": _notes_loss(conn, party, out),
                    },
                    "basis": {"methodVersion": METHOD, "request": out["request"]},
                },
            )
    for iid, rows in sorted(by_inst.items()):
        batch.add("instrument", iid, {"rows": rows, "basis": {"methodVersion": METHOD}})
    batch.views.add(("home", "all"))
    batch.inputs = [subject_of(r) for r in saved_requests()]
    return batch
