"""Shape one site's cushion output exactly as handoff/api/types.ts: `projection` (CushionProjection),
`milestones` (Milestone[] with projected p10/p50/p90), `driver`, plus `missing`, `notApplicable` and
`basis` for the method page. Also the read-side helpers builders use: the stored output for a snapshot day
and SiteSchedule.cushionHistory.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np

from marden.core.db import Conn
from marden.core.facts import sourced
from marden.models import cushion_dates as cd
from marden.models import late_cost, runs
from marden.models.cushion_history import MIN_POOLED, SlipHistory
from marden.models.cushion_inputs import SiteInputs
from marden.models.cushion_milestones import Milestone, milestone_doc
from marden.models.cushion_sim import SimResult, months_of, odds_after

MODEL = "cushion"
VERSION = "cushion v0.1"
ODDS_VERSION = "odds v0"
SOURCE_NAME = "Marden cushion model"
LABEL = "model v0"


def m_value(value: Any, as_of: date, method: str = VERSION) -> dict[str, Any]:
    """Sourced<T> for a Marden model value."""
    return {"value": value, "tag": "M", "sourceName": SOURCE_NAME, "asOf": as_of.isoformat(), "methodVersion": method}


def outside_doc(site: SiteInputs) -> dict[str, Any]:
    o = site.outside
    if o is None:
        return m_value(None, site.as_of)  # n/d: no lease outside date in any source
    doc: dict[str, Any] = {"value": o.value, "tag": o.row["tag"], "sourceName": o.row["source_name"],
                           "asOf": o.row["valid_from"].isoformat()}
    if o.row.get("source_url"):
        doc["sourceUrl"] = o.row["source_url"]
    if o.row["tag"] == "M" and o.row.get("method_version"):
        doc["methodVersion"] = o.row["method_version"]
    return doc


def _cushion_of(output: dict[str, Any] | None) -> int | None:
    if not output:
        return None
    value = ((output.get("projection") or {}).get("cushionMonths") or {}).get("value")
    return value if isinstance(value, int) else None


def _p50_of(output: dict[str, Any] | None) -> str | None:
    return ((output or {}).get("projection") or {}).get("p50")


def _usd(v: float) -> str:
    return f"${v / 1e9:,.2f}B" if v >= 1e9 else f"${v / 1e6:,.1f}M"


def _src(row: Any) -> dict[str, Any] | None:
    return sourced(row) if row is not None else None


def late_cost_doc(site: SiteInputs, debt: dict[str, Any] | None, rent_done: bool) -> dict[str, Any]:
    """SiteSchedule projection.lateCost: the month-late cost in parts with formulas, who bears each part as a
    source states it, the instruments left out and, when there is no figure, why (marden.models.late_cost)."""
    debt = debt or {"lines": [], "excluded": [], "bearers": [], "sofr": None, "instruments": 0}
    rent_monthly = site.rent.monthly_usd if site.rent is not None else None
    sums = late_cost.assemble(debt, rent_monthly=rent_monthly, rent_done=rent_done)

    def bearer(b: dict[str, Any]) -> dict[str, Any]:
        src = {k: v for k, v in (_src(b["row"]) or {}).items() if k != "value"}  # the source, not the term's object
        return {"key": b["key"], "text": b["text"], **src}

    parts: list[dict[str, Any]] = []
    if sums["interest"]:
        lines = debt["lines"]
        parts.append({
            "kind": "interest", "label": "Construction interest", "monthlyUsd": round(sums["interest"]),
            "formula": " + ".join(f"{_usd(ln.amount)} x {ln.rate_pct:g}% / 12" for ln in lines),
            "lines": [{"name": ln.name, "instrumentId": ln.instrument, "amountUsd": ln.amount,
                       "amountBasis": ln.amount_basis, "ratePct": ln.rate_pct, "rateBasis": ln.rate_basis,
                       "monthlyUsd": round(ln.monthly), "sources": [d for r in ln.rows if (d := _src(r))]}
                      for ln in lines],
            "bearers": [bearer(b) for b in debt["bearers"] if b["key"] in ("Completion support", "Reserves")],
            "paidBy": "the borrower (the issuer that owes the debt)",
        })
    if sums["rent"]:
        assert site.rent is not None
        parts.append({
            "kind": "rent", "label": "Rent not yet collected", "monthlyUsd": round(sums["rent"]),
            "formula": site.rent.basis, "source": _src(site.rent.row),
            "condition": "for each month rent start slips past its due date",
            "bearers": [bearer(b) for b in debt["bearers"] if b["key"] not in ("Completion support", "Reserves")],
            "paidBy": "the landlord (rent it does not receive)",
        })
    doc: dict[str, Any] = {"parts": parts, "excluded": debt["excluded"], "methodVersion": late_cost.VERSION,
                           "why": sums["why"]}
    if debt.get("sofr") is not None:
        doc["sofr"] = _src(debt["sofr"])
    return doc


def projection_doc(site: SiteInputs, sim: SimResult | None, prior: dict[str, Any] | None,
                   late: dict[str, Any] | None = None, rent_done: bool = False) -> dict[str, Any]:
    as_of = site.as_of
    months = months_of(sim.rent) if sim is not None else {"p10": None, "p50": None, "p90": None}
    doc: dict[str, Any] = {**months, "outsideDate": outside_doc(site)}
    o = site.outside
    if o is not None and o.assumed:
        doc["outsideDateAssumed"] = o.assumed
    if o is not None and o.window is not None:  # a year only: the range tested, and the midpoint used
        doc["outsideDateRange"] = {"from": cd.month_start(o.window[0] + 1).isoformat(),
                                   "to": cd.month_end(o.window[1]).isoformat(), "central": o.assumed}
    cushion: int | None = None
    if o is not None:
        if months["p50"] is not None:
            year, month = (int(x) for x in months["p50"].split("-"))
            p50 = cd.month_index(year, month)
            cushion = o.month - p50
            if o.window is not None:
                doc["cushionMonthsRange"] = {"worst": o.window[0] - p50, "best": o.window[1] - p50}
        doc["cushionMonths"] = m_value(cushion, as_of)
    else:
        doc["cushionMonths"] = None  # no lease date: shown as '—'
    before = _cushion_of(prior)
    doc["deltaWeek"] = m_value(cushion - before if cushion is not None and before is not None else None, as_of)
    if o is not None and sim is not None:
        doc["oddsOfMissing"] = m_value(round(odds_after(sim.rent, o.month), 3), as_of, ODDS_VERSION)
        if o.window is not None:  # best case Dec 31, worst case Jan 1; the midpoint above is what models read
            doc["oddsOfMissingRange"] = {"best": round(odds_after(sim.rent, o.window[1]), 3),
                                         "worst": round(odds_after(sim.rent, o.window[0]), 3),
                                         "bestDate": cd.month_end(o.window[1]).isoformat(),
                                         "worstDate": cd.month_start(o.window[0] + 1).isoformat()}
    lc = late_cost_doc(site, late, rent_done)
    total = sum(p["monthlyUsd"] for p in lc["parts"]) if lc["parts"] else None
    if total is not None:
        doc["costPerMonthLate"] = m_value(total, as_of, late_cost.VERSION)
    doc["lateCost"] = lc
    return doc


def p50_change(current: str | None, prior: dict[str, Any] | None) -> int | None:
    before = _p50_of(prior)
    if current is None or before is None:
        return None
    y1, m1 = (int(x) for x in current.split("-"))
    y0, m0 = (int(x) for x in before.split("-"))
    return cd.month_index(y1, m1) - cd.month_index(y0, m0)


def milestones_doc(milestones: list[Milestone], sim: SimResult | None) -> list[dict[str, Any]]:
    out = []
    for ms in milestones:
        if not ms.applicable:
            continue
        projected = months_of(sim.finish[ms.key]) if sim is not None and ms.key in sim.simulated else None
        out.append(milestone_doc(ms, projected))
    return out


def missing_list(site: SiteInputs, milestones: list[Milestone], sim: SimResult | None,
                 history: SlipHistory) -> list[str]:
    missing: list[str] = []
    if site.outside is None:
        missing.append("outside_date: no lease outside date in any source (cushion and odds not computed)")
    if site.rent is None:
        missing.append("rent: no rent or contract value and term disclosed (the month-late cost has no rent part)")
    for ms in milestones:
        if not ms.applicable or ms.is_done:
            continue
        if ms.planned is None and ms.key != "rent_start":
            missing.append(f"{ms.key}: no dated plan in any source (not simulated)")
    rent = next(m for m in milestones if m.key == "rent_start")
    if rent.note and rent.note.startswith("no rent"):
        missing.append(f"rent_start: {rent.note}")
    if rent.is_done and rent.done is None:
        missing.append("rent_start: done, but no source dates it (no projection)")
    dated = [m.key for m in milestones if m.applicable and not m.is_done and m.planned is not None]
    pool, _ = history.pool("build", site.slug)
    if sim is None and dated and len(pool) < MIN_POOLED:
        missing.append(f"slip_history: {len(pool)} plan revisions public by {site.as_of.isoformat()} (at least "
                       f"{MIN_POOLED} needed to simulate {', '.join(dated)}; p10/p50/p90 n/d)")
    elif sim is None:
        missing.append("projection: no dated milestone on the critical path to rent start (p10/p50/p90 n/d)")
    elif sim.unsimulated:
        missing.extend(f"{k}: dated, but no slip history to simulate it" for k in sim.unsimulated if k in dated)
    if not any(r.source == "eia_860m" for r in history.records):
        missing.append(f"slip_history: no EIA-860M planned-vs-actual pairs ({history.eia_reports} monthly report(s) "
                       "public by then; a pair needs two); power milestones use the pooled archive history")
    return missing


def _month_label(m: int) -> str:
    return f"{cd.month_start(m):%b} '{m // 12 % 100:02d}"


def lease_view(site: SiteInputs, sim: SimResult | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """What the projection means against the outside date (site_tab:lease 'outcomes' and 'today')."""
    o = site.outside
    if o is None or sim is None:
        return [], None
    after = odds_after(sim.rent, o.month)
    p50 = int(np.floor(np.percentile(sim.rent, 50)))
    gap = o.month - p50

    def months(n: int) -> str:
        return f"{abs(n)} month{'s' if abs(n) != 1 else ''} {'before' if n >= 0 else 'after'}"

    if o.window is not None:  # a year only: tested across the year, the midpoint Jul 1 central
        worst, best = odds_after(sim.rent, o.window[0]), odds_after(sim.rent, o.window[1])
        span = f"the lease gives {o.value} only; we test Jan\u2013Dec {o.value}, Jul 1 central"
        outcomes = [
            {"label": "Finished before the outside date",
             "text": f"{round(100 * (1 - after))}% of simulated schedules start rent by Jul 1, {o.value} ({span}): "
                     f"{round(100 * (1 - worst))}% by Jan 1, {round(100 * (1 - best))}% by Dec 31",
             "value": m_value(f"{round(100 * (1 - after))}%", site.as_of, ODDS_VERSION)},
            {"label": "Finished after it",
             "text": f"{round(100 * best)}\u2013{round(100 * worst)}% start rent after it (Dec 31 to Jan 1; "
                     f"{round(100 * after)}% after Jul 1); the lease's termination and force majeure terms apply",
             "value": m_value(f"{round(100 * after)}%", site.as_of, ODDS_VERSION)},
        ]
        position = (f"P50 rent start {_month_label(p50)}: {months(gap)} Jul 1, {o.value} ({span}; "
                    f"{months(o.window[1] - p50)} Dec 31, {months(o.window[0] - p50)} Jan 1)")
        return outcomes, m_value(position, site.as_of)
    when = f"{date.fromisoformat(o.assumed):%b} {int(o.assumed[8:])}, {o.assumed[:4]}, assumed" if o.assumed \
        else _month_label(o.month)
    outcomes = [
        {"label": "Finished before the outside date",
         "text": f"{round(100 * (1 - after))}% of simulated schedules start rent by the outside date ({when})",
         "value": m_value(f"{round(100 * (1 - after))}%", site.as_of, ODDS_VERSION)},
        {"label": "Finished after it",
         "text": f"{round(100 * after)}% start rent after it; the lease's termination and force majeure terms apply",
         "value": m_value(f"{round(100 * after)}%", site.as_of, ODDS_VERSION)},
    ]
    position = f"P50 rent start {_month_label(p50)}: {months(gap)} the outside date ({when})"
    return outcomes, m_value(position, site.as_of)


def stored_output(conn: Conn, slug: str, as_of: date) -> dict[str, Any] | None:
    """The cushion output a snapshot of `as_of` shows (the newest run on or before that day)."""
    row = runs.latest_output(conn, MODEL, "site", slug, as_of)
    return row["output"] if row else None


def cushion_history(conn: Conn, slug: str, as_of: date) -> list[dict[str, Any]]:
    """SiteSchedule.cushionHistory: weekly (the last run of each week, Monday-dated), Jan 1 of the as-of
    year to as_of, weeks with a cushion only; `event` names what moved it when it changed."""
    start = date(as_of.year, 1, 1)
    rows = runs.outputs_between(conn, MODEL, "site", slug, start, as_of)
    weekly: dict[date, dict[str, Any]] = {}
    for r in rows:
        monday = r["as_of"] - timedelta(days=r["as_of"].weekday())
        weekly[max(monday, start)] = r["output"]
    out: list[dict[str, Any]] = []
    previous: int | None = None
    for day in sorted(weekly):
        output = weekly[day]
        months = _cushion_of(output)
        if months is None:
            continue
        point: dict[str, Any] = {"date": day.isoformat(), "months": months}
        if previous is not None and months != previous:
            point["event"] = _what_moved(output)
        out.append(point)
        previous = months
    return out


def _what_moved(output: dict[str, Any]) -> str:
    delays = (output.get("basis") or {}).get("delayMonths") or {}
    key = max(delays, key=delays.get) if delays else None
    for ms in output.get("milestones") or []:
        if ms.get("key") == key and ms.get("latestEvidence"):
            return f"{ms['label']}: {ms['latestEvidence']['text']}"
    return output.get("driver") or "projection changed"
