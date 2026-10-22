"""The pieces of the `home` document (views/home.py): this week's changes and what is coming up.

Each piece names the watchlist entries it belongs to (`watch`: "site:<slug>" / "instrument:<id>"), so the
API can keep the org's names without recomputing anything (apps/api/src/lib/home-doc.ts). A site's
pieces also belong to the instruments that need that site finished, since a watched loan shows its
site's status and cushion (WatchItem, spec 11).

Changes (spec 01 Rules): link status changes (the status model now vs seven days earlier), cushion
changes (the cushion model now vs seven days earlier), Marden price moves of a point or more (the API
applies the user's threshold) and filings dated this week. Coming up: scheduled steps (events of kind
'catalyst' dated after the build day) and the next public fund marks (model 'mark_gap').
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models.status_labels import CHAIN_CLAUSE, LINK_CLAUSE, link_label
from marden.readmodels.context import day_end
from marden.readmodels.views.instrument_inputs import day, num
from marden.readmodels.views.instrument_text import short_name
from marden.readmodels.views.monitor_common import Doc, is_iso_date, method_of, plain_sourced, when_label
from marden.readmodels.views.sites_common import event_evidence

ROLE_LABELS = {"end_user": "End-user", "tenant": "Tenant", "developer": "Developer", "lenders": "Lenders"}
LINK_RANK = {"red": 3, "amber": 2, "green": 1, "none": 0}
QUIET = frozenset({"none", "green"})  # a party named (none -> green) is not a status change
PRICE_MOVE_PTS = 1.0  # the lowest user threshold (AlertSettings.priceThresholdPts 1 | 2 | 3)
LENDER_RED_BELOW = 95.0
MIN_HEADLINE_WORDS = 3
LONG_TITLE = 80  # characters a Coming up line holds


def site_watch(slug: str, instrument_ids: Sequence[str]) -> list[str]:
    return [f"site:{slug}", *(f"instrument:{i}" for i in instrument_ids)]


def _links(out: Row | None) -> dict[str, dict[str, Any]]:
    chain = out["output"].get("chain") if out is not None and isinstance(out["output"], dict) else None
    return {link["role"]: link for link in chain or [] if isinstance(link, dict) and link.get("role") in ROLE_LABELS}


def _status(out: Row | None) -> str | None:
    status = out["output"].get("status") if out is not None and isinstance(out["output"], dict) else None
    return status if status in ("Broken", "Strained", "Holding", "No build") else None


def _link_record(out: Row, link: dict[str, Any]) -> tuple[Doc, Doc]:
    """The new link status as Sourced<string> carrying the statement that set it, and that statement as
    Evidence (the status model's reason when no statement tripped a rule)."""
    fact = plain_sourced(link.get("fact"), "string")
    if fact is not None and isinstance(fact["value"], str) and fact["value"]:
        to = {**{k: v for k, v in fact.items() if k != "value"}, "value": link["status"]}
        record = {"date": fact["asOf"], "text": fact["value"], "tag": fact["tag"],
                  "sourceName": fact.get("sourceName") or "", "sourceUrl": fact.get("sourceUrl") or ""}
        return to, record
    when = out["as_of"].isoformat()
    to = {"value": link["status"], "tag": "M", "asOf": when, "sourceName": "Marden status model",
          "methodVersion": method_of(out)}
    fallback = f"{ROLE_LABELS[link['role']]} link: {link_label(link['status'])}"
    record = {"date": when, "text": str(link.get("reason") or fallback),
              "tag": "M", "sourceName": "Marden status model", "sourceUrl": ""}
    return to, record


def status_changes(site: Doc, watch: list[str], now: Row | None, before: Row | None) -> tuple[Doc | None, list[Doc]]:
    """This week's status change line for the headline ("Abilene now on watch") and one HomeChange per
    link whose status moved, with its severity. Nothing without both a current and a week-old output."""
    if now is None or before is None or now["id"] == before["id"]:
        return None, []
    old, new = _links(before), _links(now)
    changes = []
    for role, link in new.items():
        prev = (old.get(role) or {}).get("status")
        cur = link.get("status")
        if cur not in LINK_RANK or prev not in LINK_RANK or prev == cur or {prev, cur} <= QUIET:
            continue
        to, record = _link_record(now, link)
        changes.append({"severity": LINK_RANK[cur], "watch": watch, "kind": "site", "refId": site["slug"],
                        "name": site["name"], "type": "status", "label": f"{ROLE_LABELS[role]} link", "from": prev,
                        "to": to, "status": site["status"], "record": record})
    status_now, status_before = _status(now), _status(before)
    name = short_name(site["name"]) or site["name"]  # "Project Jupiter", not its long name with aliases
    if status_now and status_before and status_now != status_before:
        line = f"{name} now {CHAIN_CLAUSE.get(status_now, status_now)}"
    elif changes:
        top = max(changes, key=lambda c: c["severity"])
        line = f"{name} {top['label'].lower()} now {LINK_CLAUSE.get(top['to']['value'], top['to']['value'])}"
    else:
        return None, []
    return {"watch": watch, "name": site["name"], "line": line, "at": now["as_of"].isoformat()}, changes


def _cushion_value(out: Row | None) -> float | None:
    """The cushion a site showed with this output (as views/sites.projection_of: a projection with a P50)."""
    projection = out["output"].get("projection") if out is not None and isinstance(out["output"], dict) else None
    months = projection.get("cushionMonths") if isinstance(projection, dict) and projection.get("p50") else None
    return num(months.get("value")) if isinstance(months, dict) else None


def cushion_change(site: Doc, watch: list[str], now: Row | None, before: Row | None,
                   record: Doc | None) -> Doc | None:
    """A HomeChange when the cushion moved since the cushion model's output of seven days earlier."""
    to = site.get("cushionMonths")
    if now is None or before is None or not isinstance(to, dict) or num(to.get("value")) is None or record is None:
        return None
    prev = _cushion_value(before)
    if prev is None or prev == to["value"]:
        return None
    months = float(to["value"])
    severity = 3 if months < 0 else 2 if months < 3 else 2 if months < prev else 1
    return {"severity": severity, "watch": watch, "kind": "site", "refId": site["slug"], "name": site["name"],
            "type": "cushion", "from": prev, "to": to, "status": site["status"], "record": record}


def price_change(row: Doc, watch: list[str]) -> Doc | None:
    """A HomeChange when the Marden price moved a point or more over the week (model 'price')."""
    price, change = row.get("mardenPrice") or {}, row.get("change1w") or {}
    now, moved = num(price.get("value")), num(change.get("value"))
    if now is None or moved is None or abs(moved) < PRICE_MOVE_PTS:
        return None
    before = now - moved
    crossed = before >= LENDER_RED_BELOW > now  # the lenders link's red line (00-global: price below 95)
    severity = 3 if crossed or moved <= -3 else 2 if moved <= -2 else 1
    record = {"date": change["asOf"], "text": f"Marden price {before:.1f} to {now:.1f} over the week",
              "tag": change["tag"], "sourceName": change.get("sourceName") or price.get("sourceName") or "Marden price",
              "sourceUrl": change.get("sourceUrl") or ""}
    return {"severity": severity, "watch": watch, "kind": "instrument", "refId": row["id"], "name": row["name"],
            "type": "price", "from": round(before, 4), "to": price, "status": row.get("chainStatus"),
            "record": record}


# ---------------------------------------------------------------- events (filings, scheduled steps)
def week_events(conn: Conn, start: date) -> list[Row]:
    """Filings dated this week and scheduled steps after the build day on sites and instruments, as
    Marden knew them by the build day, oldest first."""
    return conn.execute(
        """SELECT * FROM event WHERE subject_type IN ('site', 'instrument') AND status = 'published'
             AND learned_at < %(day_end)s
             AND ((kind = 'filing' AND event_date BETWEEN %(start)s AND %(day)s)
                  OR (kind = 'catalyst' AND event_date > %(day)s))
           ORDER BY event_date, id""",
        {"day_end": day_end(), "start": start, "day": day()},
    ).fetchall()


def _detail(title: str) -> tuple[str, str]:
    """'Motions to dismiss heard: Hearing on motions ...' -> (headline, detail). The loaders write one
    step twice (the catalyst list and the memo's next step) with the same detail after the colon."""
    head, sep, rest = title.partition(": ")
    return (head, rest) if sep else (title, title)


def merge_events(rows: Sequence[Row], subjects: dict[tuple[str, str], Doc]) -> list[Doc]:
    """One entry per subject and distinct filing or scheduled step. The loaders write one step twice
    (same detail, two headlines) and copy it to each instrument; every copy of a step gets the same
    `heads`, so the API's de-duplication by date and text keeps one per watched name."""
    groups: dict[tuple[Any, ...], dict[tuple[str, str], Doc]] = {}
    heads: dict[tuple[Any, ...], list[str]] = {}
    for ev in rows:
        subject = subjects.get((ev["subject_type"], ev["subject_id"]))
        if subject is None:
            continue
        head, detail = _detail(ev["title"])
        key = (ev["kind"], ev["event_date"], detail if ev["kind"] == "catalyst" else (ev["title"], ev["source_url"]))
        heads.setdefault(key, []).append(head)
        groups.setdefault(key, {}).setdefault((subject["kind"], subject["id"]), {"event": ev, "subject": subject,
                                                                                "watch": subject["watch"]})
    return [{**entry, "heads": heads[key]} for key, by_subject in groups.items() for entry in by_subject.values()]


def filing_change(entry: Doc) -> Doc:
    ev, subject = entry["event"], entry["subject"]
    record = event_evidence(ev)
    to = {"value": ev["title"], "tag": ev["tag"], "asOf": record["date"], "sourceName": ev["source_name"]}
    if ev.get("source_url"):
        to["sourceUrl"] = ev["source_url"]
    severity = 3 if ev.get("severity") == "red" else 2 if ev.get("material") or ev.get("severity") == "amber" else 1
    label = _detail(ev["title"])[0] if ": " in ev["title"] else ev["source_name"]  # the form: '8-K', '10-Q'
    return {"severity": severity, "watch": entry["watch"], "kind": subject["kind"], "refId": subject["id"],
            "name": subject["name"], "type": "filing", "label": label, "from": None, "to": to,
            "status": subject["status"], "record": record}


def coming_up_event(entry: Doc) -> Doc:
    ev, subject = entry["event"], entry["subject"]
    heads = sorted(set(entry["heads"]), key=lambda h: (len(h), h))
    # A step written twice shows its short headline. One written once shows its title, or for a long title
    # its headline when that names the step ("CoreWeave Q3 10-Q", not "Matures").
    short = len(ev["title"]) > LONG_TITLE and len(heads[0].split()) >= MIN_HEADLINE_WORDS
    what = heads[0] if len(heads) > 1 or short else ev["title"]
    return {"watch": entry["watch"], "date": ev["event_date"].isoformat(), "when": when_label(ev["event_date"], day()),
            "what": what, "refUrl": subject["href"]}


def fund_marks_due(row: Doc, gap_out: Row | None, watch: list[str]) -> Doc | None:
    """The next public fund marks of an instrument funds hold (model 'mark_gap': N-PORT 60 days after
    the period, BDC schedules on filing), when after the build day."""
    body = gap_out["output"] if gap_out is not None and isinstance(gap_out["output"], dict) else {}
    when = body.get("nextMarksPublic")
    if not is_iso_date(when) or when <= day().isoformat():
        return None
    marks = body.get("fundMarks") if isinstance(body.get("fundMarks"), dict) else {}
    filing = marks.get("filing") if isinstance(marks.get("filing"), str) else "fund"
    return {"watch": watch, "date": when, "when": when_label(date.fromisoformat(when), day()),
            "what": f"{row['name']}: next {filing} marks public", "refUrl": f"/instruments/{row['id']}"}
