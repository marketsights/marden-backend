"""Shared model for ISO/RTO interconnection queue collectors (pjm_queue, miso_queue, nyiso_queue,
isone_queue; other queue collectors can reuse it).

- Every queue entry is subject_type 'queue_project', subject_id '<iso>:<queue id>', with an identity
  row in `queue_project` (db/migrations/20261021140000_queue_project.sql).
- Facts, tag F, source_name = the operator, source_url = the published file, producer
  'collector:<name>': name, iso, developer, transmission_owner, county, state, poi, fuel (generation
  types; 'LOAD' for large loads), project_type, mw_requested (MW), status (the operator's words),
  status_class, queue_date, proposed_cod, actual_cod, withdrawn_date, study_phase, and operator
  extras (cycle, zone, end_use). valid_from is the entry's own last-updated date when the operator
  publishes one, else the report date.
- Change-only: a value is written only when it differs from this collector's latest value for that
  subject and field, so an unchanged queue writes nothing on rerun and history is never rewritten.
- Links (tag M, method iso_queue_match.METHOD_VERSION): linked_site / linked_campus / linked_plant,
  each a list of {id, name, evidence}; [] once a previous link stops matching.
- Events (kind 'interconnection') on linked subjects when an entry's status or proposed COD changes
  between runs, or a new entry appears at a linked subject after the ISO's first run.
- Large loads (fuel LOAD) linked to a site or campus are also a fact on that subject,
  `large_load_queue_<iso>`: the list of its load requests in that ISO's queue.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from marden.collectors import iso_queue_match as qmatch
from marden.collectors.rates_common import RunStats
from marden.core import checks
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.jsonutil import canonical_json
from marden.core.records import EventWriter

SUBJECT = "queue_project"
LOAD = "LOAD"
FIELDS = (
    "name", "iso", "developer", "transmission_owner", "county", "state", "poi", "fuel", "project_type",
    "mw_requested", "status", "status_class", "queue_date", "proposed_cod", "actual_cod", "withdrawn_date",
    "study_phase", "cycle", "zone", "end_use",
)
UNITS = {"mw_requested": "MW", "queue_date": "date", "actual_cod": "date", "withdrawn_date": "date"}
EVENT_FIELDS = ("status", "proposed_cod")
BLANK = frozenset({"", "n/a", "na", "none", "null", "-", "--", "tbd", "not specified", "unknown"})
STATUS_CLASSES = ("withdrawn", "retired", "in_service", "under_construction", "suspended", "active")
_CLASS_RULES = (  # first match wins; tested against the lower-cased operator status
    ("withdrawn", r"withdraw|retract|annul|cancel|terminat|rejected|^w$|^0$"),
    ("retired", r"deactivat|retire"),
    ("under_construction", r"partial|under construction|^uc|construction"),
    ("in_service", r"in service|in-service|^i/s$|commercial|^c$|operat"),
    ("suspended", r"suspend"),
)
LARGE_LOAD_KEYS = (
    "queue_id", "name", "developer", "mw_requested", "status", "status_class", "queue_date", "proposed_cod",
    "poi", "transmission_owner", "zone", "end_use", "study_phase",
)


# ---------------------------------------------------------------- value helpers
def text(value: Any) -> str | None:
    """Trimmed text with inner whitespace collapsed; None for blanks and placeholders."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = " ".join(str(value).split())
    return None if s.lower() in BLANK else s


def number(value: Any) -> int | float | None:
    """A published MW figure; None for blanks and non-numbers ('TBD')."""
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None
    if not dec.is_finite():
        return None
    return int(dec) if dec == dec.to_integral_value() else float(dec)


def us_date(value: Any) -> date | None:
    """Dates as queues publish them: datetime cells, 'M/D/YYYY', 'YYYY-MM-DD'. Implausible years -> None."""
    if isinstance(value, datetime):
        day = value.date()
    elif isinstance(value, date):
        day = value
    else:
        s = text(value)
        if s is None:
            return None
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})(?: .*)?", s)
        m2 = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", s)
        try:
            if m:
                day = date(int(m[3]), int(m[1]), int(m[2]))
            elif m2:
                day = date(int(m2[1]), int(m2[2]), int(m2[3]))
            else:
                return None
        except ValueError:
            return None
    return day if 1950 <= day.year <= 2100 else None


def month_or_date(value: Any) -> str | None:
    """Planned dates published as a date, 'MM-YYYY', 'MM/YYYY' or 'YYYY': ISO 'YYYY-MM-DD',
    'YYYY-MM' or 'YYYY'. Words such as 'I/S' or 'N/A' are not dates (None)."""
    day = us_date(value)
    if day is not None:
        return day.isoformat()
    s = text(value)
    if s is None:
        return None
    m = re.fullmatch(r"(\d{1,2})\s*[-/]\s*(\d{4})", s)
    if m and 1 <= int(m[1]) <= 12:
        return f"{int(m[2]):04d}-{int(m[1]):02d}"
    m = re.fullmatch(r"(\d{4})\s*[-/]\s*(\d{1,2})", s)
    if m and 1 <= int(m[2]) <= 12:
        return f"{int(m[1]):04d}-{int(m[2]):02d}"
    if re.fullmatch(r"(19|20)\d{2}", s):
        return s
    return None


def status_class(status: str | None) -> str | None:
    if not status:
        return None
    low = status.lower().strip()
    for cls, pattern in _CLASS_RULES:
        if re.search(pattern, low):
            return cls
    return "active"


@dataclass
class QueueEntry:
    """One parsed queue row. `values` holds FIELDS (blanks dropped); `updated` is the operator's
    last-updated date for the row, if it publishes one."""

    queue_id: str
    values: dict[str, Any]
    updated: date | None = None

    def __post_init__(self) -> None:
        unknown = set(self.values) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown queue fields {sorted(unknown)}")
        self.values = {k: v for k, v in self.values.items() if v is not None and v != ""}
        if "status" in self.values and "status_class" not in self.values:
            self.values["status_class"] = status_class(self.values["status"])


# ---------------------------------------------------------------- the store
@dataclass
class QueueStore:
    """Writes one ISO's parsed entries on the caller's connection (one transaction per report)."""

    conn: Conn
    iso: str  # 'pjm', 'nyiso' ... (subject id prefix)
    source_name: str  # 'PJM', 'NYISO' ...
    producer: str
    report_date: date
    stats: RunStats
    prior: dict[tuple[str, str], tuple[Any, date]] = field(default_factory=dict)
    loads: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    identities: dict[str, tuple[str, str | None, str | None, str | None]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        checks.not_future(self.report_date, "report_date")
        self.facts = FactWriter(self.conn)
        self.events = EventWriter(self.conn)
        self.targets = qmatch.load_targets(self.conn)
        rows = self.conn.execute(
            """SELECT DISTINCT ON (subject_type, subject_id, field) subject_type, subject_id, field, value, valid_from
                 FROM fact
                WHERE producer = %s AND status = 'published'
                  AND ((subject_type = %s AND subject_id LIKE %s) OR field = %s)
                ORDER BY subject_type, subject_id, field, valid_from DESC, learned_at DESC, id DESC""",
            (self.producer, SUBJECT, f"{self.iso}:%", self.load_field),
        ).fetchall()
        for r in rows:
            sid = r["subject_id"] if r["subject_type"] == SUBJECT else f"{r['subject_type']}|{r['subject_id']}"
            self.prior[(sid, r["field"])] = (r["value"], r["valid_from"])
        self.first_run = not any(k[0].startswith(f"{self.iso}:") for k in self.prior)
        self.counts: Counter[str] = Counter()
        self.linked: dict[str, list[str]] = {}

    @property
    def load_field(self) -> str:
        return f"large_load_queue_{self.iso}"

    def _day(self, candidate: date | None, prior_day: date | None) -> date:
        day = candidate if candidate is not None and candidate <= self.report_date else self.report_date
        return max(day, prior_day) if prior_day is not None else day  # a new value never sorts below the old

    def _put(self, subject_type: str, subject_id: str, key: str, fld: str, value: Any, *, tag: str = "F",
             day: date | None, source_url: str, document_id: int | None) -> bool:
        old = self.prior.get((key, fld))
        if old is not None and canonical_json(old[0]) == canonical_json(value):
            return False
        res = self.facts.add(
            subject_type, subject_id, fld, value, tag=tag, source_name=self.source_name, source_url=source_url,
            valid_from=self._day(day, old[1] if old else None), producer=self.producer, status="published",
            unit=UNITS.get(fld), document_id=document_id,
            method_version=qmatch.METHOD_VERSION if tag == "M" else None,
        )
        self.stats.facts_seen += 1
        if res.created:
            self.stats.facts_new += 1
            self.counts[f"facts:{fld}"] += 1
        return res.created

    def write(self, entry: QueueEntry, *, source_url: str, document_id: int | None) -> None:
        sid = f"{self.iso}:{entry.queue_id}"
        values = {"iso": self.iso.upper() if self.iso != "isone" else "ISO-NE", **entry.values}
        self.counts["entries"] += 1
        self.counts[f"status_class:{values.get('status_class') or 'n/d'}"] += 1
        self.identities[sid] = (entry.queue_id, values.get("name"), values.get("state"), values.get("county"))
        before = {f: (self.prior.get((sid, f)) or (None,))[0] for f in EVENT_FIELDS}
        is_new = not any(k[0] == sid for k in ((sid, f) for f in FIELDS) if k in self.prior)
        for fld in FIELDS:
            if fld in values:
                self._put(SUBJECT, sid, sid, fld, values[fld], day=entry.updated, source_url=source_url,
                          document_id=document_id)
        links = qmatch.match(values, self.targets)
        for kind in qmatch.LINK_KINDS:
            fld = f"linked_{kind}"
            new = links.get(kind, [])
            if new or self.prior.get((sid, fld)):
                self._put(SUBJECT, sid, sid, fld, new, tag="M", day=self.report_date, source_url=source_url,
                          document_id=document_id)
        if not links:
            return
        self.linked[sid] = [f"{kind}:{link['id']}" for kind, ls in links.items() for link in ls]
        self._events(sid, entry, values, links, before, is_new, source_url, document_id)
        if values.get("fuel") == LOAD:
            summary = {"queue_id": entry.queue_id, **{k: values[k] for k in LARGE_LOAD_KEYS if k in values}}
            for kind in ("site", "campus"):
                for link in links.get(kind, []):
                    self.loads.setdefault((kind, link["id"]), []).append({**summary, "match": link["evidence"]})

    def _events(self, sid: str, entry: QueueEntry, values: dict[str, Any], links: dict[str, list[dict[str, Any]]],
                before: dict[str, Any], is_new: bool, source_url: str, document_id: int | None) -> None:
        label = f"{self.source_name} queue {entry.queue_id}" + (f" ({values['name']})" if values.get("name") else "")
        titles: list[str] = []
        if is_new and not self.first_run:
            mw = f"{values['mw_requested']} MW " if "mw_requested" in values else ""
            titles.append(f"{label} filed: {mw}{values.get('fuel') or ''}, status {values.get('status', 'n/d')}")
        for fld in EVENT_FIELDS:
            old, new = before[fld], values.get(fld)
            if old is not None and new is not None and canonical_json(old) != canonical_json(new):
                titles.append(f"{label}: {fld.replace('_', ' ')} {old} → {new}")
        day = entry.updated if entry.updated and entry.updated <= self.report_date else self.report_date
        for title in titles:
            for kind, ls in links.items():
                for link in ls:
                    res = self.events.add(
                        kind, link["id"], day, "interconnection", title, tag="F", source_name=self.source_name,
                        source_url=source_url, document_id=document_id, status="published",
                    )
                    self.counts["events_new"] += int(res.created)

    def finish(self, *, source_url: str, document_id: int | None) -> None:
        """Write identity rows and the per-subject large-load lists; record counts on the run."""
        if self.identities:
            ids = sorted(self.identities)
            cols = list(zip(*(self.identities[i] for i in ids), strict=True))
            row = self.conn.execute(
                """WITH up AS (
                     INSERT INTO queue_project (id, iso, queue_id, name, state, county)
                     SELECT t.id, %s, t.q, t.n, t.s, t.c
                       FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[]) AS t(id, q, n, s, c)
                     ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, state = EXCLUDED.state,
                                                    county = EXCLUDED.county
                      WHERE (queue_project.name, queue_project.state, queue_project.county)
                            IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.state, EXCLUDED.county)
                     RETURNING (xmax = 0) AS inserted)
                   SELECT count(*) FILTER (WHERE inserted) AS inserted, count(*) AS changed FROM up""",
                (self.iso, ids, *[list(c) for c in cols]),
            ).fetchone()
            assert row is not None
            self.counts["entries_new"] += row["inserted"]
        previous = {k[0] for k in self.prior if k[1] == self.load_field and "|" in k[0]}
        current = {f"{kind}|{sid}" for kind, sid in self.loads}
        for key in sorted(previous | current):
            kind, subject_id = key.split("|", 1)
            reqs = sorted(self.loads.get((kind, subject_id), []), key=lambda r: r["queue_id"])
            self._put(kind, subject_id, key, self.load_field, reqs, day=self.report_date, source_url=source_url,
                      document_id=document_id)
        self.stats.extra.setdefault("iso", {})[self.iso] = {
            "report_date": self.report_date.isoformat(), **dict(sorted(self.counts.items())),
            "linked": dict(sorted(self.linked.items())),
        }
