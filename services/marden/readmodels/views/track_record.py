"""Read model `track_record` ('all'): GET /api/track-record, TrackRecord (apps/api/src/contracts/app.ts;
spec 12 "Track record and methods").

- `cases`, `published`, `total`: the backtest's past repricings (model 'track_record', subject 'all':
  first Marden flag from the dated snapshot -> price below 95 or spread +200bp, lead days; a case still
  being backtested has `backtestRunning`). Until the backtest has run there are no cases (0 of 0), never
  a placeholder list.
- `rules` and `material`: the fixed chain status rules and what counts as material, read from the code
  the status model and the alert engine run (marden.models.materiality.RULE_CODES, method
  docs/methods/status-rules-v0.1.md), so the screen shows the rules exactly as applied.
- `models`: each Marden model that had run by the build day (model_run), its newest version, what it is
  built from, its cadence and its method page. Archive imports (legacy_*) are not Marden models.

The source health table is GET /api/sources/health (live heartbeats), not part of this document.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn
from marden.readmodels.context import business_day, day_end
from marden.readmodels.registry import builder
from marden.readmodels.views.instrument_inputs import day, num
from marden.readmodels.views.monitor_common import Doc, is_iso_date, latest_output_any

BACKTEST = "track_record"
STATUS_METHOD_URL = "docs/methods/status-rules-v0.1.md"
FLAG_BASES = ("snapshot", "reconstructed")
LINKS = (("E", "End user"), ("T", "Tenant"), ("D", "Developer"), ("L", "Lenders"))

# What each model is built from, when it runs (ARCHITECTURE "Queue and schedule", spec 22 "What runs
# when") and its method page under docs/methods/ (None until the page is written); the version shown is
# the newest one that ran (model_run).
MODEL_INFO: dict[str, tuple[str, str, str, str | None]] = {
    "status": ("Chain status", "Site facts and events, chain parties' funding and ratings, instrument terms, "
               "the Marden price", "Daily 09:53 UTC and on each published input", "status-rules-v0.1.md"),
    "cushion": ("Cushion and projected completion", "Milestone dates and slips, EIA-860M planned vs actual, "
                "lease outside dates", "Daily 09:35 UTC and on each published input", "cushion-v0.1.md"),
    "price": ("Marden price", "Tier comparable curves, fund marks, TRACE and dealer quotes, bond-implied CDS, "
              "chain status", "Daily 09:40 UTC and on each material event", "pricing-v0.1.md"),
    "curve": ("Credit curve by tier", "Issue comps, tier spreads and the Treasury par curve",
              "Daily, with the price model", "pricing-v0.1.md"),
    "mark_gap": ("Mark gap", "N-PORT and BDC fund marks vs the Marden price", "Daily 09:48 UTC",
                 "mark-gap-v0.1.md"),
    "recovery": ("Recovery", "Collateral waterfall: GPU value, powered shell", "Mondays 09:41 UTC",
                 "recovery-v0.1.md"),
    "gpu_index": ("GPU price index", "Public cloud and neocloud GPU price pages", "Daily 09:23 UTC",
                  "gpu-collateral-v0.1.md"),
    "gpu_model": ("GPU value by model and age", "GPU index, launch dates and new prices, resale listings, "
                  "useful lives in 10-Ks", "Daily 09:23 UTC", "gpu-collateral-v0.1.md"),
    "gpu_collateral": ("GPU collateral and LTV", "Units per facility, GPU value by age, facility balances",
                       "Daily 09:27 UTC", "gpu-collateral-v0.1.md"),
    "gpu_deployment": ("Delivered vs energized GPUs", "Delivery disclosures, energized IT MW, company "
                       "construction in progress vs PP&E", "Daily 09:31 UTC", "gpu-collateral-v0.1.md"),
    "stress": ("Stress scenarios", "Prices, recoveries, chain links and holders", "On request and nightly",
               None),
    "obligations": ("Obligation map", "Filed and reported contracts between companies", "On each published edge",
                    None),
    "obligation_company": ("Company obligations", "Obligation edges at either end", "On each published edge",
                           None),
    "payer_capacity": ("Payer capacity and downgrade risk", "XBRL cash flows, leases, commitments, ratings",
                       "On each filing", None),
    "leases": ("Lease map", "10-K lease notes, lease and compute contracts", "On each filing", None),
    "capacity": ("Capacity and GPU estimate", "Utility load, generation, cooling, footprints, disclosed GPUs",
                 "On each published input", None),
    "contagion": ("Contagion", "Obligation map edges and payers' capacity to pay", "On request", None),
    "risk_list": ("Risk ranking", "Status, cushion, mark gap and recent slips", "Sunday night, published Monday",
                  None),
    "track_record": ("Backtest", "Status, price and cushion signals (live snapshots, else point-in-time "
                     "reconstructions) vs market price moves", "Weekly, Sunday 21:17 UTC, and on new prices",
                     "track-record-v0.1.md"),
}


def _rule_codes() -> dict[str, tuple[str, str | None, str]]:
    from marden.models.materiality import RULE_CODES  # the rules the models apply (models import readmodels)

    return RULE_CODES


def _status_method() -> str:
    from marden.models.status_rules import METHOD_VERSION

    return METHOD_VERSION


def status_rules() -> list[Doc]:
    """The chain status rules table (spec 12): link, amber when, red when, with their rule codes."""
    codes = _rule_codes()
    rows = []
    for letter, link in LINKS:
        amber, red = codes[f"{letter}1"][0], codes[f"{letter}2"][0]
        rows.append({"link": link, "amberCode": f"{letter}1", "amberWhen": amber.split(": ", 1)[-1],
                     "redCode": f"{letter}2", "redWhen": red.split(": ", 1)[-1]})
    return rows


def material_rules() -> list[Doc]:
    """What counts as material (00-global "Materiality and alerts"): the M codes the alert engine tests."""
    return [{"code": code, "rule": rule, "alert": alert, "severity": severity}
            for code, (rule, alert, severity) in _rule_codes().items() if code.startswith("M")]


def _version_label(model: str, version: str) -> str:
    if version[:1].isdigit():
        return f"{model} v{version}"
    return version if model in version else f"{model} {version}"


def model_rows(conn: Conn) -> list[Doc]:
    """Marden models that had run by the build day, with their newest version."""
    runs = conn.execute(
        """SELECT DISTINCT ON (model) model, version, started_at FROM model_run
            WHERE started_at < %s AND model NOT LIKE 'legacy%%' AND ok IS NOT FALSE
            ORDER BY model, started_at DESC, id DESC""",
        (day_end(),),
    ).fetchall()
    rows = []
    for r in runs:
        info = MODEL_INFO.get(r["model"])
        if info is None:
            continue
        output, built_from, cadence, page = info
        rows.append({"model": r["model"], "output": output, "version": _version_label(r["model"], str(r["version"])),
                     "builtFrom": built_from, "cadence": cadence, "methodUrl": f"docs/methods/{page}" if page else None,
                     "lastRun": business_day(r["started_at"]).isoformat()})
    order = list(MODEL_INFO)
    return sorted(rows, key=lambda row: order.index(row["model"]))


def case_of(raw: Any) -> Doc | None:
    """TrackRecordCase from a backtest case, or None without a name."""
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        return None
    lead = num(raw.get("leadDays"))
    return {
        "name": raw["name"],
        "instrumentId": raw["instrumentId"] if isinstance(raw.get("instrumentId"), str) else None,
        "flagged": raw["flagged"] if is_iso_date(raw.get("flagged")) else None,
        "moved": raw["moved"] if is_iso_date(raw.get("moved")) else None,
        "leadDays": int(lead) if lead is not None else None,
        "backtestRunning": bool(raw.get("backtestRunning")),
        # spec 12: flags come only from snapshots taken at the time; a point-in-time reconstruction says so
        "flagBasis": raw["flagBasis"] if raw.get("flagBasis") in FLAG_BASES else None,
    }


def backtest_of(out: Any, body: dict[str, Any]) -> Doc | None:
    """The backtest's method and how its flag dates were obtained (None until it has run)."""
    if out is None:
        return None
    version = body.get("methodVersion") if isinstance(body.get("methodVersion"), str) else str(out["version"])
    return {"methodVersion": version,
            "methodUrl": body["methodUrl"] if isinstance(body.get("methodUrl"), str) else None,
            "flagBasis": body["flagBasis"] if isinstance(body.get("flagBasis"), str) else None,
            "asOf": out["as_of"].isoformat()}


@builder("track_record", many=True)
def build_track_record(conn: Conn):
    out = latest_output_any(conn, BACKTEST, "all")
    body: dict[str, Any] = out["output"] if out is not None and isinstance(out["output"], dict) else {}
    cases = [c for raw in body.get("cases") or [] if (c := case_of(raw)) is not None]
    published = sum(1 for c in cases if not c["backtestRunning"])
    total = num(body.get("total"))
    yield "all", {
        "asOf": day().isoformat(),
        "published": published,
        "total": max(int(total), len(cases)) if total is not None else len(cases),
        "cases": cases,
        "rules": status_rules(),
        "material": material_rules(),
        "statusMethod": {"version": _status_method(), "url": STATUS_METHOD_URL},
        "backtest": backtest_of(out, body),
        "models": model_rows(conn),
    }
