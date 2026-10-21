"""GPU collateral (field table: marden/seed/cases.py): the archive's GPU loan-to-value table
(aiwc/q3x/underwater.json), its recovery tables (aiwc/q3x/rec.json) and the GPU market block of
aiwcredit.json (`gpu`: provider list prices, resale, residual bands, chips, useful lives, PUE,
EIA power prices, operator profiles).

Not loaded: `gpu.rent`, the GPU rental index points (every point is a Silicon Data index value,
a commercial product whose redistribution terms are unconfirmed: gpu.lic), and the Silicon Data
resale point; `gpu.pwg` capacity prices (the capacity-auction collectors own them); the legacy
marks (`px`) in every table.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from marden.seed.cases_archive import AIWCREDIT, REC, UW, Archive, Parties
from marden.seed.cases_quarantine import OPS_FAC_KEYS, TABLE_KEYS, scrub_text, strip
from marden.seed.cases_sink import FactSink, ModelSink, basis_tag, first_url, num, usd_bn, usd_m, weakest
from marden.seed.common import host_of, parse_date, slugify, tag_for_url

LICENSED_HOSTS = ("silicondata.com",)  # commercial index: redistribution not confirmed (gpu.lic)
AGGREGATOR_HOSTS = ("gpuperhour.com",)  # price trackers: the provider is named in the label
_CHIP = re.compile(r"\b(GB300|GB200|B300|B200|H200|H100|A100|MI300X|MI325X|MI355X)\b", re.I)
_MONTH_RANGE = re.compile(r"\(([A-Z][a-z]{2})-([A-Z][a-z]{2}) (\d{4})\)")
_QUARTER = re.compile(r"\bQ([1-4]) (\d{4})\b")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def month_end(year: int, month: int) -> date:
    return date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)


def _three(values: Any) -> list[Any]:
    seq = list(values) if isinstance(values, list) else []
    return [*seq, None, None, None][:3]


def period_end(text: Any) -> date | None:
    """'Q2 2026' -> 2026-06-30; 'Q4 FY26 (Apr-Jun 2026)' / 'FQ1 FY27 (Jun-Aug 2026)' -> the last month's end."""
    s = str(text or "")
    if m := _MONTH_RANGE.search(s):
        return month_end(int(m[3]), _MONTHS[m[2]])
    if m := _QUARTER.search(s):
        return month_end(int(m[2]), int(m[1]) * 3)
    return None


def quarter_month(t: Any) -> str | None:
    """2026.75 -> '2026-10' (the archive's fractional years)."""
    v = num(t)
    if v is None:
        return None
    year, frac = int(v), v - int(v)
    return f"{year}-{min(12, round(frac * 12) + 1):02d}"


def _gpu_collateral(sink: FactSink, arc: Archive) -> dict[str, list[str]]:
    """GPU LTV per loan (M) and the chip model (tag by its basis). Returns loan name -> ids."""
    src, ids_of = arc.src(UW), {}
    for loan in arc.data[UW].get("loans") or []:
        ids = arc.known(loan.get("ids"))
        ids_of[loan.get("loan")] = ids
        url = first_url(loan.get("src"))
        units = loan.get("units") or []
        value = {
            "loan": loan.get("loan"), "borrower": loan.get("borrower"), "contract": loan.get("contract"),
            "chip": loan.get("chip"), "chip_basis": loan.get("chip_basis"), "balance_usd": usd_m(loan.get("bal_m")),
            "balance_source": first_url(loan.get("src_bal")), "gpu_cost_usd": usd_m(loan.get("cost_m")),
            "chips_value_conservative_usd": usd_m(loan.get("coll_m")),
            "chips_value_moderate_usd": usd_m(loan.get("coll_mod_m")), "value_basis": loan.get("coll_basis"),
            "ltv_pct": loan.get("ltv"), "underwater_year_conservative": loan.get("uw_cons"),
            "underwater_year_moderate": loan.get("uw_mod"),
            "underwater_month_conservative": quarter_month(loan.get("uw_cons_q")),
            "chips_only_gap_usd": usd_m(loan.get("gap_m")), "chips_only_gap_when": loan.get("gap_y"),
            "units": dict(zip(("low", "base", "high"), units, strict=False)) or None,
            "cost_per_gpu_usd": [n * 1000 for n in loan.get("per_k") or [] if isinstance(n, int | float)] or None,
            "purchase_month": quarter_month(loan.get("P")), "balloon_share": loan.get("balloon"),
            "contract_detail": loan.get("ct"), "source_title": loan.get("src_t"),
            "ltv_by_quarter": [{"month": quarter_month(r[0]), "balance_usd": usd_m(r[1]),
                                "chips_conservative_usd": usd_m(r[2]), "chips_moderate_usd": usd_m(r[3])}
                               for r in loan.get("ser") or [] if isinstance(r, list) and len(r) >= 4],
        }
        for iid in ids:
            sink.add("instrument", iid, "gpu_collateral", value, src=src, url=url, judged=True)
            sink.add("instrument", iid, "gpu_model", loan.get("chip"), src=src, url=url,
                     tag=basis_tag(loan.get("chip_basis"), url))
    return ids_of


def _recovery(sink: FactSink, models: ModelSink, arc: Archive, ids_of: dict[str, list[str]]) -> None:
    src, rec = arc.src(REC), strip(arc.data[REC], TABLE_KEYS)
    crwv = rec.get("crwv") or {}
    url = first_url(crwv.get("src"))
    per_id: dict[str, dict[str, Any]] = {}
    for scenario, case in (crwv.get("cases") or {}).items():
        for spv in case.get("spv") or []:
            per_id.setdefault(spv.get("id"), {})[scenario] = {
                "recovery_pct": spv.get("rec"), "total_recovery_pct": spv.get("tot"),
                "chip_value_usd": usd_m(spv.get("val")), "balance_usd": usd_m(spv.get("bal")),
                "recourse": spv.get("recourse")}
    for t in crwv.get("tranches") or []:
        lo_base_hi = t.get("rec") or []
        per_id.setdefault(t.get("id"), {}).update(
            dict(zip(("low", "base", "high"), ({"recovery_pct": r} for r in lo_base_hi), strict=False)))
    for iid, value in per_id.items():
        for known in arc.known([iid]):
            sink.add("instrument", known, "recovery_waterfall", value, src=src, url=url, judged=True)
    if crwv:
        models.add("legacy_recovery", "entity", "coreweave", src.as_of, {"coreweave_waterfall": crwv})
    for row in (rec.get("proj") or {}).get("rows") or []:
        low, base, high = _three(row.get("rec"))
        value = {"low_pct": low, "base_pct": base, "high_pct": high, "with_support_pct": row.get("rec_sup"),
                 "worst_pct": row.get("worst"), "at_completion_pct": row.get("at_done"),
                 "debt_usd": usd_m(row.get("debt_m")), "cost_usd": usd_m(row.get("cost_m")),
                 "cost_basis": row.get("cost_basis"), "mw": row.get("mw"), "pct_complete": row.get("pct"),
                 "pct_basis": row.get("pct_basis"), "funded": row.get("funded"), "tenant": row.get("tenant"),
                 "support": row.get("support"), "support_usd": usd_m(row.get("sup_m")),
                 "debt_per_mw_usd": usd_m(row.get("debt_per_mw"))}
        for iid in arc.known(row.get("ids")):
            sink.add("instrument", iid, "recovery_project", value, src=src, judged=True)
        slug = arc.slugs.get(row.get("key"))
        if slug:
            models.add("legacy_recovery", "site", slug, src.as_of,
                       {"project": value, "name": row.get("name"), "instrument_ids": arc.known(row.get("ids")),
                        "assumptions": (rec.get("proj") or {}).get("asm")})
    for row in rec.get("gpu") or []:
        value = {"balance_usd": usd_m(row.get("bal_m")), "chips_conservative_usd": usd_m(row.get("chips_cons")),
                 "chips_moderate_usd": usd_m(row.get("chips_mod")), "recovery_conservative_pct": row.get("rec_cons"),
                 "recovery_moderate_pct": row.get("rec_mod"), "contract": row.get("contract")}
        for iid in ids_of.get(row.get("loan"), []):
            sink.add("instrument", iid, "recovery_gpu", value, src=src, judged=True)


def _market(sink: FactSink, arc: Archive, stats: dict[str, Any]) -> None:
    gpu, src = arc.gpu, arc.src(AIWCREDIT)
    asof = parse_date(gpu.get("asof"))
    skipped = stats["skipped"].setdefault("GPU index or resale point from a licensed source (Silicon Data)", [])
    skipped += [f"rent {m} {r.get('d')}" for m, rows in (gpu.get("rent") or {}).items() for r in rows]
    for row in gpu.get("lists") or []:
        url = first_url(row.get("src"))
        provider = host_of(url).split(".")[0] or "unknown"
        if any(host_of(url).endswith(h) for h in AGGREGATOR_HOSTS):
            provider = str(row.get("p") or provider).split(" (")[0]
        sid = f"gpu_list_price:{slugify(provider)}:{row.get('m')}"
        sink.add("series", sid, "usd_per_gpu_hr", num(row.get("v")), src=src, url=url, unit="USD/GPU-hr",
                 valid_from=asof)
        sink.add("series", sid, "label", row.get("p"), src=src, url=url, valid_from=asof)
    for row in gpu.get("resale") or []:
        url = first_url(row.get("src"))
        if any(host_of(url).endswith(h) for h in LICENSED_HOSTS):
            skipped.append(f"resale {row.get('d')}")
            continue
        chip = _CHIP.search(f"{row.get('c')} {url}")
        sid = f"gpu_resale:{chip.group(1).upper() if chip else 'unknown'}"
        sink.add("series", sid, "usd_per_gpu", num(row.get("v")), src=src, url=url, unit="USD",
                 valid_from=row.get("d"))
        sink.add("series", sid, "condition", row.get("c"), src=src, url=url, valid_from=row.get("d"))
    for model, bands in (gpu.get("resid") or {}).items():
        sink.add("series", f"gpu_residual:{model}", "residual_pct_of_new",
                 {year: {"conservative": v[0], "moderate": v[1]} for year, v in bands.items() if len(v) == 2},
                 src=src, judged=True, unit="pct", source_name="American Compute residual bands (midpoints)")
    for chip in gpu.get("chips") or []:
        url, sid = first_url(chip.get("csrc")), f"gpu_model:{chip.get('k')}"
        for fld, value, unit in (("name", chip.get("name"), None), ("first_volume_ship", chip.get("ship"), None),
                                 ("tdp_w", chip.get("tdp"), "W"), ("fp8_dense_pflops", chip.get("fp8"), "PFLOPS"),
                                 ("new_price_usd", {"low": chip.get("lo"), "high": chip.get("hi"),
                                                    "basis": chip.get("what")} if chip.get("lo") else None, "USD")):
            # roadmap chips without a source (Rubin Ultra, Feynman) are the archive's estimates (M)
            sink.add("series", sid, fld, value, src=src, url=url, unit=unit, valid_from=asof, judged=url is None)
    pue = gpu.get("pue") or {}
    sink.add("series", "data_center_pue", "pue_range", {"low": pue.get("lo"), "high": pue.get("hi"),
                                                        "note": pue.get("note")},
             src=src, url=first_url(pue.get("src")), valid_from=asof)
    for c in gpu.get("cents") or []:
        year = re.match(r"(\d{4}) annual", str(c.get("period") or ""))
        sid, url = f"eia_industrial_price:{c.get('state')}", first_url(c.get("src"))
        sink.add("series", sid, "cents_per_kwh", c.get("cents"), src=src, url=url, unit="USc/kWh",
                 valid_from=date(int(year[1]), 12, 31) if year else None)
        if year:
            sink.add("series", sid, "cents_per_kwh", c.get("prior_year_cents"), src=src, url=url, unit="USc/kWh",
                     valid_from=date(int(year[1]) - 1, 12, 31))
    sink.add("series", "gpu_market", "licence_notes", gpu.get("lic"), src=src, judged=True, valid_from=asof)


def ensure_operator_entities(arc: Archive, parties: Parties) -> dict[str, str]:
    """Entities for the operator profiles and useful-life companies (created only when missing)."""
    out = {}
    for op in arc.gpu.get("ops") or []:
        out[op["n"]] = parties.ensure(op.get("full") or op["n"], "neocloud", ticker=op.get("tk"), aliases=(op["n"],))
    for life in arc.gpu.get("life") or []:
        out.setdefault(life["c"], parties.ensure(life["c"], "company"))
    return out


def write_operators(sink: FactSink, arc: Archive, eids: dict[str, str], ops: list[dict[str, Any]]) -> None:
    """Operator profiles (entity facts, tag by the 10-Q / release they cite) and their GPU
    facilities (instrument facts when the facility is a loaded instrument)."""
    src = arc.src(AIWCREDIT)
    for op in ops:
        eid, url, day = eids[op["n"]], first_url(op.get("src")), period_end(op.get("per"))
        for fld, value, unit in (
            ("revenue_q_usd", usd_m(op.get("rev")), "USD"), ("revenue_ttm_usd", usd_m(op.get("rev_ttm")), "USD"),
            ("adj_ebitda_q_usd", usd_m(op.get("ebitda")), "USD"),
            ("interest_expense_q_usd", usd_m(op.get("int")), "USD"),
            ("capex_q_usd", usd_m(op.get("capex")), "USD"), ("depreciation_q_usd", usd_m(op.get("dep")), "USD"),
            ("backlog_rpo_usd", usd_bn(op.get("rpo")), "USD"), ("active_it_mw", op.get("mw_a"), "MW"),
            ("contracted_mw", op.get("mw_c"), "MW"), ("gpu_useful_life_years", num(op.get("life")), "years"),
            ("top_customers", op.get("top"), None), ("gpu_secured_debt_usd", usd_m(op.get("gpud")), "USD"),
            ("other_debt_usd", usd_m(op.get("othd")), "USD"), ("contract_terms", op.get("len"), None),
            ("power_cost", op.get("pw"), None), ("operator_role", op.get("role_full"), None),
        ):
            sink.add("entity", eid, fld, value, src=src, url=url, unit=unit, valid_from=day)
        sink.add("entity", eid, "operator_notes", [scrub_text(op.get("notes")), scrub_text(op.get("util")),
                                                   scrub_text(op.get("life_t"))], src=src, judged=True)
        facs = []
        for f in op.get("fac") or []:
            f, furl = strip(f, OPS_FAC_KEYS), first_url(f.get("src"))
            item = {"name": f.get("n"), "amount_usd": usd_m(f.get("amt")), "drawn_usd": usd_m(f.get("drawn")),
                    "rate": scrub_text(f.get("r")), "maturity": f.get("mat"), "amortization": f.get("am"),
                    "collateral": f.get("col"), "customer": f.get("cust"), "instrument_id": f.get("rid"),
                    "case": f.get("case"), "source_url": furl}
            facs.append((item, tag_for_url(furl)))
            for iid in arc.known(f.get("rid")):
                for fld, value, unit in (("commitment_usd", item["amount_usd"], "USD"),
                                         ("drawn_usd", item["drawn_usd"], "USD"), ("rate_text", item["rate"], None),
                                         ("amortization", f.get("am"), None), ("collateral_text", f.get("col"), None),
                                         ("customer_text", f.get("cust"), None)):
                    sink.add("instrument", iid, fld, value, src=src, url=furl, unit=unit, valid_from=day)
        sink.add("entity", eid, "gpu_secured_facilities", [i for i, _ in facs], src=src,
                 url=first_url([i.get("source_url") for i, _ in facs]), tag=weakest([t for _, t in facs]))


def write_useful_lives(sink: FactSink, arc: Archive, eids: dict[str, str]) -> None:
    """GPU depreciation lives (gpu.life): taken by the archive from American Compute's schedule,
    which cites each company's filing (gpu.lic: not re-verified against the filing), so R."""
    src = arc.src(AIWCREDIT)
    via, asof = "American Compute schedule (citing SEC filing)", parse_date(arc.gpu.get("asof"))
    for life in arc.gpu.get("life") or []:
        url = first_url(life.get("src"))
        value = num(life.get("y")) if num(life.get("y")) is not None else life.get("y")
        sink.add("entity", eids[life["c"]], "gpu_useful_life_years", value, src=src, url=url, tag="R",
                 unit="years", source_name=via, valid_from=asof)
        sink.add("entity", eids[life["c"]], "gpu_useful_life_note", life.get("n"), src=src, url=url, tag="R",
                 source_name=via, valid_from=asof)


def write_collateral(sink: FactSink, models: ModelSink, arc: Archive) -> None:
    ids_of = _gpu_collateral(sink, arc)
    uw = arc.data[UW]
    models.add("legacy_gpu_ltv", "series", "gpu_loans", arc.src(UW).as_of,
               {"tiles": uw.get("tiles"), "assumptions": uw.get("asm"), "loans": ids_of})
    _recovery(sink, models, arc, ids_of)


def write_market(sink: FactSink, arc: Archive, stats: dict[str, Any]) -> None:
    _market(sink, arc, stats)
