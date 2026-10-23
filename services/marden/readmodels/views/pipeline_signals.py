"""Pipeline signals (read model `pipeline`, views/pipeline.py): the dated evidence of each project's stage, read
from three kinds of record as Marden knew them on the build date.

- Tracked campuses (US `campus` rows): status 'Under construction' and dated building construction starts
  (Construction started); the campus's and its linked sites' `large_load_request` (Power requested); the linked
  sites' `land` (Land acquired); Texas TDLR `state_registration` on the campus and its buildings and the air
  permits of the campus's sites and serving plants (Rezoning / permits filed, or Permits approved once issued).
- Interconnection-queue load requests (`queue_project`): BPA "Line and Load Interconnection" entries named as data
  centers and NYISO load entries whose end use is a data center (DAT, DAT-AI; crypto mining DAT-CM left out), not
  withdrawn or in service. The queue date is a Power requested signal, a signed interconnection agreement another;
  a queue status of under construction is Construction started.
- Announced projects (`pipeline_project` facts, seed/pipeline_2026_10): a `project` profile, `planned_mw`, and one
  fact per dated stage signal (`announced`, `land_acquired`, `permits_filed`, `power_requested`,
  `permits_approved`, `construction_started`), plus `financing` and a stated `next_step`.

Signals dated after the build date (planned starts) are not evidence and are left out.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.core.licensing import hidden_source_names
from marden.readmodels.context import day_end, known_fact_rows, known_facts
from marden.readmodels.views.facilities_campus import ROW_FIELDS, row_doc
from marden.readmodels.views.facilities_common import campus_buildings, campus_row, is_us, serving_plants
from marden.readmodels.views.pipeline_group import STAGES, Candidate, Signal
from marden.readmodels.views.sites_common import day

QUEUE_FIELDS = ["name", "developer", "mw_requested", "queue_date", "status", "status_class", "ia_date", "end_use",
                "project_type", "state", "county", "poi", "transmission_owner"]
LIVE_QUEUE = ("active", "under_construction", "suspended")
BPA_STATES = ("WA", "OR", "ID", "MT")  # BPA entries without a state sit in its service area
BPA_DC_NAME = re.compile(r"\b(data|server|compute|computing|ai)\b|\bdc\b", re.I)
NYISO_DC_USE = re.compile(r"^DAT(-AI)?\b")
MIN_QUEUE_MW = 20
PROJECT_FIELDS = ("project", "planned_mw", "financing", "next_step", *STAGES)
PERMIT_DONE = ("active", "issued", "approved", "granted", "final")


def _date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _signal(stage: str, day_: date | None, text: str, row: Row, origin: str, quote: str | None = None,
            url: str | None = None) -> Signal | None:
    if day_ is None or day_ > day():
        return None
    return Signal(stage, day_, text, row["tag"], row["source_name"], url or row.get("source_url"), origin,
                  quote if quote is not None else row.get("quote"))


def _keep(out: list[Signal], sig: Signal | None) -> None:
    if sig is not None:
        out.append(sig)


# ---------------------------------------------------------------- tracked campuses
def us_campus_ids(conn: Conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM campus WHERE created_at < %s ORDER BY id", (day_end(),))]


def linked_sites(conn: Conn, campus_id: str) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT site_slug FROM site_link WHERE campus_id = %s AND status = 'approved'
             AND (approved_at IS NULL OR approved_at < %s) ORDER BY 1""",
        (campus_id, day_end()),
    ).fetchall()
    return [r["site_slug"] for r in rows]


def _load_text(value: Any) -> str:
    if not isinstance(value, dict):
        return f"Large-load request: {value}"
    mw = value.get("mw")
    who = value.get("utility") or value.get("iso")
    parts = [f"{mw:,.0f} MW" if isinstance(mw, int | float) else None, f"with {who}" if who else None]
    stage = value.get("stage")
    head = "Large-load request " + " ".join(p for p in parts if p)
    return f"{head.strip()} ({stage})" if stage else head.strip()


def _permit_signals(rows: list[Row], origin: str) -> list[Signal]:
    out: list[Signal] = []
    for row in rows:
        items = row["value"] if isinstance(row["value"], list) else [row["value"]]
        for p in items:
            if not isinstance(p, dict):
                continue
            label = " ".join(str(x) for x in (p.get("agency"), p.get("type"), "permit", p.get("number")) if x)
            url = p.get("url") if isinstance(p.get("url"), str) else None
            filed = _date(p.get("filed"))
            if filed is None:
                acts = [_date(a.get("date")) for a in p.get("actions") or [] if isinstance(a, dict)]
                acts = [a for a in acts if a is not None]
                filed = min(acts) if acts else None
            _keep(out, _signal("permits_filed", filed, f"{label} filed", row, origin, p.get("quote"), url))
            status = str(p.get("status") or "").lower()
            if status in PERMIT_DONE or p.get("issued"):
                issued = _date(p.get("issued")) or row["valid_from"]
                _keep(out, _signal("permits_approved", issued, f"{label} {status or 'issued'}", row, origin,
                                   p.get("quote"), url))
    return out


def _registration(row: Row, origin: str = "campus") -> Signal | None:
    v = row["value"]
    if not isinstance(v, dict):
        return None
    text = f"{v.get('agency') or 'State'} registration {v.get('number') or ''}: {v.get('project') or ''}".strip()
    if v.get("status"):
        text += f" ({v['status']})"
    return _signal("permits_filed", _date(v.get("date")) or row["valid_from"], re.sub(r"\s+", " ", text), row, origin)


def campus_candidate(conn: Conn, campus_id: str) -> Candidate | None:
    campus = campus_row(conn, campus_id)
    if campus is None:
        return None
    facts = known_facts(conn, "campus", campus_id, ROW_FIELDS)
    if not is_us(campus["state"], facts):
        return None
    doc = row_doc(conn, campus, facts)
    signals: list[Signal] = []
    status = (doc.get("status") or {}).get("value")
    own = known_facts(conn, "campus", campus_id, ["large_load_request", "state_registration", "status"])
    sites = linked_sites(conn, campus_id)
    starts: list[Signal] = []
    for b in campus_buildings(conn, campus_id):
        bf = known_facts(conn, "building", b["id"], ["construction_start", "state_registration"])
        if (row := bf.get("construction_start")) is not None:
            _keep(starts, _signal("construction_started", _date(row["value"]), f"{b['label']}: construction started",
                                  row, "campus"))
        if (row := bf.get("state_registration")) is not None:
            _keep(signals, _registration(row))
    if starts:
        signals.append(min(starts, key=lambda s: s.day))
    elif status == "Under construction" and (row := own.get("status")) is not None:
        _keep(signals, _signal("construction_started", row["valid_from"], "Status: under construction", row, "campus"))
    if status == "Planned" and (row := own.get("status")) is not None:
        _keep(signals, _signal("announced", row["valid_from"], "Status: planned", row, "campus"))
    if (row := own.get("state_registration")) is not None:
        _keep(signals, _registration(row))
    loads = known_fact_rows(conn, "campus", campus_id, ["large_load_request"])["large_load_request"]
    for slug in sites:
        sf = known_fact_rows(conn, "site", slug, ["large_load_request", "land", "air_permits"])
        loads += sf["large_load_request"]
        for row in sf["land"][:1]:
            text = row["value"] if isinstance(row["value"], str) else str(row["value"])
            _keep(signals, _signal("land_acquired", row["valid_from"], f"Land: {text}", row, "campus"))
        signals += _permit_signals(sf["air_permits"], "campus")
    seen_loads: set[str] = set()
    for row in loads:
        text = _load_text(row["value"])
        if text in seen_loads:
            continue
        seen_loads.add(text)
        _keep(signals, _signal("power_requested", row["valid_from"], text, row, "campus"))
        if isinstance(row["value"], dict) and row["value"].get("stage") == "approved":
            _keep(signals, _signal("permits_approved", row["valid_from"], f"{text}: approved", row, "campus"))
    for plant in serving_plants(conn, campus_id):
        pf = known_fact_rows(conn, "plant", plant["id"], ["air_permit", "air_permits"])
        signals += _permit_signals(pf["air_permit"] + pf["air_permits"], "campus")
    point = (campus["lat"], campus["lon"]) if campus.get("lat") is not None else None
    dev = doc.get("developer") if (doc.get("developer") or {}).get("value") else None
    tenant = doc.get("tenant") if (doc.get("tenant") or {}).get("value") else None
    mw = doc.get("facilityMw") if isinstance((doc.get("facilityMw") or {}).get("value"), int | float) else None
    return Candidate(
        key=f"campus:{campus_id}", origin="tracked", name=campus["name"], state=campus["state"], county=doc["county"],
        developer=dev, tenant=tenant, point=point, parcels=frozenset(campus.get("parcel_ids") or []), mw=mw,
        signals=_dedupe(signals), financing=_campus_financing(doc), aliases=tuple(doc.get("aliases") or ()),
        extra={"status": status, "siteSlugs": sites},
    )


def _campus_financing(doc: dict[str, Any]) -> dict[str, Any] | None:
    fin = doc.get("financed")
    if isinstance(fin, dict):
        debt = fin.get("debtB") or {}
        value = debt.get("value")
        amount = f"${value:,.2f}B of debt" if isinstance(value, int | float) else "Financed site"
        return {"text": f"{amount} on {fin.get('siteName')}", "tag": debt.get("tag", "F"),
                "sourceName": debt.get("sourceName") or "Marden", "sourceUrl": debt.get("sourceUrl"),
                "asOf": debt.get("asOf") or day().isoformat(), "siteSlug": fin.get("siteSlug"),
                "debtB": value if isinstance(value, int | float) else None}
    note = doc.get("financing")
    if isinstance(note, dict) and isinstance(note.get("value"), dict) and note["value"].get("kind") != "not_found":
        return {"text": note["value"]["note"], "tag": note["tag"], "sourceName": note.get("sourceName") or "Marden",
                "sourceUrl": note.get("sourceUrl"), "asOf": note["asOf"], "siteSlug": None, "debtB": None}
    return None


# ---------------------------------------------------------------- queue load requests
def queue_ids(conn: Conn) -> list[str]:
    """Queue entries that are data-center load requests (BPA load entries named as data centers, NYISO DAT loads)."""
    hidden = hidden_source_names()
    rows = conn.execute(
        """SELECT p.id, p.iso, p.name, t.value AS ptype, e.value AS end_use FROM queue_project p
             LEFT JOIN fact_current t ON t.subject_type = 'queue_project' AND t.subject_id = p.id
                  AND t.field = 'project_type'
             LEFT JOIN fact_current e ON e.subject_type = 'queue_project' AND e.subject_id = p.id AND e.field = 'end_use'
            WHERE p.first_seen_at < %s AND (p.iso = 'bpa' OR e.value IS NOT NULL)
              AND NOT (coalesce(t.source_name, '') = ANY(%s))
            ORDER BY p.id""",
        (day_end(), hidden),
    ).fetchall()
    out = []
    for r in rows:
        ptype = r["ptype"] if isinstance(r["ptype"], str) else ""
        use = r["end_use"] if isinstance(r["end_use"], str) else ""
        if (r["iso"] == "bpa" and "load" in ptype.lower() and BPA_DC_NAME.search(r["name"] or "")) or (r["iso"] == "nyiso" and NYISO_DC_USE.match(use)):
            out.append(r["id"])
    return out


def _link_ids(row: Row) -> list[str]:
    return [x["id"] for x in row["value"] if isinstance(x, dict) and isinstance(x.get("id"), str)] \
        if isinstance(row["value"], list) else []


def _linked(conn: Conn, queue_id: str) -> str | None:
    """The tracked campus the queue collector linked the entry to, directly or through a linked site."""
    rows = known_fact_rows(conn, "queue_project", queue_id, ["linked_campus", "linked_site"])
    for row in rows["linked_campus"]:
        for cid in _link_ids(row):
            return cid
    for row in rows["linked_site"]:
        for slug in _link_ids(row):
            hit = conn.execute(
                """SELECT campus_id FROM site_link WHERE site_slug = %s AND status = 'approved'
                     AND (approved_at IS NULL OR approved_at < %s) ORDER BY campus_id LIMIT 1""",
                (slug, day_end()),
            ).fetchone()
            if hit:
                return hit["campus_id"]
    return None


def queue_candidate(conn: Conn, queue_id: str) -> Candidate | None:
    f = known_facts(conn, "queue_project", queue_id, QUEUE_FIELDS)
    status_class = (f.get("status_class") or {}).get("value")
    mw_row = f.get("mw_requested")
    mw = mw_row["value"] if mw_row and isinstance(mw_row["value"], int | float) else None
    if status_class not in LIVE_QUEUE or (mw is not None and mw < MIN_QUEUE_MW):
        return None
    qp = conn.execute("SELECT iso, queue_id, name, state, county FROM queue_project WHERE id = %s", (queue_id,)).fetchone()
    if qp is None:
        return None
    iso = qp["iso"].upper()
    signals: list[Signal] = []
    if (row := f.get("queue_date")) is not None:
        who = (f.get("transmission_owner") or {}).get("value") or iso
        text = f"{iso} load interconnection request {qp['queue_id']}" + (f" for {mw:,.0f} MW" if mw else "")
        _keep(signals, _signal("power_requested", _date(row["value"]), f"{text} ({who})", row, "queue"))
    if (row := f.get("ia_date")) is not None:
        _keep(signals, _signal("power_requested", _date(row["value"]), f"{iso} interconnection agreement signed",
                               row, "queue"))
    if status_class == "under_construction" and (row := f.get("status")) is not None:
        _keep(signals, _signal("construction_started", row["valid_from"], f"{iso} queue status: {row['value']}",
                               row, "queue"))
    dev_row = f.get("developer")
    developer = _sourced_text(dev_row)
    mw_doc = _sourced(mw_row, mw) if mw is not None else None
    status = (f.get("status") or {}).get("value")
    return Candidate(
        key=f"queue:{queue_id}", origin="queue", name=qp["name"] or queue_id, state=qp["state"], county=qp["county"],
        developer=developer, mw=mw_doc, campus_link=_linked(conn, queue_id), signals=_dedupe(signals),
        extra={"queueStatus": status if isinstance(status, str) else None, "iso": iso},
    )


# ---------------------------------------------------------------- announced projects
def project_ids(conn: Conn) -> list[str]:
    hidden = hidden_source_names()
    rows = conn.execute(
        """SELECT DISTINCT subject_id FROM fact WHERE subject_type = 'pipeline_project' AND field = 'project'
             AND status IN ('published', 'superseded') AND learned_at < %s AND source_name <> ALL(%s) ORDER BY 1""",
        (day_end(), hidden),
    ).fetchall()
    return [r["subject_id"] for r in rows]


def project_candidate(conn: Conn, project_id: str) -> Candidate | None:
    rows = known_fact_rows(conn, "pipeline_project", project_id, list(PROJECT_FIELDS))
    if not rows["project"]:
        return None
    prof_row = rows["project"][0]
    prof = prof_row["value"] if isinstance(prof_row["value"], dict) else {}
    if not prof.get("name"):
        return None
    signals: list[Signal] = []
    for stage in STAGES:
        for row in rows[stage]:
            v = row["value"] if isinstance(row["value"], dict) else {"text": str(row["value"])}
            _keep(signals, _signal(stage, _date(v.get("date")) or row["valid_from"], str(v.get("text") or ""), row,
                                   "announcement"))
    mw_row = rows["planned_mw"][0] if rows["planned_mw"] else None
    mw = _sourced(mw_row, mw_row["value"]) if mw_row and isinstance(mw_row["value"], int | float) else None
    fin = None
    if rows["financing"]:
        r = rows["financing"][0]
        v = r["value"] if isinstance(r["value"], dict) else {"text": str(r["value"])}
        fin = {"text": str(v.get("text")), "tag": r["tag"], "sourceName": r["source_name"],
               "sourceUrl": r.get("source_url"), "asOf": r["valid_from"].isoformat(), "siteSlug": None,
               "debtB": v.get("debtB") if isinstance(v.get("debtB"), int | float) else None}
    nxt = None
    if rows["next_step"]:
        r = rows["next_step"][0]
        v = r["value"] if isinstance(r["value"], dict) else {"text": str(r["value"])}
        nxt = {"text": str(v.get("text")), "basis": "stated", "tag": r["tag"], "sourceName": r["source_name"],
               "sourceUrl": r.get("source_url")}
    point = None
    if isinstance(prof.get("lat"), int | float) and isinstance(prof.get("lon"), int | float):
        point = (float(prof["lat"]), float(prof["lon"]))
    return Candidate(
        key=f"project:{project_id}", origin="announced", name=str(prof["name"]), state=prof.get("state"),
        county=prof.get("county"), developer=_sourced(prof_row, prof.get("developer")) if prof.get("developer") else None,
        tenant=_sourced(prof_row, prof.get("tenant")) if prof.get("tenant") else None, point=point,
        parcels=frozenset(str(p) for p in prof.get("parcelIds") or []), mw=mw,
        campus_link=prof.get("campusId") if isinstance(prof.get("campusId"), str) else None,
        signals=_dedupe(signals), financing=fin, next_step=nxt,
        aliases=tuple(str(a) for a in prof.get("aliases") or ()),
        extra={"place": prof.get("place")},
    )


# ---------------------------------------------------------------- helpers
def _sourced(row: Row, value: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"value": value, "tag": row["tag"], "asOf": row["valid_from"].isoformat(),
                           "sourceName": row["source_name"]}
    if row.get("source_url"):
        out["sourceUrl"] = row["source_url"]
    return out


def _sourced_text(row: Row | None) -> dict[str, Any] | None:
    return _sourced(row, row["value"]) if row is not None and isinstance(row["value"], str) and row["value"] else None


def _dedupe(signals: list[Signal]) -> list[Signal]:
    seen: set[tuple[str, date, str]] = set()
    out: list[Signal] = []
    for s in sorted(signals, key=lambda s: (s.day, STAGES.index(s.stage)), reverse=True):
        key = (s.stage, s.day, s.text)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out
