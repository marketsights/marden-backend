"""Site detail tabs built from records: `site_tab:evidence`, `site_tab:opposition`, `site_tab:events`
(spec 04; payloads SiteEvidenceTab, SiteOppositionTab, SiteEventsTab of the app's site-tab types,
apps/api/src/contracts/site-tabs.ts).

- Evidence: every published past-dated event on the site (dockets, county agendas, permits, filings,
  press, capacity auctions), the dated evidence items of the case research (risk_evidence facts)
  and the monthly Sentinel-2 images, newest first.
- Opposition: opposition events and the court proceedings Marden watches for the site (docket
  entities whose linked_sites names it); next hearing from those proceedings' next_dates.
  'Latest ruling' and 'impact if voided' (M) stay n/d until a ruling parser / model exists.
- Material events: what changed a status or the cushion, and the records behind them, newest first:
  * each change of a chain link's status between two `status` runs (link, rule codes from the
    rule text, 'No flags → Warning', and the site's own change on that run, 'site Watch → At risk'),
    dated by the record the link cites when it falls between the two runs, else by the run;
  * each move of the `cushion` model that is material (M1: 3+ months later; M2: below zero), with
    the record of the milestone that moved it most ('Cushion +4 → -1 months', with a true minus);
  * events approved as material or judged material by the archive (materiality_note) and events
    the collectors flagged red or amber, one row per story: a headline syndicated by several
    outlets is one row, and press reports with the same rule within 30 days of the first are that
    first report's later coverage (`reports`). The archive's rule label is shown as words
    ('sev 3: Force majeure' -> 'Force majeure') and names the chain link it is about where the
    status rules tie it to one (force majeure: tenant).
  Lanes come from the `status` model's daily outputs since Jan 1 (empty until it runs).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from marden.core.db import Conn, Row
from marden.models.status_labels import chain_label, link_label
from marden.readmodels.context import business_day, known_fact_rows, known_facts
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
    output_field,
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
    # a step still to come (a hearing set for next week) is listed too: its alert links to this tab
    upcoming = site_events(conn, slug, kinds=["opposition"], future=True)
    for ev in [*site_events(conn, slug, kinds=["opposition"]), *upcoming]:
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


def next_hearing(proceedings: list[tuple[str, dict[str, Row]]], upcoming: list[Row] | None = None) -> Doc:
    """The next hearing as a KPI: the date as the value (ISO day, tagged by the proceeding's
    next_dates fact, or by the published opposition event that sets it: a hearing on motions to dismiss a
    permit) and what is heard as its one-line note."""
    best: tuple[str, str, Row] | None = None
    for _, facts in proceedings:
        row = facts.get("next_dates")
        for item in (row["value"] if row and isinstance(row["value"], list) else []):
            when = item.get("d") if isinstance(item, dict) else None
            if isinstance(when, str) and when >= day().isoformat() and (best is None or when < best[0]):
                best = (when, str(item.get("what") or ""), row)
    for ev in upcoming or []:
        when = ev["event_date"].isoformat()
        if when >= day().isoformat() and (best is None or when < best[0]):
            best = (when, str(ev["title"] or ""), {
                "tag": ev["tag"], "valid_from": business_day(ev["learned_at"]), "source_name": ev["source_name"],
                "source_url": ev.get("source_url")})
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
        "nextHearing": next_hearing(hearings, site_events(conn, slug, kinds=["opposition"], future=True)),
        "ifVoided": {"value": nd("M")},
        "items": opposition_items(conn, slug, courts),
    }


# ---------------------------------------------------------------- material events
LINK_LABEL = {"end_user": "End user", "tenant": "Tenant", "developer": "Developer", "lenders": "Lenders"}
STATUS_WORD = {"red": "red", "amber": "amber", "green": "green", "none": "no data"}
RULE_LINK = {"force majeure": "Tenant", "tenant pull-out": "Tenant", "court case": "Opposition",
             "moratorium": "Opposition"}
RULE_CODE = re.compile(r"(?:^|[.;]\s+)([EDTLM][12])\b")
COVERAGE_DAYS = 30
MINUS = "\u2212"


def story_key(title: str) -> str:
    """One key per story: the same headline syndicated by several outlets ('… - Yahoo Finance',
    '… - TheStreet') is one material event, not five."""
    text = re.sub(r"\s+-\s+[^-]{2,40}$", "", title or "")
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("\u2019", "'")).strip()


def rule_words(rule: str | None) -> str | None:
    """The archive's rule label as words: 'sev 3: Force majeure' -> 'Force majeure'."""
    if not rule:
        return None
    text = re.sub(r"^sev\s*\d+\s*:\s*", "", str(rule)).strip()
    return text[:1].upper() + text[1:] if text else None


def signed(months: float) -> str:
    n = round(months)
    return f"+{n}" if n > 0 else f"{MINUS}{abs(n)}" if n < 0 else "0"


def _links(output: Any) -> dict[str, Doc]:
    chain = output.get("chain") if isinstance(output, dict) else None
    return {link["role"]: link for link in chain or [] if isinstance(link, dict) and link.get("role") in LINK_LABEL}


def _codes(reason: str) -> str | None:
    codes = list(dict.fromkeys(RULE_CODE.findall(reason or "")))
    return ", ".join(codes) or None


def status_changes(conn: Conn, slug: str, start: date) -> list[Doc]:
    """One row per change of a link's status between consecutive `status` runs since `start`."""
    history = output_history(conn, "status", "site", slug, start)
    out: list[Doc] = []
    for prev, cur in pairwise(history):
        before, after = _links(prev["output"]), _links(cur["output"])
        rows: list[Doc] = []
        for role, link in after.items():
            was = (before.get(role) or {}).get("status")
            now = link.get("status")
            if was not in STATUS_WORD or now not in STATUS_WORD or was == now:
                continue
            fact = link.get("fact") if isinstance(link.get("fact"), dict) else None
            reason = str(link.get("reason") or "")
            when = cur["as_of"].isoformat()
            if fact and fact.get("value") and isinstance(fact.get("asOf"), str) \
                    and prev["as_of"].isoformat() < fact["asOf"][:10] <= when:
                when = fact["asOf"][:10]
            text = _one_line(str(fact["value"]), 120) if fact and fact.get("value") else _one_line(
                re.sub(r"^[EDTLM][12]\s+", "", reason.split(". ")[0]), 120)
            rows.append({
                "date": when, "text": text or f"{LINK_LABEL[role]}: {link_label(now)}",
                "tag": fact.get("tag", "R") if fact else "M",
                "sourceName": (fact.get("sourceName") or "") if fact else "Marden status model",
                "sourceUrl": (fact.get("sourceUrl") or "") if fact else "",
                "severity": now, "link": LINK_LABEL[role], "ruleHit": _codes(reason),
                "changed": f"{link_label(was)} \u2192 {link_label(now)}",
            })
        chain_was, chain_now = output_field(prev, "status"), output_field(cur, "status")
        if rows and isinstance(chain_was, str) and isinstance(chain_now, str) and chain_was != chain_now:
            worst = min(rows, key=lambda r: ("red", "amber", "green", "none").index(r["severity"]))
            worst["changed"] += f"; site {chain_label(chain_was)} \u2192 {chain_label(chain_now)}"
        out.extend(rows)
    return out


def _cushion(output: Any) -> float | None:
    months = ((output or {}).get("projection") or {}).get("cushionMonths") if isinstance(output, dict) else None
    value = months.get("value") if isinstance(months, dict) else None
    return value if isinstance(value, int | float) else None


def _what_moved(output: dict[str, Any]) -> Doc | None:
    """The record of the milestone that delays the projection most (the cushion model's basis)."""
    delays = ((output.get("basis") or {}).get("delayMonths") or {}) if isinstance(output, dict) else {}
    key = max(delays, key=delays.get) if isinstance(delays, dict) and delays else None
    for ms in output.get("milestones") or []:
        ev = ms.get("latestEvidence") if isinstance(ms, dict) else None
        if ms.get("key") == key and isinstance(ev, dict) and ev.get("text"):
            return {**ev, "text": _one_line(f"{ms.get('label')}: {ev['text']}", 120)}
    return None


def cushion_changes(conn: Conn, slug: str, start: date) -> list[Doc]:
    """Material moves of the cushion between consecutive `cushion` runs: 3+ months later (M1) or
    below zero (M2)."""
    out: list[Doc] = []
    previous: float | None = None
    for row in output_history(conn, "cushion", "site", slug, start):
        months = _cushion(row["output"])
        if months is None:
            continue
        if previous is not None and months != previous:
            rules = [code for code, hit in (("M1", months - previous <= -3), ("M2", months < 0 <= previous)) if hit]
            if rules:
                ev = _what_moved(row["output"]) or {
                    "text": str(output_field(row, "driver") or "Projected completion moved"), "tag": "M",
                    "sourceName": "Marden cushion model", "sourceUrl": ""}
                out.append({
                    "date": row["as_of"].isoformat(), "text": ev["text"], "tag": ev.get("tag") or "M",
                    "sourceName": ev.get("sourceName") or "", "sourceUrl": ev.get("sourceUrl") or "",
                    "severity": "red" if months < 0 else "amber", "link": "Schedule", "ruleHit": ", ".join(rules),
                    "changed": f"Cushion {signed(previous)} \u2192 {signed(months)} months",
                })
        previous = months
    return out


def record_events(conn: Conn, slug: str) -> list[Doc]:
    """Material and red / amber records, one row per story, oldest report first within a story."""
    events = site_events(conn, slug)
    notes = materiality_notes(conn, slug)
    material = {ev["id"]: note for ev, note in material_events(conn, slug, events)}
    rows: list[Doc] = []
    stories: dict[str, Doc] = {}
    coverage: dict[str, Doc] = {}
    for ev in reversed(events):  # oldest first: the first report of a story stands for it
        if ev["id"] not in material and severity(ev) not in ("red", "amber"):
            continue
        note = material.get(ev["id"]) or notes.get((ev["event_date"].isoformat(), ev["title"]))
        rule = rule_words(ev.get("rule_code") or (note or {}).get("rule"))
        key = story_key(ev["title"])
        first = stories.get(key) if key else None
        if first is None and ev["kind"] == "news" and rule:
            lead = coverage.get(rule)
            window = timedelta(days=COVERAGE_DAYS)
            if lead is not None and date.fromisoformat(lead["date"]) + window >= ev["event_date"]:
                first = lead
        if first is not None:
            first["reports"] = first.get("reports", 0) + 1
            continue
        row = {**event_evidence(ev), "severity": severity(ev),
               "link": RULE_LINK.get((rule or "").lower(), AREA.get(ev["kind"], "Record")),
               "ruleHit": rule, "changed": None}
        rows.append(row)
        if key:
            stories[key] = row
        if ev["kind"] == "news" and rule:
            coverage[rule] = row
    return rows


def site_event_log(conn: Conn, slug: str) -> list[Doc]:
    """Status changes, material cushion moves and material records since Jan 1 (records: all
    past-dated), newest first; on one day, what changed a status before the records."""
    start = date(day().year, 1, 1)
    changes = status_changes(conn, slug, start) + cushion_changes(conn, slug, start)
    rows = changes + record_events(conn, slug)
    order = {id(r): i for i, r in enumerate(rows)}
    rows.sort(key=lambda r: (r["date"], -order[id(r)]), reverse=True)
    return rows


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
        return (STATUS_TONE.get(status), chain_label(status)) if status in STATUS_TONE else (None, "")
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
