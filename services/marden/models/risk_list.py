"""Model `risk_list` (spec 10, spec 22 "Risk ranking"): which instruments to look at this week.

    python -m marden model risk_list [--as-of YYYY-MM-DD] [--force] [--json]

A rule-based score until the backtest supports a fitted model. One output per published week, subject
('week', '<Monday YYYY-MM-DD>') = RiskList (types.ts) + `missing`, `basis`. The week is the Monday of the
as-of day (a Sunday run publishes the next day's list). A list is frozen once published: a later run for a
week that already has a list does nothing unless --force (a backtest re-run).

Universe: the V1 instruments the price model has priced on or before the as-of day. Score (points):
- chain status of its site (status model): Broken 3, Strained 2;
- cushion (cushion model): below zero 3, 0-3 months 2, 3-6 months 1; P50 later on the week 1;
- a dated schedule change on its site in the last 30 days (`schedule_change`) 1;
- mark gap (mark-gap model): 5+ points 2, 2+ points 1;
- its tenant's downgrade risk (payer_capacity): High 2, Watch 1 (spec 24: a payer at Watch or High raises
  every linked instrument);
- Marden price moved 2+ points on the week 1.
odds of a 3+ point move in 90 days = score / (score + 6), 'odds v0' (uncalibrated). Rank by odds, ties by
size. Up to 15 rows with a positive score. `why` lists the records behind the points (risk_note), the note
is filled by code templates from those numbers. `move` compares with the previous week's list.
earlierCalls: earlier lists' rows whose Marden price then moved 3+ points (first price output that did,
leadDays from the list's week).

Runs: Sundays 23:40 ET (cron 40 3 * * 1 UTC, the Monday's list), plus a dirty-mark runner that only
writes a week with no list yet (so a published list stays frozen).
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from typing import Any

from marden import cli
from marden.core.db import Conn
from marden.core.queue import app
from marden.models import pit, risk_note, triggers
from marden.models.runs import latest_output, outputs_between
from marden.models.sourced import Batch, m_value, method_version, num, run_outputs
from marden.readmodels.context import business_today
from marden.readmodels.views.instrument_inputs import instrument_identity
from marden.readmodels.views.instrument_text import display_name

MODEL, VERSION = "risk_list", "0.1"
METHOD = method_version(MODEL, VERSION)
ODDS_METHOD = "odds v0"
CRON_UTC = "40 3 * * 1"  # Monday 03:40 UTC = Sunday 23:40 EDT
MAX_ROWS = 15
ODDS_K = 6.0
MOVE_PTS = 3.0


def week_of(day: date) -> date:
    return day + timedelta(days=1) if day.weekday() == 6 else day - timedelta(days=day.weekday())


def _out(conn: Conn, model: str, st: str, sid: str | None, day: date) -> dict[str, Any] | None:
    if not sid:
        return None
    row = latest_output(conn, model, st, sid, day)
    return row["output"] if row is not None and isinstance(row["output"], dict) else None


def _size(row: Any) -> tuple[float | None, str]:
    v = num(row["value"]) if row is not None else None
    if v is None:
        return None, "n/d"
    b = v / 1e9
    return v, f"${b:,.2f}B" if b < 1 else f"${b:,.1f}B"


def score_one(conn: Conn, inst: dict[str, Any], day: date, ctx: dict[str, Any]) -> dict[str, Any] | None:
    iid, site = inst["id"], inst["site_slug"]
    px = _out(conn, "price", "instrument", iid, day)
    if px is None or num(px.get("price")) is None:
        return None
    points, reasons = 0, []
    status = _out(conn, "status", "site", site, day)
    chain = status.get("chain") if status else None
    worst = {"red": 3, "amber": 2}
    for link in sorted([lk for lk in chain or [] if isinstance(lk, dict) and lk.get("status") in worst],
                       key=lambda lk: -worst[lk["status"]])[:1]:
        points += worst[link["status"]]
        if (ev := risk_note.status_reason(link, site, day)) is not None:
            reasons.append(ev)
    cushion = _out(conn, "cushion", "site", site, day)
    proj = cushion.get("projection") if cushion else None
    c = num(((proj or {}).get("cushionMonths") or {}).get("value"))
    delta = num(((proj or {}).get("deltaWeek") or {}).get("value"))
    if c is not None and c < 6:
        points += 3 if c < 0 else 2 if c < 3 else 1
        reasons.append(risk_note.cushion_reason(int(c), proj.get("p50"), (proj.get("outsideDate") or {}).get("value"),
                                                int(delta) if delta else None, site, day))
    if delta is not None and delta < 0:
        points += 1
    slips = [r for r in ctx["slips"].get(site, []) if day - timedelta(days=30) <= r["valid_from"] <= day]
    if slips:
        points += 1
        reasons.append(risk_note.slip_reason(slips[0]))
    gap_out = _out(conn, "mark_gap", "instrument", iid, day)
    raw_gap = (gap_out or {}).get("markGap")
    gap = num(raw_gap.get("value")) if isinstance(raw_gap, dict) else num(raw_gap)
    marks = (gap_out or {}).get("fundMarks") or {}
    if gap is not None and abs(gap) >= 2:
        points += 2 if abs(gap) >= 5 else 1
        reasons.append(risk_note.gap_reason(gap, int(num(gap_out.get("count")) or 0), marks.get("filing"),
                                            marks.get("date"), iid, day))
    tenant = ctx["tenants"].get(site)
    band_out = _out(conn, "payer_capacity", "entity", tenant, day)
    band = ((band_out or {}).get("downgradeRisk") or {}).get("value")
    if band in ("High", "Watch"):
        points += 2 if band == "High" else 1
        reasons.append(risk_note.band_reason(tenant, band, (band_out.get("basis") or {}).get("bandReasons") or [], day))
    change = num(px.get("change1w"))
    if change is not None and abs(change) >= 2:
        points += 1
        reasons.append(risk_note.price_reason(change, float(px["price"]), iid, day))
    size_usd, size = _size(ctx["sizes"].get(iid))
    fund_mark: Any = None
    if isinstance(raw_gap, str):
        fund_mark = {"no fund": "no fund holds it"}.get(raw_gap, raw_gap)
    elif marks.get("low") is not None and marks.get("high") is not None:
        fund_mark = round((num(marks["low"]) + num(marks["high"])) / 2, 2)  # type: ignore[operator]
    elif int(num((gap_out or {}).get("count")) or 0) == 0 and gap_out is not None:
        fund_mark = "no fund holds it"
    px_day = date.fromisoformat(px["asOf"]) if isinstance(px.get("asOf"), str) else day
    return {"instrumentId": iid, "name": inst["name"], "size": size, "_size": size_usd or 0.0, "_points": points,
            "why": reasons, "price": m_value(round(float(px["price"]), 3), px_day, "price v0.1",
                                              source_name="Marden model price"),
            "change1w": m_value(round(change, 3), px_day, "price v0.1", source_name="Marden model price")
            if change is not None else None,
            "fundMark": fund_mark, "gap": round(gap, 2) if gap is not None else None}


def earlier_calls(conn: Conn, week: date, day: date) -> list[dict[str, Any]]:
    calls = []
    for prev in conn.execute("SELECT DISTINCT subject_id FROM model_output WHERE model = %s AND subject_id < %s",
                             (MODEL, week.isoformat())).fetchall():
        out = _out(conn, MODEL, "week", prev["subject_id"], day)
        flagged = date.fromisoformat(prev["subject_id"])
        for r in (out or {}).get("rows") or []:
            p0 = num((r.get("price") or {}).get("value"))
            if p0 is None:
                continue
            for o in outputs_between(conn, "price", "instrument", r["instrumentId"], flagged + timedelta(days=1), day):
                p1 = num(o["output"].get("price")) if isinstance(o["output"], dict) else None
                if p1 is not None and abs(p1 - p0) >= MOVE_PTS:
                    calls.append({"name": r["name"], "flagged": flagged.isoformat(), "moved": o["as_of"].isoformat(),
                                  "leadDays": (o["as_of"] - flagged).days})
                    break
    return sorted(calls, key=lambda c: c["flagged"], reverse=True)


def compute(conn: Conn, day: date) -> Batch:
    week = week_of(day)
    insts = [{**i, "name": display_name(i)} for i in instrument_identity(conn, v1_only=True)]
    sites = sorted({i["site_slug"] for i in insts if i["site_slug"]})
    ctx = {"tenants": {r["site_slug"]: r["entity_id"] for r in conn.execute(
               "SELECT site_slug, entity_id FROM chain_party WHERE role = 'tenant' AND site_slug = ANY(%s)",
               (sites,)).fetchall()},
           "sizes": {}, "slips": {}}
    size_rows: dict[str, list[Any]] = {}
    for r in pit.fact_rows(conn, day, "instrument", [i["id"] for i in insts], ["amount_usd"]):
        size_rows.setdefault(r["subject_id"], []).append(r)
    ctx["sizes"] = {k: pit.current_of(v) for k, v in size_rows.items()}
    for r in pit.fact_rows(conn, day, "site", sites, ["schedule_change"]):
        ctx["slips"].setdefault(r["subject_id"], []).append(r)
    scored = [s for i in insts if (s := score_one(conn, i, day, ctx)) is not None]
    missing = [f"instrument {i['id']}: no price model output on or before {day}" for i in insts
               if i["id"] not in {s["instrumentId"] for s in scored}]
    ranked = sorted((s for s in scored if s["_points"] > 0), key=lambda s: (-s["_points"], -s["_size"], s["instrumentId"]))
    prev = conn.execute("SELECT DISTINCT subject_id FROM model_output WHERE model = %s AND subject_id < %s "
                        "ORDER BY 1 DESC LIMIT 1", (MODEL, week.isoformat())).fetchone()
    before = {r["instrumentId"]: r["rank"] for r in ((_out(conn, MODEL, "week", prev["subject_id"], day) or {})
                                                     .get("rows") or [])} if prev else None
    rows, reasons = [], {}
    for rank, s in enumerate(ranked[:MAX_ROWS], start=1):
        odds = round(s["_points"] / (s["_points"] + ODDS_K), 3)
        reasons[s["instrumentId"]] = s["why"]
        was = (before or {}).get(s["instrumentId"])
        move = {"kind": "new"} if was is None else {"kind": "same"} if was == rank else \
            {"kind": "up" if was > rank else "down", "by": abs(was - rank)}
        rows.append({"rank": rank, "instrumentId": s["instrumentId"], "name": s["name"], "size": s["size"],
                     "why": [risk_note.public(e) for e in s["why"]], "price": s["price"], "change1w": s["change1w"],
                     "fundMark": s["fundMark"], "gap": s["gap"],
                     "odds": m_value(odds, day, ODDS_METHOD, source_name="Marden risk ranking"), "move": move})
    batch = Batch()
    batch.add("week", week.isoformat(), {
        "week": {"start": week.isoformat(), "end": (week + timedelta(days=6)).isoformat()},
        "rows": rows, "note": risk_note.note(week, rows, reasons), "earlierCalls": earlier_calls(conn, week, day),
        "missing": missing,
        "basis": {"methodVersion": f"{METHOD} (model v0)", "odds": f"score / (score + {ODDS_K:g})", "scored": len(scored),
                  "points": {s["instrumentId"]: s["_points"] for s in scored}},
    })
    batch.views.add(("risk_list", week.isoformat()))
    batch.inputs = [week.isoformat(), {s["instrumentId"]: s["_points"] for s in scored}]
    return batch


def run(as_of: date | None = None, *, force: bool = False, trigger: str = "manual", mark: bool = True) -> dict[str, Any]:
    day = as_of or business_today()
    week = week_of(day)
    if not force:
        from marden.core import db

        with db.transaction() as conn:
            done = conn.execute("SELECT as_of FROM model_output WHERE model = %s AND subject_id = %s AND as_of <= %s "
                                "LIMIT 1", (MODEL, week.isoformat(), day)).fetchone()
        if done is not None:
            return {"frozen": True, "week": week.isoformat(), "as_of": day.isoformat(), "outputs": {}, "changed": []}
    return run_outputs(MODEL, VERSION, day, compute, trigger=trigger, mark=mark)


def _run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    r = run(trigger=trigger)
    return {"week": r.get("week") or next(iter(r["outputs"]), None), "frozen": r.get("frozen", False)}


def _install_triggers() -> None:
    for model in ("price", "mark_gap"):
        triggers.model_depends(MODEL, "instrument", f"@model:{model}", triggers.everything)
    for model in ("status", "cushion"):
        triggers.model_depends(MODEL, "site", f"@model:{model}", triggers.everything)
    triggers.register_runner(MODEL, _run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="risk_list_weekly")
@app.task(name="models.risk_list", queue="models", queueing_lock="models.risk_list", lock="models.risk_list")
def risk_list_task(timestamp: int | None = None) -> dict[str, Any]:
    return _run_keys(["*"], "schedule")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--force", action="store_true", help="recompute a week that already has a list (backtest)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true")


@cli.action("model", MODEL, help="Weekly risk ranking and note (spec 10)", configure=_args)
def risk_list_cli(args: argparse.Namespace) -> int:
    r = run(args.as_of, force=args.force, trigger="manual", mark=not args.no_mark)
    if r.get("frozen"):
        print(f"risk_list: week {r['week']} already published (frozen); --force to recompute")
        return 0
    week, doc = next(iter(r["outputs"].items()))
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0
    print(f"risk_list {VERSION} week {week} as of {r['as_of']} (run {r['run_id']}): {len(doc['rows'])} rows")
    for row in doc["rows"]:
        print(f"  {row['rank']:2} {row['instrumentId']:26} odds {row['odds']['value']:.2f} px {row['price']['value']:7.2f} "
              f"gap {row['gap']} {row['move']['kind']:4} | {' / '.join(e['text'][:60] for e in row['why'][:2])}")
    for p in doc["note"]["paragraphs"]:
        print(f"  NOTE {p['lead']}: {p['text'][:200]}")
    return 0
