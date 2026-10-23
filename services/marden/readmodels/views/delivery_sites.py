"""One site's delivery history for the delivery record (readmodels/views/delivery.py, docs/methods/
delivery-record-v0.1.md): each dated milestone, building and in-service date judged against the first target a
source stated for it, and each time the schedule was pushed.

Items (DeliveryItem), every date with the record that states it:
- milestones: the Schedule tab's rows (`site_schedule` read model), `originalTarget` (first stated plan) against
  `actual` when done, else the current `target`; an open milestone whose target month has passed counts as late
  by at least the months since that target;
- buildings: the state registration's estimated completion (`expected_completion`) of each building on the
  site's approved campuses against its completion (`construction_end`, or `construction_complete` seen by the
  target date); a building still under construction after its date is late by at least the months since;
- in-service date: the status model's D1 rule (the site's stated in-service date against the first one stated).
Slip = later month minus the first stated month, in whole months (window ends compared: 'Q4 2026' is Dec 2026).

Pushes (DeliveryPush): a milestone marked slipped on the Schedule tab, a filed or reported schedule change that
says the date moved later (`schedule_change`), and each later move of the Marden projected completion (the
`event_impact` model's completion changes, tag M).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.views.instrument_inputs import day
from marden.readmodels.views.sites_common import latest_output

Doc = dict[str, Any]

PUSH_WORDS = re.compile(
    r"\b(delay|delayed|pushed|slip|slipped|later than|postpon|deferred|extended to|moved to|"
    r"behind schedule)\w*",
    re.IGNORECASE,
)
D1 = re.compile(
    r"target moved (-?\d+) months?, (\d{4}-\d{2}) -> (\d{4}-\d{2}): '([^']*)' \(([^,]+), (\d{4}-\d{2}-\d{2})\)"
    r" -> '([^']*)' \(([^,]+), (\d{4}-\d{2}-\d{2})\)"
)
TAG_RANK = {"F": 0, "R": 1, "M": 2}


def month_no(ym: str | None) -> int | None:
    """'2026-09' (or a day '2026-09-15') -> a month count; None when not a month."""
    m = re.match(r"^(\d{4})-(\d{2})", ym or "")
    return int(m.group(1)) * 12 + int(m.group(2)) - 1 if m else None


def ym_of(n: int) -> str:
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def claim(raw: Any, month: str | None = None) -> Doc | None:
    """A dated statement: the month it gives, its words and the record behind it (tag, source, as-of)."""
    if not isinstance(raw, dict):
        return None
    when = month or raw.get("to") or raw.get("from")
    if month_no(when) is None:
        return None
    out: Doc = {
        "month": str(when)[:7],
        "text": str(raw.get("text") or when),
        "tag": raw.get("tag"),
        "sourceName": raw.get("sourceName"),
        "asOf": raw.get("asOf"),
    }
    if raw.get("sourceUrl"):
        out["sourceUrl"] = raw["sourceUrl"]
    return out


def fact_claim(row: Row | None, month: str, text: str) -> Doc:
    out: Doc = {"month": month, "text": text, "tag": None, "sourceName": None, "asOf": None}
    if row is not None:
        out.update(
            {"tag": str(row["tag"]).strip(), "sourceName": row["source_name"], "asOf": row["valid_from"].isoformat()}
        )
        if row.get("source_url"):
            out["sourceUrl"] = row["source_url"]
    return out


def judged(first: Doc | None, later: Doc | None, *, done: bool, now: int) -> tuple[str, int | None, bool]:
    """(outcome, slip months, at least): 'on_time' / 'late' when done ('done_unjudged' without both dates),
    'open' / 'open_late' (behind its first target) when not."""
    a, b = month_no((first or {}).get("month")), month_no((later or {}).get("month"))
    if done:
        if a is None or b is None:
            return "done_unjudged", None, False
        return ("on_time" if b <= a else "late"), b - a, False
    if b is not None and b < now:  # the current target has passed and it is not done
        return "open_late", (now - a if a is not None else None), True
    if a is None or b is None:
        return "open", None, False
    return ("open_late" if b > a else "open"), b - a, False


def item(site: Doc, kind: str, what: str, first: Doc | None, later: Doc | None, *, done: bool, tab: str) -> Doc:
    now = month_no(day().isoformat()) or 0
    outcome, slip, at_least = judged(first, later, done=done, now=now)
    return {
        "siteSlug": site["slug"],
        "siteName": site["name"],
        "href": f"/sites/{site['slug']}?tab={tab}",
        "kind": kind,
        "what": what,
        "first": first,
        "current": later,
        "done": done,
        "outcome": outcome,
        "slipMonths": slip,
        "atLeast": at_least,
    }


# ---------------------------------------------------------------- items
def milestone_items(site: Doc, schedule: Doc | None) -> list[Doc]:
    out: list[Doc] = []
    for m in (schedule or {}).get("milestones") or []:
        if not isinstance(m, dict):
            continue
        first, actual, target = (
            claim(m.get("originalTarget")),
            claim(m.get("actual"), (m.get("actual") or {}).get("from")),
            claim(m.get("target")),
        )
        done = m.get("status") == "Done"
        later = actual if done else target
        if first is None and later is None:
            continue  # no dated plan in any source: nothing to judge
        out.append(
            item(site, "milestone", str(m.get("label") or m.get("key")), first, later, done=done, tab="schedule")
        )
    return out


def building_items(conn: Conn, site: Doc) -> list[Doc]:
    rows = conn.execute(
        """SELECT b.id, b.label FROM building b JOIN site_link sl ON sl.campus_id = b.campus_id
            WHERE sl.site_slug = %s AND sl.status = 'approved' ORDER BY b.id""",
        (site["slug"],),
    ).fetchall()
    out: list[Doc] = []
    for b in rows:
        f = known_facts(conn, "building", b["id"], ["expected_completion", "construction_end", "construction_complete"])
        exp = f.get("expected_completion")
        if exp is None or month_no(str(exp["value"])) is None:
            continue
        first = fact_claim(exp, str(exp["value"])[:7], f"Estimated completion {exp['value']}")
        end, complete = f.get("construction_end"), f.get("construction_complete")
        later, done = None, False
        if end is not None and month_no(str(end["value"])):
            later, done = fact_claim(end, str(end["value"])[:7], f"Construction ended {end['value']}"), True
        elif complete is not None and complete["value"] is True:
            seen = complete["valid_from"].isoformat()
            done = True
            if seen <= str(exp["value"]):  # seen finished by its target date: on time
                later = fact_claim(complete, seen[:7], f"Seen complete by {seen}")
        else:
            seen = complete["valid_from"].isoformat() if complete is not None else day().isoformat()
            later = fact_claim(complete, str(exp["value"])[:7], f"Not complete as of {seen}")
        label = str(b["label"])
        out.append(
            item(
                site,
                "building",
                label if label.lower().startswith("building") else f"Building {label}",
                first,
                later,
                done=done,
                tab="buildings",
            )
        )
    return out


def _first_source(conn: Conn, slug: str, source_name: str, as_of: str) -> Row | None:
    """The site fact a D1 reason cites for the first stated in-service date (its tag and link): the one from that
    source on that day, else that source's nearest-dated plan."""
    fields = ["original_energization", "rfs_statement", "expected_energization"]
    rows = [
        r for rs in known_fact_rows(conn, "site", slug, fields).values() for r in rs if r["source_name"] == source_name
    ]
    rows.sort(
        key=lambda r: (
            r["valid_from"].isoformat() != as_of,
            r["field"] != "original_energization",
            abs((r["valid_from"] - date.fromisoformat(as_of)).days),
        )
    )
    return rows[0] if rows else None


def in_service_item(conn: Conn, site: Doc) -> Doc | None:
    out = latest_output(conn, "status", "site", site["slug"])
    links = (out["output"] or {}).get("chain") if out is not None else None
    for link in links or []:
        m = (
            D1.search(str(link.get("reason") or ""))
            if isinstance(link, dict) and link.get("ruleCode") == "D1"
            else None
        )
        if m is None:
            continue
        first = fact_claim(_first_source(conn, site["slug"], m.group(5), m.group(6)), m.group(2), m.group(4))
        if first["sourceName"] is None:
            first.update({"sourceName": m.group(5), "asOf": m.group(6)})
        later = claim({**(link.get("fact") or {}), "text": m.group(7)}, m.group(3))
        return item(site, "in_service", "In-service date", first, later, done=False, tab="schedule")
    return None


# ---------------------------------------------------------------- pushes
def push(site: Doc, when: str, text: str, src: Doc, tab: str) -> Doc:
    out: Doc = {
        "siteSlug": site["slug"],
        "siteName": site["name"],
        "date": when,
        "text": text,
        "tag": src.get("tag") or "M",
        "sourceName": src.get("sourceName") or "Marden",
        "href": f"/sites/{site['slug']}?tab={tab}",
    }
    if src.get("sourceUrl"):
        out["sourceUrl"] = src["sourceUrl"]
    return out


def pushes(conn: Conn, site: Doc, schedule: Doc | None) -> list[Doc]:
    found: list[Doc] = []
    for m in (schedule or {}).get("milestones") or []:
        src = m.get("latestEvidence") or m.get("target") or {}
        for ev in m.get("events") or []:
            if isinstance(ev, dict) and ev.get("kind") == "slipped" and ev.get("date"):
                found.append(push(site, ev["date"], f"{m.get('label')} marked slipped", src, "schedule"))
    for r in known_fact_rows(conn, "site", site["slug"], ["schedule_change"]).get("schedule_change", []):
        v = r["value"] if isinstance(r["value"], dict) else {}
        what = str(v.get("what") or "")
        if PUSH_WORDS.search(what):
            src = {"tag": str(r["tag"]).strip(), "sourceName": r["source_name"], "sourceUrl": r.get("source_url")}
            found.append(push(site, str(v.get("date") or r["valid_from"].isoformat())[:10], what, src, "schedule"))
    impact = latest_output(conn, "event_impact", "site", site["slug"])
    for ch in ((impact["output"] or {}).get("changes") if impact is not None else None) or []:
        a, b = ch.get("from"), ch.get("to")
        if ch.get("kind") == "completion" and a and b and str(b) > str(a):
            src = {"tag": "M", "sourceName": "Marden projected completion (cushion model)"}
            found.append(
                push(site, ch["date"], str(ch.get("label") or "Projected completion moved later"), src, "schedule")
            )
    best: dict[str, Doc] = {}
    for p in sorted(found, key=lambda p: TAG_RANK.get(p["tag"], 3)):
        best.setdefault(p["date"], p)  # one push a day per site, the best-sourced record kept
    return sorted(best.values(), key=lambda p: p["date"], reverse=True)


def within(p: Doc, days: int) -> bool:
    try:
        return 0 <= (day() - date.fromisoformat(p["date"][:10])).days <= days
    except ValueError:
        return False
