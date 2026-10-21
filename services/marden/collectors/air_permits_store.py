"""Writing matched air permits onto plants (spec 15 'Air permit card', spec 20 owner). See air_permits.

Per plant with at least one matched facility, change-only (a value equal to the one this collector last wrote
is not written again, so reruns write nothing):

- `air_permit`: the plant's main permit (the newest generating-unit permit: PSD / NSR / standard permit before a
  Title V or PBR; then the latest activity) as {agency, number, ids, type, status, agency_status, applicant,
  filed, issued, expires, units, emission_limits, amendments, actions, url, quote, facility, match}. `units` and
  `emission_limits` are the statements of the agency's record, each with its quote, URL and document id.
  `amendments` are the decisions after the first one (spec 15: "amendments in order").
- `air_permits`: every matched permit in the same shape.
- `air_permit_number`: the permit numbers as one string, for the API's permit-number search.
- events of kind `permit` on the plant, one per dated action (application, notice, draft / preliminary
  decision, decision, withdrawal), with the record's URL.

Tag F (the agency's record states the values), source_name the agency, method_version air-permit-match@0.1 (how
the record was tied to the plant; the evidence is in `match`). valid_from is the day Marden read the record.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.collectors.air_permits_match import METHOD_VERSION, Action, Match, Permit
from marden.collectors.rates_common import RunStats, today_et
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.jsonutil import canonical_json, to_jsonable
from marden.core.records import EventWriter

PRODUCER = "collector:air_permits"
FIELDS = ("air_permit", "air_permits", "air_permit_number")
TYPE_RANK = {"PSD": 0, "NSR": 1, "Standard": 1, "Minor": 1, "Major": 0, "Title V": 3, "PBR": 4}
ACTION_WORDS = {"application": "application received", "notice": "public notice", "draft": "preliminary decision",
                "final_action": "decision", "withdrawal": "withdrawn", "completeness": "application complete",
                "hearing": "hearing"}


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def action_doc(a: Action) -> dict[str, Any]:
    out = {"date": a.day.isoformat(), "kind": a.kind, "text": a.text, "url": a.url}
    if a.project:
        out["project"] = a.project
    if a.quote:
        out["quote"] = a.quote
    if a.document_id:
        out["document_id"] = a.document_id
    return out


def amendments(p: Permit) -> list[dict[str, Any]]:
    decisions = sorted((a for a in p.actions if a.kind in ("final_action", "withdrawal")), key=lambda a: a.day)
    return [action_doc(a) for a in decisions[1:]]


def permit_doc(p: Permit) -> dict[str, Any]:
    return {
        "agency": p.agency, "number": p.number, "ids": p.ids, "type": p.type, "status": p.status,
        "agency_status": p.agency_status, "applicant": p.applicant, "filed": iso(p.filed), "issued": iso(p.issued),
        "expires": iso(p.expires), "units": p.units, "emission_limits": p.emission_limits,
        "amendments": amendments(p), "actions": [action_doc(a) for a in sorted(p.actions, key=lambda a: a.day)],
        "url": p.url, "quote": p.quote,
    }


def last_activity(p: Permit) -> date:
    days = [a.day for a in p.actions] + [d for d in (p.filed, p.issued) if d]
    return max(days, default=date.min)


def primary(permits: list[Permit]) -> Permit:
    """Generating-unit permits before Title V / PBR; an issued permit before a pending one of the same kind (the
    authorization in force); then the latest activity."""
    live = [p for p in permits if p.status not in ("withdrawn", "cancelled", "denied", "inactive")] or permits
    return min(live, key=lambda p: (TYPE_RANK.get(p.type or "NSR", 2), p.status != "issued",
                                    -last_activity(p).toordinal(), p.number))


def _last_written(conn: Conn, plant_id: str, field: str) -> Any:
    row = conn.execute(
        """SELECT value FROM fact WHERE subject_type = 'plant' AND subject_id = %s AND field = %s AND producer = %s
            AND status = 'published' ORDER BY valid_from DESC, learned_at DESC, id DESC LIMIT 1""",
        (plant_id, field, PRODUCER),
    ).fetchone()
    return row["value"] if row else None


def write_plant(conn: Conn, plant_id: str, matches: list[Match], stats: RunStats) -> dict[str, Any]:
    """Facts and events for one plant from its matched facilities; returns what was written."""
    pairs = [(m, p) for m in matches for p in m.facility.permits]
    if not pairs:
        return {"plant": plant_id, "permits": 0, "facts_new": 0, "events_new": 0}
    main_match, main = next((m, p) for m, p in pairs if p is primary([p for _, p in pairs]))
    f = main_match.facility
    value = dict(permit_doc(main), facility={"system": f.system, "key": f.key, "name": f.name, "county": f.county,
                                             "url": f.url}, match=main_match.evidence)
    every = [dict(permit_doc(p), facility=m.facility.key) for m, p in pairs]
    numbers = ", ".join(dict.fromkeys(i for _, p in pairs for i in p.ids))
    writer = FactWriter(conn)
    day = today_et()
    new = 0
    for field, v in (("air_permit", value), ("air_permits", every), ("air_permit_number", numbers)):
        stats.facts_seen += 1
        if canonical_json(to_jsonable(_last_written(conn, plant_id, field))) == canonical_json(to_jsonable(v)):
            continue
        res = writer.add(
            "plant", plant_id, field, v, tag="F", source_name=main.agency, source_url=main.url or f.url,
            valid_from=day, producer=PRODUCER, status="published", document_id=main.document_id or f.document_id,
            quote=main.quote or f.quote, quote_loc=main.quote_loc, method_version=METHOD_VERSION,
        )
        new += int(res.created)
    stats.facts_new += new
    events = EventWriter(conn)
    ev_new = 0
    for _, p in pairs:
        for a in p.actions:
            res = events.add(
                "plant", plant_id, a.day, "permit", f"{p.agency} air permit {p.number}: "
                f"{ACTION_WORDS.get(a.kind, a.kind)}" + (f" (project {a.project})" if a.project else ""),
                tag="F", source_name=p.agency, source_url=a.url or p.url, document_id=a.document_id,
                status="published",
            )
            ev_new += int(res.created)
    stats.extra.setdefault("events_new", 0)
    stats.extra["events_new"] += ev_new
    return {"plant": plant_id, "permits": len(pairs), "primary": main.number, "facts_new": new, "events_new": ev_new}
