"""When a withdrawn permit or queue position is a replacement, not a power failure (status v0.4).

T2's power test reads 'power denied or withdrawn'. A denial stands on its own, but a developer withdraws an
application all the time to refile it, to fold it into another permit or to move the same units to another
record: Stargate Abilene's on-site plant withdrew TCEQ air permit 182267 on 2026-04-20 while its other
permits stayed in force (177262, 177263, Title V O4721) and the PSD permit 182126 for the plant went to
public notice in August. So a withdrawal (`status_text` rule 'power_withdrawn') counts only when, on the
run's as-of day, no other permit, application or queue position for the same plant / campus is live.

The records come from what the run already reads point in time (`status_inputs`):

- permit, queue and interconnection events on the site and its approved campuses and plants: one record
  per permit number or queue id, in the state of its latest action on or before the day. Withdrawn,
  denied, rejected, cancelled, revoked, vacated, expired, terminated, retired: ended; an application,
  notice, preliminary decision, hearing or decision: live (a 'decision' reads as issued; the agency's
  record says when one was a denial, below);
- `air_permits` facts on the site and its plants (each permit's own status) and the plants' `queue_ids`
  with each queue position's `status_class` (active, under construction or in service: live).

Scope: a withdrawal on a plant is replaced by a live record on that plant, the campus it serves or the site;
on a campus, by one on the campus, its plants or the site; on the site, by one on any of them. A later
live record for the same permit (refiled, reinstated) replaces it too; an earlier one does not.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date

from marden.models.status_inputs import Authorization, Item, SiteInputs

RECORD_FIELDS = frozenset({"event:permit", "event:queue", "event:interconnection"})
LIVE_QUEUE = frozenset({"active", "under_construction", "in_service"})
ENDED = re.compile(r"\b(?:withdr\w+|denied|denials?|rejected|cancel+ed|revoked|vacated|expired|terminated|retired"
                   r"|inactive|void(?:ed)?)\b", re.I)
PERMIT_TITLE = re.compile(r"\bpermits?\s+(?P<key>[^:]+?)\s*:\s*(?P<action>.+)$", re.I)
QUEUE_TITLE = re.compile(r"\bqueue\s+(?P<key>[A-Za-z0-9][\w.\-]*)[^:]*:\s*(?P<action>.+)$", re.I)
QUEUE_LEAD = re.compile(r"^\s*(?P<status>withdrawn|active|suspended|in[- ]service|operational|cancel+ed|retracted"
                        r"|deactivated|completed|under construction)\s+(?P<key>.+)$", re.I)
_ARROW = re.compile(r"\s*(?:→|->)\s*")
IN_FORCE = re.compile(r"\b(?:decision|issued|active|approved|final|in[_ -]service|operational|completed)\b", re.I)
_PRELIMINARY = re.compile(r"\b(?:preliminary|draft|proposed)\s+decision\b", re.I)


@dataclass(frozen=True)
class Record:
    subject: str  # 'site:<slug>' | 'campus:<id>' | 'plant:<id>'
    kind: str  # 'permit' | 'queue'
    key: str  # normalized permit number / queue id
    ids: frozenset[str]  # every normalized number the record gives
    live: bool
    pending: bool  # live and not yet in force: an application, notice, draft decision, a queue position in study
    day: date
    text: str
    cite: str  # 'source, day'
    source: str  # 'event' | 'fact'


def norm_ids(raw: str) -> set[str]:
    """'NSR 10883' -> {'NSR10883', '10883'}; '26-A-117, 26-A-118' -> {'26A117', '26A118'}."""
    out = set()
    for part in re.split(r"\s*[,;/]\s*", raw):
        part = part.strip()
        if not part:
            continue
        out.add(re.sub(r"[^A-Z0-9]", "", part.upper()))
        if " " in part:
            out.add(re.sub(r"[^A-Z0-9]", "", part.split()[-1].upper()))
    return {i for i in out if i}


def _state(action: str) -> tuple[bool, bool]:
    """(live, pending) of a record whose latest action or status is `action` (for a 'status A → B' change,
    B): live unless it ends the record; pending while live and not in force (a draft decision is pending)."""
    now = _ARROW.split(action)[-1]
    live = not ENDED.search(now)
    return live, live and not IN_FORCE.search(_PRELIMINARY.sub(" ", now))


def event_record(it: Item) -> Record | None:
    """The permit or queue record a permit / queue / interconnection event is about, in that event's state."""
    if it.field not in RECORD_FIELDS:
        return None
    cite = f"{it.source_name}, {it.day.isoformat()}"
    if it.field == "event:permit":
        m = PERMIT_TITLE.search(it.text)
        if m is None or not (ids := norm_ids(m.group("key"))):
            return None
        live, pending = _state(m.group("action"))
        return Record(it.subject, "permit", min(ids), frozenset(ids), live, pending, it.day, it.text, cite, "event")
    if (m := QUEUE_TITLE.search(it.text)) is not None:
        key, (live, pending) = m.group("key").upper(), _state(m.group("action"))
    elif (m := QUEUE_LEAD.match(it.text)) is not None:
        key, (live, pending) = re.sub(r"[^A-Z0-9]", "", m.group("key").upper()), _state(m.group("status"))
    else:
        return None
    return Record(it.subject, "queue", key, frozenset({key}), live, pending, it.day, it.text, cite, "event")


def fact_record(a: Authorization) -> Record:
    ids = set().union(*(norm_ids(i) for i in a.ids)) | norm_ids(a.key)
    if a.kind == "queue":
        live = a.status.lower() in LIVE_QUEUE
        pending = live and a.status.lower() != "in_service"
    else:
        live, pending = _state(a.status)
    label = "queue position" if a.kind == "queue" else "permit"
    return Record(a.subject, a.kind, min(norm_ids(a.key) or {a.key}), frozenset(ids), live, pending, a.day,
                  f"{label} {a.key}: {a.status or 'status n/d'}", f"{a.source_name}, {a.day.isoformat()}", "fact")


def records(site: SiteInputs) -> list[Record]:
    """Each permit / queue record on the site, its campuses and plants, in its latest state on the run day
    (the latest event or fact; same day: live last, so it wins). Shown by its latest event since it came
    into that state (the dated action, with its source), else by the fact."""
    groups: dict[tuple[str, str, str], list[Record]] = defaultdict(list)
    for r in [r for it in site.items if (r := event_record(it)) is not None] + [
            fact_record(a) for a in site.authorizations]:
        groups[(r.subject, r.kind, r.key)].append(r)
    out = []
    for group in groups.values():
        group.sort(key=lambda r: (r.day, r.live, r.text))
        last = group[-1]
        same: list[Record] = []  # the latest run of records in the record's present state
        for r in reversed(group):
            if r.live != last.live:
                break
            same.append(r)
        shown = next((r for r in same if r.source == "event"), last)
        out.append(replace(shown, live=last.live, pending=last.pending,
                           ids=frozenset().union(*(r.ids for r in group))))
    return out


def scope(site: SiteInputs, subject: str) -> set[str]:
    """The subjects whose records can replace a withdrawal on `subject` (module docstring)."""
    kind, _, sid = subject.partition(":")
    out = {subject, f"site:{site.slug}"}
    if kind == "plant":
        out |= {f"campus:{c}" for c, plants in site.links.items() if sid in plants}
    elif kind == "campus":
        out |= {f"plant:{p}" for p in site.links.get(sid, [])}
    else:
        out |= {f"campus:{c}" for c in site.links} | {f"plant:{p}" for ps in site.links.values() for p in ps}
    return out


def replacement(site: SiteInputs, item: Item) -> Record | None:
    """The live permit, application or queue position that replaces the withdrawal `item` states, or None
    (the withdrawal then counts). Cites a record on the same subject first, then one still pending (an
    application in progress is what replaces a withdrawn one), then the latest."""
    own = event_record(item)
    where = scope(site, item.subject)
    live = []
    for r in records(site):
        if r.subject not in where or not r.live:
            continue
        if own is not None and r.kind == own.kind and r.ids & own.ids and r.day < own.day:
            continue  # the same permit before it was withdrawn
        live.append(r)
    if not live:
        return None
    return min(live, key=lambda r: (r.subject != item.subject, not r.pending, -r.day.toordinal(), r.kind, r.key,
                                    r.text))
