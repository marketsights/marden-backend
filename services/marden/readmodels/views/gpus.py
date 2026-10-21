"""Read models `gpus` ('all'), `gpu_model` (the `:model` path segment) and `gpu_deployment` ('all'):
GET /api/gpus, GET /api/gpus/:model and GET /api/gpus/deployment (spec 19).

All three carry the GPU models' outputs (marden.models.gpu_index / gpu_deployment, method page
docs/methods/gpu-collateral-v0.1.md), the latest one dated on or before the build day:

- `gpus` = GpuIndexPage (apps/api/src/contracts/app.ts): the 'gpu_index' output's index points
  (GpuPricePoint[], every capture day) and each provider's newest price (GpuProviderPrice[]), plus the
  value-per-unit curve by age of each model with a 'gpu_model' output (spec 19 "GPU index" screen).
- `gpu_model` = GpuModelDetail for each model with a 'gpu_model' output.
- `gpu_deployment` = GpuDeploymentPage from the 'gpu_deployment' output for 'all'.

Each piece is checked against its types.ts shape and only the type's keys are kept (the outputs'
`missing` and `basis` stay in model_output). Until a model has run, its lists are empty and its
values n/d. The GPU tasks mark these views themselves (views.SCHEDULED_VIEWS for gpus and gpu_model;
gpu_deployment also follows campus and company PP&E facts, deps.py).
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.registry import builder
from marden.readmodels.views.gpu_collateral import DEPLOYMENT_SOURCED, DEPLOYMENT_STATUSES
from marden.readmodels.views.instrument_inputs import day, nd, num
from marden.readmodels.views.monitor_common import (
    Doc,
    evidence_of,
    is_iso_date,
    items_of,
    latest_output_any,
    method_of,
    output_subjects,
    sourced_of,
)

TERMS = ("on-demand", "1y", "3y")


# ---------------------------------------------------------------- GPU index pieces
def price_point(raw: Any) -> Doc | None:
    """types.ts GpuPricePoint, or None when a field is missing."""
    if not isinstance(raw, dict) or raw.get("term") not in TERMS or not is_iso_date(raw.get("date")):
        return None
    if not isinstance(raw.get("model"), str) or not isinstance(raw.get("region"), str):
        return None
    nums = {k: num(raw.get(k)) for k in ("medianUsdHr", "p25", "p75", "nProviders")}
    if any(v is None for v in nums.values()):
        return None
    return {"date": raw["date"], "model": raw["model"], "term": raw["term"], "region": raw["region"],
            "medianUsdHr": nums["medianUsdHr"], "p25": nums["p25"], "p75": nums["p75"],
            "nProviders": int(nums["nProviders"])}  # type: ignore[arg-type]


def provider_price(out: Row, raw: Any) -> Doc | None:
    """types.ts GpuProviderPrice: the provider's captured price with its page (F/R as captured)."""
    if not isinstance(raw, dict):
        return None
    texts = ("provider", "model", "term", "region", "capturedAt", "pageUrl")
    if not all(isinstance(raw.get(k), str) for k in texts):
        return None
    usd = sourced_of(out, raw.get("usdHr"), "number", "F")
    if usd["value"] is None:
        return None
    return {**{k: raw[k] for k in texts[:4]}, "usdHr": usd, "capturedAt": raw["capturedAt"], "pageUrl": raw["pageUrl"]}


def value_by_age(out: Row, raw: Any) -> list[Doc]:
    rows = []
    for p in raw if isinstance(raw, list) else []:
        if isinstance(p, dict) and num(p.get("age")) is not None:
            rows.append({"age": num(p["age"]), "usd": sourced_of(out, p.get("usd"), "number")})
    return sorted(rows, key=lambda r: r["age"])


def resale_rows(raw: Any) -> list[Doc]:
    rows = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or not is_iso_date(r.get("date")) or num(r.get("usd")) is None:
            continue
        rows.append({"date": r["date"], "usd": num(r["usd"]),
                     "source": r.get("source") if isinstance(r.get("source"), str) else "",
                     "url": r.get("url") if isinstance(r.get("url"), str) else ""})
    return sorted(rows, key=lambda r: r["date"])


def facility_rows(out: Row, raw: Any) -> list[Doc]:
    rows = []
    for f in raw if isinstance(raw, list) else []:
        if isinstance(f, dict) and isinstance(f.get("instrumentId"), str) and isinstance(f.get("name"), str):
            rows.append({"instrumentId": f["instrumentId"], "name": f["name"],
                         "units": sourced_of(out, f.get("units"), "number")})
    return rows


def assumptions_of(out: Row, raw: Any) -> Doc | None:
    """GpuModelDetail.assumptions: utilization, decline and haircut are the model's numbers (no n/d in
    the type), so an output without them has no page."""
    if not isinstance(raw, dict):
        return None
    nums = {k: num(raw.get(k)) for k in ("utilization", "annualDecline", "haircut")}
    if any(v is None for v in nums.values()):
        return None
    method = raw.get("methodVersion") if isinstance(raw.get("methodVersion"), str) else method_of(out)
    return {**nums, "usefulLifeYears": sourced_of(out, raw.get("usefulLifeYears"), "number"), "methodVersion": method}


def model_detail(out: Row) -> Doc | None:
    """GpuModelDetail from one 'gpu_model' output."""
    body = out["output"] if isinstance(out["output"], dict) else {}
    model = body.get("model") if isinstance(body.get("model"), str) else out["subject_id"]
    assumptions = assumptions_of(out, body.get("assumptions"))
    if assumptions is None:
        return None
    return {
        "model": model,
        "launch": sourced_of(out, body.get("launch"), "string", "R"),
        "newPrice": sourced_of(out, body.get("newPrice"), "number", "R"),
        "index": sorted(items_of(body.get("index"), price_point), key=lambda p: (p["date"], p["term"], p["region"])),
        "providers": [p for raw in body.get("providers") or [] if (p := provider_price(out, raw)) is not None],
        "resale": resale_rows(body.get("resale")),
        "valueByAgeYears": value_by_age(out, body.get("valueByAgeYears")),
        "assumptions": assumptions,
        "facilities": facility_rows(out, body.get("facilities")),
    }


def gpu_model_keys(conn: Conn) -> list[str]:
    return output_subjects(conn, "gpu_model")


@builder("gpu_model", keys=gpu_model_keys)
def build_gpu_model(conn: Conn, key: str) -> Doc | None:
    out = latest_output_any(conn, "gpu_model", key)
    return model_detail(out) if out is not None else None


def value_curves(conn: Conn) -> list[Doc]:
    """GpuIndexPage.valueByAge: each model's value-per-unit curve by age (its 'gpu_model' output)."""
    curves = []
    for model in gpu_model_keys(conn):
        out = latest_output_any(conn, "gpu_model", model)
        body = out["output"] if out is not None and isinstance(out["output"], dict) else {}
        points = value_by_age(out, body.get("valueByAgeYears")) if out is not None else []
        if not points or all(p["usd"]["value"] is None for p in points):
            continue
        curves.append({"model": model, "launch": sourced_of(out, body.get("launch"), "string", "R"),
                       "newPrice": sourced_of(out, body.get("newPrice"), "number", "R"), "points": points})
    return curves


@builder("gpus", many=True)
def build_gpus(conn: Conn):
    out = latest_output_any(conn, "gpu_index", "all")
    body = out["output"] if out is not None and isinstance(out["output"], dict) else {}
    index = sorted(items_of(body.get("index"), price_point), key=lambda p: (p["date"], p["model"], p["term"]))
    providers = [p for raw in body.get("providers") or [] if (p := provider_price(out, raw)) is not None] if out else []
    providers.sort(key=lambda p: (p["model"], p["provider"], p["term"], p["region"]))
    yield "all", {"asOf": day().isoformat(), "index": index, "providers": providers, "valueByAge": value_curves(conn)}


# ---------------------------------------------------------------- delivered vs energized
def deployment_of(out: Row, body: Any) -> Doc | None:
    """types.ts GpuDeployment from a facility row of the 'gpu_deployment' output, or None when a field
    the type requires is missing (as views/gpu_collateral.deployment)."""
    if not isinstance(body, dict) or body.get("status") not in DEPLOYMENT_STATUSES:
        return None
    if body.get("scope") not in ("facility", "site") or not isinstance(body.get("name"), str):
        return None
    if not isinstance(body.get("id"), str) or not body["id"]:
        return None
    assumptions = body.get("assumptions")
    if not isinstance(assumptions, dict) or num(assumptions.get("pue")) is None:
        return None
    kw = sourced_of(out, assumptions.get("kwPerUnit"), "number")
    return {
        "scope": body["scope"],
        "id": body["id"],
        "name": body["name"],
        "model": body.get("model") if isinstance(body.get("model"), str) else None,
        **{k: sourced_of(out, body.get(k), "number") for k in DEPLOYMENT_SOURCED},
        "status": body["status"],
        "missing": [m for m in body.get("missing") or [] if isinstance(m, str)],
        "evidence": items_of(body.get("evidence"), evidence_of),
        "assumptions": {
            "kwPerUnit": kw,
            "pue": num(assumptions["pue"]),
            "couponPct": sourced_of(out, assumptions.get("couponPct"), "number", "F"),
            "methodVersion": assumptions["methodVersion"] if isinstance(assumptions.get("methodVersion"), str)
            else kw.get("methodVersion") or method_of(out),
        },
    }


def company_cip(out: Row, raw: Any) -> Doc | None:
    """types.ts CompanyCip: construction in progress vs PP&E in service per company and quarter."""
    if not isinstance(raw, dict) or not isinstance(raw.get("company"), str) or not isinstance(raw.get("quarter"), str):
        return None
    return {"company": raw["company"], "quarter": raw["quarter"],
            "cipB": sourced_of(out, raw.get("cipB"), "number", "F"),
            "ppeInServiceB": sourced_of(out, raw.get("ppeInServiceB"), "number", "F"),
            "cipShare": num(raw.get("cipShare")), "changeQoQB": num(raw.get("changeQoQB"))}


@builder("gpu_deployment", many=True)
def build_gpu_deployment(conn: Conn):
    out = latest_output_any(conn, "gpu_deployment", "all")
    body = out["output"] if out is not None and isinstance(out["output"], dict) else {}
    totals = body.get("totals") if isinstance(body.get("totals"), dict) else {}
    keys = ("deliveredUnits", "energizedUnits", "idleShare")
    yield "all", {
        "asOf": day().isoformat(),
        "facilities": [f for raw in body.get("facilities") or [] if out and (f := deployment_of(out, raw))],
        "companies": [c for raw in body.get("companies") or [] if out and (c := company_cip(out, raw))],
        "totals": {k: sourced_of(out, totals.get(k), "number") if out else nd("M") for k in keys},
    }
