"""Pipeline (read model `pipeline`, key 'all'; GET /api/pipeline, PipelinePage in handoff/api/types.ts): AI
data-center projects before and around the start of construction, one row per project whether or not Marden
tracks it as a campus.

Each row groups the signals of one project (pipeline_group: developer + county + coordinates or parcel, or an
explicit link) from tracked campuses, interconnection-queue load requests and announced projects
(pipeline_signals). Its stage is the furthest of Announced -> Land acquired -> Rezoning / permits filed -> Power
requested -> Permits approved -> Construction started that a dated signal shows, each step listing its evidence
with tag and source. The next expected step is the one a source states (an announced project's `next_step`),
else the step that usually follows the furthest one.

Which tracked campuses appear: those Marden holds as Planned or Under construction, and an operating campus only
when an announcement or queue request joins it (an expansion; its row then shows only those signals). Location is
the campus point, the project's stated point, else the county's interior point (`locationApprox`). Planned MW is
the campus's facility MW, else the announced figure, else the largest single queue request (never a sum). "New in
the pipeline" = first signal within the last 30 days of the build date.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from marden.core.db import Conn
from marden.readmodels.registry import builder
from marden.readmodels.views.pipeline_group import (
    NEXT_STEP,
    STAGE_LABELS,
    STAGES,
    Candidate,
    Signal,
    county_key,
    furthest,
    group,
)
from marden.readmodels.views.pipeline_signals import (
    BPA_STATES,
    campus_candidate,
    project_candidate,
    project_ids,
    queue_candidate,
    queue_ids,
    us_campus_ids,
)
from marden.readmodels.views.sites_common import day

Doc = dict[str, Any]
NEW_DAYS = 30
PIPELINE_STATUSES = ("Planned", "Under construction")
ORIGIN_RANK = {"tracked": 0, "announced": 1, "queue": 2}


def counties(conn: Conn, states: set[str]) -> dict[str, list[tuple[str, str, float, float]]]:
    """state -> [(county key, county name, lat, lon)] (the county's interior point)."""
    out: dict[str, list[tuple[str, str, float, float]]] = {}
    if not states:
        return out
    rows = conn.execute(
        """SELECT stusps, name, ST_Y(ST_PointOnSurface(geom)) AS lat, ST_X(ST_PointOnSurface(geom)) AS lon
             FROM us_county WHERE stusps = ANY(%s)""",
        (sorted(states),),
    ).fetchall()
    for r in rows:
        out.setdefault(r["stusps"], []).append((county_key(r["name"]), r["name"], r["lat"], r["lon"]))
    return out


def canonical_county(table: dict[str, list[tuple[str, str, float, float]]], state: str | None,
                     county: str | None) -> tuple[str, float, float] | None:
    """The census county a stated county matches ('St; Lawrence' -> St. Lawrence, 'NIAGARA FALLS' -> Niagara)."""
    key = county_key(county)
    if not key:
        return None
    states = [state.upper()] if state else [s for s in BPA_STATES if s in table]
    options = [o for s in states for o in table.get(s, [])]
    if not state and len([o for o in options if o[0] == key]) != 1:
        return None  # a stateless county name must be unique in the BPA area
    for k, name, lat, lon in options:
        if k == key:
            return name, lat, lon
    if not state:
        return None
    for k, name, lat, lon in sorted(options, key=lambda o: -len(o[0])):
        if k and (key.startswith(k + " ") or key.replace(" ", "") == k.replace(" ", "")):
            return name, lat, lon
    return None


def _state_of(table: dict[str, list[tuple[str, str, float, float]]], place: tuple[str, float, float] | None) -> str | None:
    for state, options in table.items():
        if any((o[1], o[2], o[3]) == place for o in options):
            return state
    return None


def candidates(conn: Conn) -> list[Candidate]:
    out: list[Candidate] = []
    for cid in us_campus_ids(conn):
        if (c := campus_candidate(conn, cid)) is not None:
            out.append(c)
    for qid in queue_ids(conn):
        if (c := queue_candidate(conn, qid)) is not None and c.signals:
            out.append(c)
    for pid in project_ids(conn):
        if (c := project_candidate(conn, pid)) is not None and c.signals:
            out.append(c)
    return out


def _first(members: list[Candidate], attr: str) -> Any:
    for c in sorted(members, key=lambda m: ORIGIN_RANK[m.origin]):
        value = getattr(c, attr)
        if value:
            return value
    return None


def _nd(tag: str, why: str) -> Doc:
    return {"value": None, "tag": tag, "asOf": day().isoformat(), "reason": why}


def _planned_mw(members: list[Candidate]) -> Doc:
    for origin in ("tracked", "announced"):
        for c in members:
            if c.origin == origin and c.mw:
                return c.mw
    queued = [c.mw for c in members if c.origin == "queue" and c.mw]
    if queued:
        best = dict(max(queued, key=lambda m: m["value"]))
        if len(queued) > 1:
            best["note"] = f"Largest of {len(queued)} queue requests"
        return best
    return _nd("R", "No source states a planned MW for this project")


def row_doc(members: list[Candidate], reasons: list[str], place: tuple[str, float, float] | None) -> Doc | None:
    tracked = next((c for c in members if c.origin == "tracked"), None)
    others = [c for c in members if c.origin != "tracked"]
    status = tracked.extra.get("status") if tracked else None
    if tracked and status not in PIPELINE_STATUSES and not others:
        return None
    use = members if not tracked or status in PIPELINE_STATUSES else others
    signals = sorted({(s.stage, s.day, s.text, s.source_url): s for c in use for s in c.signals}.values(),
                     key=lambda s: (s.day, STAGES.index(s.stage)), reverse=True)
    if not signals:
        return None
    stage = furthest(signals)
    assert stage is not None
    lead = tracked or sorted(members, key=lambda m: (ORIGIN_RANK[m.origin], m.key))[0]
    point = tracked.point if tracked and tracked.point else _first(others, "point")
    approx = point is None and place is not None
    if approx and place:
        point = (place[1], place[2])
    stated = next((c.next_step for c in members if c.next_step), None)
    next_step = stated or {"text": NEXT_STEP[stage], "basis": "usual next step"}
    if stage == "construction_started" and not stated:
        next_step = {"text": NEXT_STEP[stage], "basis": "usual next step"}
    # Marden's own status reads (tag M, dated when recorded) date nothing when a filed or reported signal exists.
    last: Signal = next((s for s in signals if s.tag != "M"), signals[0])
    first_day = min(s.day for s in signals)
    county = place[0] if place else lead.county
    queue_ids = sorted(c.key.split(":", 1)[1] for c in members if c.origin == "queue")
    expansion = bool(tracked and status not in PIPELINE_STATUSES)
    return {
        "id": lead.key,
        "name": lead.name if not expansion else f"{lead.name} (expansion)",
        "developer": _first(members, "developer") or _nd("R", "No source names the developer"),
        "tenant": _first(members, "tenant") or _nd("R", "No source names a tenant or end user"),
        "county": county,
        "state": lead.state or _first(members, "state"),
        "lat": point[0] if point else None,
        "lon": point[1] if point else None,
        "locationApprox": approx,
        "plannedMw": _planned_mw(members),
        "stage": stage,
        "stageLabel": STAGE_LABELS[stage],
        "steps": [
            {"stage": s, "label": STAGE_LABELS[s], "signals": [x.doc() for x in signals if x.stage == s]}
            for s in STAGES
        ],
        "nextStep": next_step,
        "financing": _first(members, "financing"),
        "lastSignal": last.doc(),
        "daysSinceSignal": (day() - last.day).days,
        "firstSignal": first_day.isoformat(),
        "campus": {"id": tracked.campus_id, "name": tracked.name, "status": status} if tracked else None,
        "queueIds": queue_ids,
        "projectIds": sorted(c.key.split(":", 1)[1] for c in members if c.origin == "announced"),
        "origin": lead.origin,
        "expansion": expansion,
        "groupedBy": reasons,
    }


def build_page(conn: Conn) -> Doc:
    cands = candidates(conn)
    table = counties(conn, {c.state.upper() for c in cands if c.state} | set(BPA_STATES))
    places: dict[str, tuple[str, float, float] | None] = {}
    for c in cands:
        places[c.key] = canonical_county(table, c.state, c.county)
        if places[c.key] and not c.state:
            c.state = _state_of(table, places[c.key])
        if places[c.key]:
            c.county = places[c.key][0]  # type: ignore[index]
    rows: list[Doc] = []
    for members, reasons in group(cands):
        lead = next((c for c in members if c.origin == "tracked"), members[0])
        place = places.get(lead.key) or next((places[c.key] for c in members if places.get(c.key)), None)
        if (doc := row_doc(members, reasons, place)) is not None:
            rows.append(doc)
    rows.sort(key=lambda r: (r["lastSignal"]["date"], r["name"]), reverse=True)
    since = (day() - timedelta(days=NEW_DAYS)).isoformat()
    stages = []
    for s in STAGES:
        hit = [r for r in rows if r["stage"] == s]
        mw = sum(r["plannedMw"]["value"] for r in hit if isinstance(r["plannedMw"].get("value"), int | float))
        stages.append({"stage": s, "label": STAGE_LABELS[s], "count": len(hit), "mw": round(mw)})
    return {
        "asOf": day().isoformat(),
        "stages": stages,
        "rows": rows,
        "newSince": since,
        "newIds": [r["id"] for r in rows if r["firstSignal"] >= since],
        "notes": [
            "Signals from the same project are grouped by developer + county + coordinates or parcel; a signal that "
            "matches a tracked campus joins its row.",
            "Queue rows are utility load-interconnection requests named or classed as data centers (BPA, NYISO).",
            "Planned MW is never summed across queue requests; a dash means no source states it.",
        ],
    }


@builder("pipeline", keys=("all",))
def build_pipeline(conn: Conn, key: str) -> Doc | None:
    return build_page(conn)

