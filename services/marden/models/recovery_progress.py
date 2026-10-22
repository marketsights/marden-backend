"""How much of a site's works is finished when no source states it (recovery v0.2; recovery_shell): an
estimate, tag M, from what is on file point in time (marden.models.pit), with every estimator shown.

A stated share wins (`pct_complete`, else delivered MW over capacity; recovery_shell.SiteFacts.completion).
Without one, the estimate is:

1. **Capex spent vs budget**, when a filed or reported `cost_to_complete_usd` and `total_project_cost_usd`
   are on file: 1 - cost to complete / budget. Money in the ground is the best evidence, so it stands alone.
2. Otherwise the mean of the estimators available of:
   - **Schedule**: months elapsed since construction started over the months from start to the planned
     completion of the whole campus. Start: the earliest building `construction_start` on the linked
     campuses, else a start month the site's records state ('Construction began ~Sep/Q4 2025' in
     `risk_construction`), else the financial close (the issue date of the site's `debt_facility` comp).
     Planned completion: the last date of the campus `capacity_plan`, else the site's cushion P50 rent
     start (model 'cushion'). Spend runs roughly with time, so this is the share of the budget spent.
   - **Imagery**: the roofed share of the planned buildings (campus `imagery_note.roof_ha` over the planned
     footprint from `sqft_total`, else the share of the campus's planned buildings whose Sentinel-2
     `imagery_stage` is 'roofed', half for 'under_construction') x SHELL_SHARE: a roofed shell is the
     structure and envelope, about a third of a data center's cost; the electrical and mechanical fit-out
     is not visible from orbit.
3. **Energization** bounds it: with no MW delivered, the works are not finished (at most UNENERGIZED_CAP).

Parameters are judgment, uncalibrated ('model v0'), stated in docs/methods/recovery-v0.1.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit, runs
from marden.models.sourced import num

SHELL_SHARE = 1 / 3  # structure and envelope as a share of a data center's cost (judgment, model v0)
UNENERGIZED_CAP = 0.9  # nothing delivered: the fit-out and commissioning are not done
SQFT_PER_HA = 107639.1
SOURCED = ("F", "R")
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), 1)}
_START = re.compile(r"(?:began|begun|started|start(?:ed)? of construction|groundbreaking|broke ground)\D{0,12}?"
                    r"(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?[\s/]*)?(?:Q([1-4])\s*)?(20\d\d)", re.I)
CAMPUS_FIELDS = ("capacity_plan", "imagery_note", "sqft_total", "building_count")
BUILDING_FIELDS = ("construction_start", "imagery_stage")


@dataclass
class Estimate:
    share: float | None
    basis: str  # 'stated' | 'estimated' | why it is n/d
    estimators: list[dict[str, Any]] = field(default_factory=list)


def _val(row: Row | None) -> Any:
    return None if row is None else row["value"]


def _src(row: Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {k: v for k, v in (("subject", f"{row['subject_type']}:{row['subject_id']}"), ("field", row["field"]),
                              ("tag", str(row["tag"]).strip()), ("sourceName", row.get("source_name")),
                              ("sourceUrl", row.get("source_url"))) if v}


def _months(a: date, b: date) -> float:
    return (b - a).days / 30.4375


def _by(rows: list[Row]) -> dict[tuple[str, str], Row]:
    out: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        out.setdefault((r["subject_id"], r["field"]), []).append(r)
    return {k: best for k, rs in out.items() if (best := pit.current_of(rs)) is not None}


def _parse_start(text: str) -> date | None:
    m = _START.search(text)
    if not m:
        return None
    year = int(m[3])
    month = MONTHS[m[1][:3].lower()] if m[1] else (3 * int(m[2]) - 2 if m[2] else 1)
    return date(year, month, 1)


def _iso(v: Any) -> date | None:
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", v[:10].rstrip("-")):
        y, m = int(v[:4]), int(v[5:7])
        return date(y, m, int(v[8:10]) if len(v) >= 10 else 1)
    return None


def campuses(conn: Conn, slug: str) -> list[str]:
    return [r["campus_id"] for r in conn.execute("SELECT campus_id FROM site_link WHERE site_slug = %s ORDER BY 1",
                                                 (slug,)).fetchall()]


def _schedule(conn: Conn, day: date, slug: str, site_rows: dict[str, list[Row]], camp: dict[tuple[str, str], Row],
              bld: dict[tuple[str, str], Row], ids: list[str]) -> dict[str, Any] | None:
    starts: list[tuple[date, str, dict[str, Any]]] = []
    for (bid, fld), row in bld.items():
        if fld == "construction_start" and (d := _iso(_val(row))) is not None:
            starts.append((d, f"building {bid} construction start", _src(row)))
    if not starts:
        for row in site_rows.get("risk_construction", []):
            v = _val(row)
            text = v.get("detail") if isinstance(v, dict) else v
            if isinstance(text, str) and (d := _parse_start(text)) is not None:
                starts.append((d, "construction start stated in the site's records", _src(row)))
                break
    if not starts:
        fac = pit.current_of(site_rows.get("debt_facility", []))
        fid = _val(fac).get("fid") if fac is not None and isinstance(_val(fac), dict) else None
        if fid:
            comp = pit.current_of(pit.fact_rows(conn, day, "series", [f"issue_comp:{fid}"], ["issue_date"]))
            if comp is not None and (d := _iso(_val(comp))) is not None:
                starts.append((d, "financial close (issue date of the site's debt)", _src(comp)))
    if not starts:
        return None
    start, start_how, start_src = min(starts, key=lambda s: s[0])
    end, end_how, end_src = None, "", {}
    for cid in ids:
        plan = camp.get((cid, "capacity_plan"))
        dates = [d for p in (_val(plan) or []) if isinstance(p, dict) and (d := _iso(p.get("date")))]
        if dates and (end is None or max(dates) > end):
            end, end_how, end_src = max(dates), "planned completion of the whole campus (capacity plan)", _src(plan)
    if end is None:
        cush = runs.latest_output(conn, "cushion", "site", slug, day)
        p50 = ((cush["output"].get("projection") or {}).get("p50") if cush and isinstance(cush["output"], dict)
               else None)
        if (d := _iso(p50)) is not None:
            end, end_how, end_src = d, "projected rent start (cushion model P50)", {"model": "cushion",
                                                                                   "asOf": str(cush["as_of"])}
    if end is None or end <= start:
        return None
    share = max(0.0, min(1.0, _months(start, day) / _months(start, end)))
    return {"key": "schedule", "share": round(share, 4), "label": "months elapsed vs the construction schedule",
            "detail": f"{_months(start, day):.0f} of {_months(start, end):.0f} months from {start:%b %Y} "
                      f"({start_how}) to {end:%b %Y} ({end_how})",
            "sources": [s for s in (start_src, end_src) if s]}


def _imagery(camp: dict[tuple[str, str], Row], bld: dict[tuple[str, str], Row], ids: list[str],
             n_buildings: dict[str, int]) -> dict[str, Any] | None:
    roof = footprint = 0.0
    srcs: list[dict[str, Any]] = []
    for cid in ids:
        note, sqft = camp.get((cid, "imagery_note")), camp.get((cid, "sqft_total"))
        r = num((_val(note) or {}).get("roof_ha")) if isinstance(_val(note), dict) else None
        f = num(_val(sqft))
        if r is not None and f:
            roof, footprint = roof + r, footprint + f / SQFT_PER_HA
            srcs += [_src(note), _src(sqft)]
    if footprint > 0:
        roofed = min(1.0, roof / footprint)
        return {"key": "imagery", "share": round(roofed * SHELL_SHARE, 4), "label": "roofed buildings in imagery",
                "detail": f"{roof:.1f} ha roofed of a {footprint:.1f} ha planned footprint ({roofed:.0%}) x "
                          f"{SHELL_SHARE:.0%} (the shell's share of the cost; fit-out is not visible from orbit)",
                "sources": srcs}
    planned = sum(n_buildings.get(cid, 0) for cid in ids)
    stages = [(_val(row) or {}).get("stage") for (bid, fld), row in bld.items() if fld == "imagery_stage"
              and isinstance(_val(row), dict)]
    if planned and stages:
        done = sum(1.0 if s == "roofed" else 0.5 if s == "under_construction" else 0.0 for s in stages)
        roofed = min(1.0, done / planned)
        return {"key": "imagery", "share": round(roofed * SHELL_SHARE, 4), "label": "roofed buildings in imagery",
                "detail": f"{sum(s == 'roofed' for s in stages)} of {planned} planned buildings roofed, "
                          f"{sum(s == 'under_construction' for s in stages)} under construction (half) x "
                          f"{SHELL_SHARE:.0%} (the shell's share of the cost)",
                "sources": [_src(row) for (_, fld), row in bld.items() if fld == "imagery_stage"]}
    return None


def estimate(conn: Conn, day: date, slug: str, site_rows: dict[str, list[Row]], stated: tuple[float | None, str],
             delivered: float | None) -> Estimate:
    """The share of the works finished: stated, else estimated from capex, schedule and imagery."""
    if stated[0] is not None:
        return Estimate(stated[0], "stated", [{"key": "stated", "share": round(stated[0], 4), "label": stated[1]}])
    found: list[dict[str, Any]] = []
    cost = pit.current_of([r for r in site_rows.get("total_project_cost_usd", []) if str(r["tag"]).strip() in SOURCED])
    ctc = pit.current_of([r for r in site_rows.get("cost_to_complete_usd", []) if str(r["tag"]).strip() in SOURCED])
    if cost is not None and ctc is not None and num(_val(cost)) and num(_val(ctc)) is not None:
        spent = max(0.0, min(1.0, 1.0 - float(num(_val(ctc)) or 0) / float(num(_val(cost)) or 1)))
        found.append({"key": "capex", "share": round(spent, 4), "label": "capex spent vs budget",
                      "detail": f"1 - ${num(_val(ctc)) / 1e9:,.2f}B to complete / ${num(_val(cost)) / 1e9:,.2f}B "
                                "budget", "sources": [_src(ctc), _src(cost)]})
    ids = campuses(conn, slug)
    camp = _by(pit.fact_rows(conn, day, "campus", ids, list(CAMPUS_FIELDS))) if ids else {}
    bids = [r["id"] for r in conn.execute("SELECT id FROM building WHERE campus_id = ANY(%s) ORDER BY 1",
                                          (ids,)).fetchall()] if ids else []
    bld = _by(pit.fact_rows(conn, day, "building", bids, list(BUILDING_FIELDS))) if bids else {}
    n_buildings = {cid: int(num(_val(camp.get((cid, "building_count")))) or 0) for cid in ids}
    for cid in ids:
        n_buildings[cid] = max(n_buildings[cid], sum(1 for b in bids if b.startswith(f"{cid}-")))
    if not found:
        for got in (_schedule(conn, day, slug, site_rows, camp, bld, ids), _imagery(camp, bld, ids, n_buildings)):
            if got is not None:
                found.append(got)
    if not found:
        return Estimate(None, "no stated completion, capex, schedule or imagery on file", [])
    share = sum(f["share"] for f in found) / len(found)
    if not delivered and share > UNENERGIZED_CAP:
        found.append({"key": "energization", "share": UNENERGIZED_CAP, "label": "nothing energized yet",
                      "detail": f"no MW delivered: at most {UNENERGIZED_CAP:.0%} finished"})
        share = UNENERGIZED_CAP
    return Estimate(share, "estimated", found)
