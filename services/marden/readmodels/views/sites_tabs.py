"""Site detail tabs built from records: `site_tab:evidence`, `site_tab:opposition`, `site_tab:events`
(spec 04; payloads SiteEvidenceTab, SiteOppositionTab, SiteEventsTab of the app's site-tab types,
apps/api/src/contracts/site-tabs.ts).

- Evidence: every published past-dated event on the site (dockets, county agendas, permits, filings,
  press, capacity auctions), the dated evidence items of the case research (risk_evidence facts)
  and the monthly Sentinel-2 images, newest first.
- Opposition: opposition events and the court proceedings Marden watches for the site (docket
  entities whose linked_sites names it); next hearing from those proceedings' next_dates.
  'Latest ruling' and 'impact if voided' (M) stay n/d until a ruling parser / model exists.
- Material events: events approved as material or judged material by the archive
  (materiality_note), and events the collectors flagged red or amber. Lanes come from the `status`
  model's daily outputs since Jan 1 (empty until it runs); 'what it changed' is n/d until the
  status and cushion models record it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.sites_common import (
    ROLES,
    Doc,
    day,
    event_evidence,
    fact_evidence,
    material_events,
    materiality_notes,
    nd,
    output_history,
    severity,
    site_events,
    site_slugs,
    subjects_listing,
    weakest_tag,
)
from marden.readmodels.views.sites_links import imagery, last_visible_change

COUNTY_WORDS = ("county", "parish", "city", "planning", "zoning", "commission", "board", "council", "township")
OPPOSITION_TYPES = (("moratori", "Moratorium"), ("lawsuit", "Lawsuit"), ("suit", "Lawsuit"), ("petition", "Petition"),
                    ("ruling", "Ruling"), ("judge", "Ruling"), ("vote", "Vote"), ("hearing", "Hearing"))
CLOSED = ("closed", "dismissed", "resolved", "withdrawn", "decided")
AREA = {"docket": "Permits and power", "permit": "Permits and power", "news": "Press", "opposition": "Opposition",
        "filing": "Filing", "capacity_auction": "Power", "catalyst": "Schedule", "interconnection": "Power"}
STATUS_TONE = {"Broken": "red", "Strained": "amber", "Holding": "green", "No build": "none"}


def record_type(ev: Row) -> str:
    kind, name = ev["kind"], (ev.get("source_name") or "").lower()
    if kind == "docket":
        if "court" in name:
            return "Court"
        return "County" if any(w in name for w in COUNTY_WORDS) else "Docket"
    return {"permit": "Permit", "news": "Press", "filing": "Filing", "opposition": "Opposition",
            "capacity_auction": "Market", "interconnection": "Docket"}.get(kind, kind.replace("_", " ").capitalize())


# ---------------------------------------------------------------- evidence
def evidence_records(conn: Conn, slug: str) -> list[Doc]:
    records = [{**event_evidence(ev), "type": record_type(ev)}
               for ev in site_events(conn, slug) if ev["kind"] != "catalyst"]
    seen = {(r["date"], r["sourceUrl"]) for r in records}
    for row in known_fact_rows(conn, "site", slug, ["risk_evidence"])["risk_evidence"]:
        v = row["value"] if isinstance(row["value"], dict) else {}
        when, url, text = v.get("date"), v.get("url") or row.get("source_url") or "", v.get("headline")
        if not (isinstance(when, str) and text) or when > day().isoformat() or (when, url) in seen:
            continue
        seen.add((when, url))
        rec = fact_evidence(row, text, when)
        rec["sourceUrl"] = url
        records.append({**rec, "type": "Filing" if row["tag"] == "F" else "Press"})
    for row in known_fact_rows(conn, "site", slug, ["imagery"])["imagery"]:
        v = row["value"] if isinstance(row["value"], dict) else {}
        if v.get("url"):
            rec = fact_evidence(row, v.get("caption") or f"Sentinel-2 image {v.get('month')}")
            rec["sourceUrl"] = v["url"]
            records.append({**rec, "type": "Imagery"})
    records.sort(key=lambda r: (r["date"], r["type"], r["text"]), reverse=True)
    return records


def evidence_tab(conn: Conn, slug: str) -> Doc:
    doc: Doc = {"records": evidence_records(conn, slug), "imagery": imagery(conn, slug)}
    if (finding := last_visible_change(conn, slug)) is not None:
        doc["imageryFinding"] = finding
    return doc


# ---------------------------------------------------------------- opposition
def site_proceedings(conn: Conn, slug: str, kinds: tuple[str, ...]) -> list[tuple[str, dict[str, Row]]]:
    """Docket entities (court, county board, zoning ...) whose linked_sites names the site."""
    out = []
    for entity_id in subjects_listing(conn, "entity", "linked_sites", slug):
        facts = known_facts(conn, "entity", entity_id,
                            ["linked_sites", "proceeding", "proceeding_kind", "status", "list_url", "next_dates"])
        linked = facts.get("linked_sites")
        kind = (facts.get("proceeding_kind") or {}).get("value")
        if linked and isinstance(linked["value"], list) and slug in linked["value"] and kind in kinds:
            out.append((entity_id, facts))
    return out


def opposition_type(title: str) -> str:
    low = title.lower()
    return next((label for word, label in OPPOSITION_TYPES if word in low), "Press")


def opposition_items(conn: Conn, slug: str, courts: list[tuple[str, dict[str, Row]]]) -> list[Doc]:
    """Opposition events and watched proceedings, newest first. An event's court or body is its source
    only when the body filed it (F); a press report (R) names an outlet, not where the case sits, so
    'where' is n/d. No event record states the case's status: n/d, never the tag restated as a word."""
    items = []
    for ev in site_events(conn, slug, kinds=["opposition"]):
        tone = {"red": "red", "amber": "amber", "green": "green"}.get(severity(ev), "grey")
        items.append({"date": ev["event_date"].isoformat(), "type": opposition_type(ev["title"]), "text": ev["title"],
                      "where": (ev["source_name"] or "n/d") if ev["tag"] == "F" else "n/d", "status": "n/d",
                      "statusTone": tone, "evidence": event_evidence(ev)})
    for _, facts in courts:
        name = facts.get("proceeding")
        if not name:
            continue
        status = facts.get("status")
        status_text = str(status["value"]) if status else "n/d"
        ev = fact_evidence(name, str(name["value"]))
        if facts.get("list_url"):
            ev["sourceUrl"] = str(facts["list_url"]["value"])
        where = str(name["value"]).split("(", 1)[0].strip()
        items.append({"date": name["valid_from"].isoformat(), "type": "Court case", "text": str(name["value"]),
                      "where": where, "status": status_text, "statusTone": "grey", "evidence": ev})
    items.sort(key=lambda i: i["date"], reverse=True)
    return items


def _count_value(rows: list[Row], count: int) -> Doc:
    names = sorted({r["source_name"] for r in rows if r.get("source_name")})
    return {"value": count, "tag": weakest_tag(r["tag"] for r in rows),
            "asOf": max(r["valid_from"] for r in rows).isoformat(), "sourceName": " + ".join(names)}


def next_hearing(proceedings: list[tuple[str, dict[str, Row]]]) -> Doc:
    """The next hearing as a KPI: the date as the value (ISO day, tagged by the proceeding's
    next_dates fact) and what is heard as its one-line note."""
    best: tuple[str, str, Row] | None = None
    for _, facts in proceedings:
        row = facts.get("next_dates")
        for item in (row["value"] if row and isinstance(row["value"], list) else []):
            when = item.get("d") if isinstance(item, dict) else None
            if isinstance(when, str) and when >= day().isoformat() and (best is None or when < best[0]):
                best = (when, str(item.get("what") or ""), row)
    if best is None:
        return {"value": nd("F")}
    when, what, row = best
    out = {"value": when, "tag": row["tag"], "asOf": row["valid_from"].isoformat(),
           "sourceName": row.get("source_name") or ""}
    if row.get("source_url"):
        out["sourceUrl"] = row["source_url"]
    kpi: Doc = {"value": out}
    if note := _one_line(what):
        kpi["note"] = note
    return kpi


def _one_line(text: str, limit: int = 90) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0].rstrip(" ,;:([")
    return f"{cut}…"


def opposition_tab(conn: Conn, slug: str) -> Doc:
    courts = site_proceedings(conn, slug, ("court",))
    hearings = site_proceedings(conn, slug, ("court", "county_board", "zoning", "city_council"))
    open_rows = [f["proceeding"] for _, f in courts if f.get("proceeding")
                 and not any(w in str((f.get("status") or {}).get("value") or "").lower() for w in CLOSED)]
    court_rows = [f["proceeding"] for _, f in courts if f.get("proceeding")]
    return {
        "openCases": {"value": _count_value(court_rows, len(open_rows)) if court_rows else nd("F"),
                      "note": "court proceedings Marden watches for this site"},
        "latestRuling": {"value": nd("F")},
        "nextHearing": next_hearing(hearings),
        "ifVoided": {"value": nd("M")},
        "items": opposition_items(conn, slug, courts),
    }


# ---------------------------------------------------------------- material events
def site_event_log(conn: Conn, slug: str) -> list[Doc]:
    events = site_events(conn, slug)
    notes = materiality_notes(conn, slug)
    material = {ev["id"]: note for ev, note in material_events(conn, slug, events)}
    out = []
    for ev in events:
        if ev["id"] not in material and severity(ev) not in ("red", "amber"):
            continue
        note = material.get(ev["id"]) or notes.get((ev["event_date"].isoformat(), ev["title"]))
        rule = ev.get("rule_code") or (note or {}).get("rule")
        out.append({**event_evidence(ev), "severity": severity(ev), "link": AREA.get(ev["kind"], "Record"),
                    "ruleHit": rule, "changed": None})
    return out


def lanes(conn: Conn, slug: str, start: date) -> list[Doc]:
    history = output_history(conn, "status", "site", slug, start)
    out = []
    for role in (*ROLES, "chain"):
        segments: list[Doc] = []
        for i, row in enumerate(history):
            end = history[i + 1]["as_of"] if i + 1 < len(history) else day()
            status, reason = _lane_status(row["output"], role)
            if status is None:
                continue
            if segments and segments[-1]["status"] == status and segments[-1]["reason"] == reason:
                segments[-1]["to"] = end.isoformat()
            else:
                segments.append({"from": row["as_of"].isoformat(), "to": end.isoformat(), "status": status,
                                 "reason": reason})
        out.append({"role": role, "segments": segments})
    return out


def _lane_status(output: Any, role: str) -> tuple[str | None, str]:
    if not isinstance(output, dict):
        return None, ""
    if role == "chain":
        status = output.get("status")
        return (STATUS_TONE.get(status), str(status)) if status in STATUS_TONE else (None, "")
    for link in output.get("chain") or []:
        if isinstance(link, dict) and link.get("role") == role and link.get("status") in STATUS_TONE.values():
            return link["status"], str(link.get("reason") or "")
    return None, ""


def events_tab(conn: Conn, slug: str) -> Doc:
    start = date(day().year, 1, 1)
    return {"period": {"start": start.isoformat(), "end": day().isoformat()}, "lanes": lanes(conn, slug, start),
            "events": site_event_log(conn, slug)}


@builder("site_tab:evidence", keys=site_slugs)
def build_evidence(conn: Conn, slug: str) -> Doc | None:
    return evidence_tab(conn, slug) if _exists(conn, slug) else None


@builder("site_tab:opposition", keys=site_slugs)
def build_opposition(conn: Conn, slug: str) -> Doc | None:
    return opposition_tab(conn, slug) if _exists(conn, slug) else None


@builder("site_tab:events", keys=site_slugs)
def build_events(conn: Conn, slug: str) -> Doc | None:
    return events_tab(conn, slug) if _exists(conn, slug) else None


def _exists(conn: Conn, slug: str) -> bool:
    return conn.execute("SELECT 1 FROM site WHERE slug = %s", (slug,)).fetchone() is not None
