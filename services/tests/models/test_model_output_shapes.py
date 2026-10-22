"""Every model's stored output is exactly the types.ts piece its read-model builder drops in.

The outputs are read (read-only) from the local `marden` database: the newest stored day of each model,
i.e. what the builders actually serve. Each output's piece is compiled with tsc --strict against the
interfaces in handoff/api/types.ts (`T.*`) and the API's own contracts (`A.*`) by
tests/models/ts_shapes_models.assert_matches_ts, so extra keys, wrong literals and unexpected nulls fail.
Every Sourced value must also name its source (hard rule 4).

Fields types.ts declares non-null that a model leaves n/d (null + a `missing` entry, 00-global data rule
3: never 0) are listed per model with `NDFields<...>`; nothing else may be null. The bookkeeping keys
every output adds (`missing`, `basis`, `request`, ...) are checked to be there and then dropped.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import psycopg
import pytest
from conftest import BASE_URL
from psycopg.rows import dict_row
from ts_shapes_models import APP_TYPES, assert_matches_ts

MAX_DOCS = 60
SITE_TABS = f"import type * as S from {json.dumps(str(APP_TYPES.with_name('site-tabs')))};"
Pick = Callable[[str, Any], Any]


def _drop(*keys: str) -> Pick:
    return lambda _sid, o: {k: v for k, v in o.items() if k not in keys}


def _keep(*keys: str) -> Pick:
    return lambda _sid, o: {k: o[k] for k in keys}


# (model, subject_type, subject filter, TypeScript type of the piece, how to cut the piece from the output)
SPECS: list[tuple[str, str, Callable[[str], bool], str, Pick]] = [
    ("status", "site", lambda s: True,
     "Pick<T.SiteSummary, 'chain' | 'status'> & { missing: string[] }", _keep("chain", "status", "missing")),
    ("cushion", "site", lambda s: True,
     "{ projection: NDFields<T.CushionProjection, 'p10' | 'p50' | 'p90'>; milestones: T.Milestone[]; "
     "driver: ND<T.CompletionRow['driver']>; missing: string[] }",
     _keep("projection", "milestones", "driver", "missing")),
    ("cushion", "site", lambda s: True,  # the Lease tab's pieces (apps/api/src/contracts/site-tabs.ts)
     "{ leaseOutcomes: S.SiteLeaseTab['outcomes']; leasePosition: S.SiteLeaseTab['today'] | null; "
     "notApplicable: T.Milestone['key'][] }", _keep("leaseOutcomes", "leasePosition", "notApplicable")),
    ("price", "instrument", lambda s: True,
     "Pick<T.InstrumentRow, 'mardenPrice'> & { price: number | null; spreadBp: number | null; "
     "yieldPct: number | null; change1w: number | null; recovery: number | null; "
     "band: { low: number; high: number; label: 'model v0'; level: number } | null; "
     "inputs: (T.Sourced<number> & { key: string; label: string; unit: string })[]; missing: string[]; "
     "methodVersion: 'price v0.1' }",
     _keep("mardenPrice", "price", "spreadBp", "yieldPct", "change1w", "recovery", "band", "inputs", "missing",
           "methodVersion")),
    ("cds", "entity", lambda s: True,
     "{ label: 'Marden bond-implied CDS'; spreadBp: number | null; upfrontPct: number | null; "
     "hazard: number | null; maturity: string | null; years: number | null; "
     "band: { low: number; high: number; label: 'model v0'; level: number } | null; missing: string[]; "
     "methodVersion: 'price v0.1' }",
     _keep("label", "spreadBp", "upfrontPct", "hazard", "maturity", "years", "band", "missing", "methodVersion")),
    ("curve", "series", lambda s: s == "all", "T.CurveTier[]", lambda _s, o: o),
    ("mark_gap", "instrument", lambda s: True,
     "{ holders: NDFields<T.Holder, 'parMm' | 'lastMark' | 'gapPts' | 'navHitMm' | 'shareOfNav'>[]; count: number; "
     "parMm: number; "
     "fundMarks: T.InstrumentDetail['fundMarks'] | null; "
     "nextMarksPublic: string | null; markGap: T.InstrumentRow['markGap']; "
     "mardenPrice: T.Sourced<number> | null; missing: string[] }",
     _drop("basis")),
    ("recovery", "instrument", lambda s: True,
     "{ recovery: T.Sourced<string>; valueB: T.Sourced<number>; method: 'gpu_collateral' | 'powered_shell' | null; "
     "missing: string[] }", _drop("basis")),
    ("gpu_index", "series", lambda s: s == "all",
     "{ index: T.GpuPricePoint[]; providers: T.GpuProviderPrice[] }", _keep("index", "providers")),
    ("gpu_model", "gpu_model", lambda s: True, "T.GpuModelDetail", _drop("basis", "missing")),
    ("gpu_collateral", "instrument", lambda s: True, "T.GpuCollateral", _drop("basis", "missing")),
    ("gpu_deployment", "series", lambda s: s == "all", "T.GpuDeploymentPage", _drop("basis", "missing")),
    ("obligations", "series", lambda s: s == "all", "T.ObligationGraph", _drop("basis", "missing")),
    ("obligations", "obligation", lambda s: True, "T.ObligationEdge", _drop("basis", "missing")),
    ("obligation_company", "entity", lambda s: True, "T.CompanyObligations", _drop("basis", "missing")),
    ("payer_capacity", "entity", lambda s: True, "T.PayerCapacity", _drop("basis", "missing")),
    ("leases", "series", lambda s: s == "all",
     "Omit<T.LeaseMapPage, 'totals'> & { totals: NDFields<T.LeaseMapPage['totals'], "
     "'disclosedB' | 'unattributedB' | 'attributedShare' | 'triggerLeases'> }", _drop("basis", "missing")),
    ("capacity", "campus", lambda s: True, "T.CapacityEstimate", _drop("missing")),
    ("capacity", "building", lambda s: True, "T.CapacityEstimate", _drop("missing")),
    ("stress", "scenario", lambda s: not s.startswith(("contagion:", "custom:")),
     "Omit<T.StressResult, 'kpis' | 'flow' | 'rows'> & { kpis: NDFields<T.StressResult['kpis'], 'valueLostB' | "
     "'valueLostPct' | 'fundsHit'>; flow: NDFields<T.StressResult['flow'][number], 'priceChange'>[]; "
     "rows: NDFields<T.StressResult['rows'][number], 'priceAfter'>[] }",
     lambda s, o: _rows_without_walk(_drop("basis", "missing", "request")(s, o))),
    ("stress", "scenario", lambda s: s.startswith("contagion:"), "NDFields<T.ContagionResult, 'totalAtRiskB'>",
     _drop("basis", "missing", "request")),
    ("stress", "instrument", lambda s: True, "{ rows: T.InstrumentDetail['stress'] }", _keep("rows")),
    ("stress", "entity", lambda s: True,
     "{ stressSummary: NDFields<NonNullable<T.Payer['stressSummary']>, 'notesLossB'> }", _keep("stressSummary")),
    ("risk_list", "week", lambda s: True, "T.RiskList", _drop("basis", "missing")),
    ("track_record", "series", lambda s: s == "all",
     "{ cases: Pick<A.TrackRecordCase, 'name' | 'instrumentId' | 'flagged' | 'moved' | 'leadDays' | "
     "'backtestRunning' | 'flagBasis'>[]; earlierCalls: T.RiskList['earlierCalls']; total: number; "
     "published: number; asOf: string }",
     lambda _s, o: {"cases": [{k: c[k] for k in ("name", "instrumentId", "flagged", "moved", "leadDays",
                                                  "backtestRunning", "flagBasis")} for c in o["cases"]],
                    "earlierCalls": o["earlierCalls"], "total": o["total"], "published": o["published"],
                    "asOf": o["asOf"]}),
]
def _rows_without_walk(doc: dict[str, Any]) -> dict[str, Any]:
    """A stress row's `walk` (the waterfall behind a walked row's price, stress_walk) is bookkeeping, like basis."""
    return {**doc, "rows": [{k: v for k, v in r.items() if k != "walk"} for r in doc.get("rows", [])]}


BOOKKEEPING = {  # keys a model adds beside its piece; they must be there (a missing list is never absent)
    "status": ("missing",), "cushion": ("missing", "basis"), "price": ("missing", "basis"),
    "mark_gap": ("missing", "basis"), "recovery": ("missing", "basis"), "gpu_index": ("missing", "basis"),
    "payer_capacity": ("missing", "basis"), "leases": ("missing", "basis"), "risk_list": ("missing", "basis"),
    "capacity": ("missing",), "track_record": ("missing",),
}


def _stored(model: str, subject_type: str) -> tuple[date, list[tuple[str, Any]]]:
    try:
        src = psycopg.connect(BASE_URL, connect_timeout=5, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        pytest.skip(f"source database not reachable: {exc}")
    with src:
        day = src.execute("SELECT max(as_of) d FROM model_output WHERE model = %s AND subject_type = %s",
                          (model, subject_type)).fetchone()["d"]
        if day is None:
            pytest.skip(f"no stored {model} output on {subject_type} in the source database")
        rows = src.execute("""SELECT DISTINCT ON (subject_id) subject_id, output FROM model_output
                               WHERE model = %s AND subject_type = %s AND as_of = %s
                               ORDER BY subject_id, created_at DESC, id DESC""", (model, subject_type, day)).fetchall()
    return day, [(r["subject_id"], r["output"]) for r in rows]


@pytest.mark.parametrize(("model", "subject_type", "keep", "ts_type", "cut"), SPECS,
                         ids=[f"{m}:{st}:{i}" for i, (m, st, *_rest) in enumerate(SPECS)])
def test_stored_output_is_its_types_ts_piece(model, subject_type, keep, ts_type, cut):
    _day, rows = _stored(model, subject_type)
    rows = [(sid, out) for sid, out in rows if keep(sid)][:MAX_DOCS]
    if not rows:
        pytest.skip(f"no {model} output on the chosen {subject_type} subjects")
    for sid, out in rows:
        for key in BOOKKEEPING.get(model, ()):
            assert isinstance(out, dict) and key in out, f"{model} {sid}: no `{key}`"
        if isinstance(out, dict) and "missing" in out:
            assert isinstance(out["missing"], list) and all(isinstance(m, str) for m in out["missing"])
    assert_matches_ts(ts_type, [cut(sid, out) for sid, out in rows], prelude=SITE_TABS)


def test_status_chain_is_four_links_in_role_order():
    _day, rows = _stored("status", "site")
    for sid, out in rows:
        assert [link["role"] for link in out["chain"]] == ["end_user", "tenant", "developer", "lenders"], sid


def test_nd_fields_are_null_with_a_missing_entry_never_zero():
    """Where a piece allows n/d, a null comes with a `missing` entry (spot checks on the n/d fields)."""
    _day, rows = _stored("cushion", "site")
    for sid, out in rows:
        if out["projection"]["p50"] is None:
            assert out["missing"], f"cushion {sid}: n/d projection without a missing entry"
    _day, rows = _stored("leases", "series")
    for _sid, out in rows:
        totals = out["totals"]
        assert totals["disclosedB"] != 0 and totals["triggerLeases"] != 0  # n/d is null, never 0
    _day, rows = _stored("payer_capacity", "entity")
    for sid, out in rows:
        for year in out["years"]:
            for key in ("leaseCommencingB", "computeCommitmentsB", "fundingGapB"):
                if year[key]["value"] is None:
                    assert out["missing"], f"payer_capacity {sid}: {key} n/d without a missing entry"


def test_nd_never_stored_as_zero_in_the_review_cases():
    """The n/d cases fixed in the model review stay null with a reason (00-global data rule 3)."""
    _day, rows = _stored("price", "instrument")
    for sid, out in rows:
        basis = out.get("basis") or {}
        if basis.get("mode") == "facility" and not any(k in (basis.get("today") or {})
                                                       for k in ("tenantHazard", "obligorHazard", "guarantorHazard")):
            assert basis.get("defaultOddsToMaturity") is None, f"price {sid}: default odds with no credit leg"
            assert any("default odds n/d" in m for m in out["missing"]), sid
    _day, rows = _stored("cds", "entity")
    for sid, out in rows:
        eq = out.get("equity") or {}
        if eq and not eq.get("pairs") and not eq.get("prior"):
            assert eq.get("loading") is None, f"cds {sid}: equity loading 0 for an equity model that did not run"
    _day, rows = _stored("stress", "scenario")
    for sid, out in rows:
        kpis = out.get("kpis")
        if kpis and kpis.get("valueLostB") is None and sid.endswith(":severe"):
            assert kpis["fundsHit"] is None, f"stress {sid}: fundsHit counted with prices n/d"
        if sid.startswith("contagion:") and any("has no annual schedule" in m for m in out["missing"]):
            assert out["totalAtRiskB"] != 0, f"stress {sid}: totalAtRiskB 0 with unscheduled edges"
    _day, rows = _stored("mark_gap", "instrument")
    for sid, out in rows:
        for h in out["holders"]:
            assert h["parMm"] is None or h["parMm"] > 0, f"mark_gap {sid}: holder {h['name']} with par {h['parMm']}"
