"""Campus detail tabs (spec 14, 27): `dc_tab:buildings` (types.ts Building[]), `dc_tab:records`
(DcRecord[]), `dc_tab:history` (DcHistoryEvent[]) and `dc_tab:capacity` (CapacityEstimate[]).

- Buildings: the campus's building rows with footprints, each with its own facts (sites_links.building_doc,
  the same shape the site Buildings tab shows).
- Records: every source document Marden holds for the campus, newest first: published events
  (dockets, agendas, filings, press), the monthly Sentinel-2 images, the large-load requests and
  the other documents its facts were read from (one record per document URL; the spatial joins and
  Marden's own rules are methods, not documents). Type chips by agency and host.
- History: status and capacity changes (before -> after) from every value Marden recorded for the
  campus, and the first-seen date.
- Capacity: the `capacity` model's output (spec 27) for the campus and then each building. Until that
  model has run the list is empty: the estimate is the model's to make, never the builder's.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.facilities_common import (
    FIELD_LABELS,
    FIRST_SEEN,
    GRID_JOIN,
    Change,
    campus_buildings,
    campus_ids,
    campus_row,
    fact_changes,
    field_label,
    first_seen,
    history_entries,
    subject_events,
)
from marden.readmodels.views.sites_common import Doc, day, event_evidence, fact_evidence, latest_output
from marden.readmodels.views.sites_links import building_doc
from marden.readmodels.views.sites_text import text_of

HISTORY_FIELDS = (
    "status",
    "facility_mw",
    "it_mw",
    "operating_mw",
    "operating_it_mw",
    "buildings_operational",
    "building_count",
    "power_source",
    "developer",
    "tenant",
    "online_year",
)
HISTORY_INITIAL = ("status", "facility_mw", "it_mw")
# Fields whose source is not a document about the campus (methods, joins, Marden's own reads).
NOT_DOCUMENTS = frozenset(
    {
        "imagery",
        "change_score",
        "last_visible_change",
        "imagery_note",
        "copyright_notice",
        "site_link_evidence",
        "materiality_note",
        "large_load_request",
        "capacity_plan",
    }
)
METHOD_SOURCES = ("hifld", "us-atlas", "marden")
_TYPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Court", re.compile(r"\bcourt\b|\bappeal|\blawsuit", re.I)),
    ("Zoning", re.compile(r"zoning|rezon|planning|county|parish|city of|township|council|annex|site plan", re.I)),
    ("Permit", re.compile(r"\btceq\b|\bnmed\b|\bdeq\b|\bepa\b|air permit|\bpermit|\btdlr\b|tabs", re.I)),
    (
        "Utility",
        re.compile(
            r"\bferc\b|\bpsc\b|\bpuc\b|\bmpsc\b|\bpscw\b|commission|\bercot\b|\bpjm\b|\bmiso\b|"
            r"\bspp\b|\bnyiso\b|utility|electric|interconnection|large.load|transmission",
            re.I,
        ),
    ),
    ("Filing", re.compile(r"\bsec\b|edgar|10-[kq]|8-k", re.I)),
)
CAPACITY_KEYS = frozenset({"facilityMw", "itMw", "gpus", "gpuValueB", "methods"})
# Every CapacityEstimate field (types.ts); the model's own bookkeeping (`missing`, `basis`) stays out of the doc.
CAPACITY_FIELDS = CAPACITY_KEYS | {"scope", "id", "name", "pue", "gpuGeneration", "kwPerGpu", "gpuShareOfIt"}
CAPACITY_FIELDS |= {"confidence", "disagreement", "replacedByFiling", "methodVersion"}


def doc_type(kind: str, *texts: str | None) -> str:
    """DocType chip of a record: Press for news, else by the first text (agency, source, host, title,
    in that order) whose words name a kind of body, else Filing."""
    if kind == "news":
        return "Press"
    for text in texts:
        if text and (label := next((lb for lb, pattern in _TYPES if pattern.search(text)), None)):
            return label
    return "Filing"


def agency_of(ev: Row) -> str:
    """The body that issued a record: the 'Agency: ...' prefix the archive puts on docket titles, else
    the source (publication, portal)."""
    title, source = ev["title"] or "", ev.get("source_name") or ""
    head, sep, _ = title.partition(": ")
    if ev["kind"] != "news" and sep and 1 < len(head) <= 60:
        return head
    return source or "n/d"


def _host(url: str | None) -> str:
    return urlparse(url).netloc.lower() if url else ""


# ---------------------------------------------------------------- records
def campus_records(conn: Conn, campus_id: str) -> list[Doc]:
    out: list[Doc] = []
    seen_events: set[tuple[str, str]] = set()
    # A docket item and its news copy are one document: the docket record first.
    for ev in sorted(subject_events(conn, "campus", campus_id), key=lambda e: e["kind"] == "news"):
        rec = event_evidence(ev)
        if (rec["date"], rec["sourceUrl"] or rec["text"]) in seen_events:
            continue
        seen_events.add((rec["date"], rec["sourceUrl"] or rec["text"]))
        out.append(
            {
                **rec,
                "type": doc_type(
                    ev["kind"], agency_of(ev), ev.get("source_name"), _host(ev.get("source_url")), ev["title"]
                ),
                "agency": agency_of(ev),
            }
        )
    rows = known_fact_rows(conn, "campus", campus_id, ["imagery", "large_load_request"])
    for row in rows["imagery"]:
        v = row["value"] if isinstance(row["value"], dict) else {}
        if isinstance(v.get("url"), str) and row["valid_from"] <= day():
            rec = fact_evidence(row, v.get("caption") or f"Sentinel-2 image {v.get('month')}")
            out.append({**rec, "sourceUrl": v["url"], "type": "Imagery", "agency": row.get("source_name") or "n/d"})
    for row in rows["large_load_request"]:
        v = row["value"] if isinstance(row["value"], dict) else {}
        if row["valid_from"] <= day():
            out.append(
                {
                    **fact_evidence(row, request_text(v)),
                    "type": "Utility",
                    "agency": str(v.get("utility") or row.get("source_name") or "n/d"),
                }
            )
    seen = {r["sourceUrl"] for r in out if r["sourceUrl"]}
    out.extend(_fact_documents(conn, campus_id, seen))
    out.sort(key=lambda r: (r["date"], r["type"], r["text"]), reverse=True)
    return out


def _fact_documents(conn: Conn, campus_id: str, seen: set[str]) -> list[Doc]:
    """One record per document URL the campus's current facts were read from."""
    by_url: dict[str, list[Row]] = {}
    order = {f: i for i, f in enumerate(FIELD_LABELS)}
    for field, row in sorted(
        known_facts(conn, "campus", campus_id).items(), key=lambda kv: (order.get(kv[0], 999), kv[0])
    ):
        url = row.get("source_url")
        source = (row.get("source_name") or "").lower()
        if (
            not url
            or url in seen
            or field in NOT_DOCUMENTS
            or row["tag"] == "M"
            or row.get("producer") == GRID_JOIN
            or source.startswith(METHOD_SOURCES)
            or row["valid_from"] > day()
        ):
            continue
        by_url.setdefault(url, []).append(row)
    out = []
    for url, rows in by_url.items():
        lead = max(rows, key=lambda r: (r["valid_from"], r["id"]))
        parts = [f"{field_label(r['field'])}: {t}" for r in rows if (t := text_of(r["value"], 120))]
        rec = fact_evidence(lead, "; ".join(parts[:4]) or field_label(lead["field"]))
        kind = "news" if lead["tag"] == "R" else "filing"
        out.append(
            {
                **rec,
                "sourceUrl": url,
                "type": doc_type(kind, lead.get("source_name"), _host(url)),
                "agency": lead.get("source_name") or _host(url) or "n/d",
            }
        )
    return out


def request_text(v: dict[str, Any]) -> str:
    """A large-load / interconnection request as stated: name, utility, MW, stage."""
    parts = [str(v[k]) for k in ("name", "utility") if v.get(k)]
    if isinstance(v.get("mw"), int | float):
        parts.append(f"{v['mw']:,.0f} MW")
    if v.get("stage"):
        parts.append(f"({v['stage']})")
    return " · ".join(parts) or text_of(v) or "Large-load request"


# ---------------------------------------------------------------- history
def campus_changes(conn: Conn, campus_id: str) -> list[Change]:
    """The History tab's changes (value changes, first status / MW, first seen), unordered."""
    out = fact_changes(conn, "campus", campus_id, HISTORY_FIELDS, initial=HISTORY_INITIAL)
    row = known_facts(conn, "campus", campus_id, ["first_seen"]).get("first_seen")
    when = first_seen({"first_seen": row}, None) if row else None
    if row is not None and when and when <= day().isoformat():
        entry = {
            "date": when,
            "field": "first_seen",
            "before": None,
            "after": "First seen",
            "evidence": fact_evidence(row, f"First seen in Marden's records {when}", when),
        }
        out.append(Change(entry, row["learned_at"], FIRST_SEEN, 0, row["id"]))
    return out


def campus_history(conn: Conn, campus_id: str) -> list[Doc]:
    return history_entries(campus_changes(conn, campus_id))


# ---------------------------------------------------------------- buildings and capacity
def building_docs(conn: Conn, campus_id: str) -> list[Doc]:
    return [d for b in campus_buildings(conn, campus_id) if (d := building_doc(conn, b)) is not None]


def capacity_estimates(conn: Conn, campus: Row) -> list[Doc]:
    """The capacity model's estimate for the campus, then for each building (spec 27)."""
    subjects = [("campus", campus["id"], campus["name"])]
    subjects += [("building", b["id"], b["label"] or b["id"]) for b in campus_buildings(conn, campus["id"])]
    out = []
    for scope, sid, name in subjects:
        output = latest_output(conn, "capacity", scope, sid)
        value = output["output"] if output else None
        if isinstance(value, dict) and set(value) >= CAPACITY_KEYS:
            kept = {k: v for k, v in value.items() if k in CAPACITY_FIELDS}
            out.append({**kept, "scope": scope, "id": sid, "name": name})
    return out


def _campus(conn: Conn, campus_id: str) -> Row | None:
    return campus_row(conn, campus_id)


@builder("dc_tab:buildings", keys=campus_ids)
def build_buildings(conn: Conn, campus_id: str) -> list[Doc] | None:
    return building_docs(conn, campus_id) if _campus(conn, campus_id) else None


@builder("dc_tab:records", keys=campus_ids)
def build_records(conn: Conn, campus_id: str) -> list[Doc] | None:
    return campus_records(conn, campus_id) if _campus(conn, campus_id) else None


@builder("dc_tab:history", keys=campus_ids)
def build_history(conn: Conn, campus_id: str) -> list[Doc] | None:
    return campus_history(conn, campus_id) if _campus(conn, campus_id) else None


@builder("dc_tab:capacity", keys=campus_ids)
def build_capacity(conn: Conn, campus_id: str) -> list[Doc] | None:
    campus = _campus(conn, campus_id)
    return capacity_estimates(conn, campus) if campus else None
