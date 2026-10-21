"""The eight critical-path milestones of a site (spec 04 "Milestones"), read from its facts and events.

For each milestone the rules below give a status, the dates it was done / denied / slipped, its planned
window (when a source states one), the evidence row and its predecessors on the critical path. Rules are
fixed text and field rules (no estimate is added): a milestone no source dates is not simulated and is
listed as missing. Power milestones are in cushion_power.

Critical path: land -> shells -> first hall -> rent start; utility agreement -> gas supply / grid in
service; air permit (+ gas supply when the plant burns gas off-grid) -> on-site generation; the first hall
needs the site's power: grid sites the grid milestone, off-grid sites on-site generation, hybrid sites
both. Rent start is the rent commencement of the financed scope: for a phased lease ('first hall',
'remaining tranches', 'hall by hall') the last phase, else the first rent date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from typing import Any

from marden.core.db import Row
from marden.models import cushion_dates as cd
from marden.models.cushion_inputs import SiteInputs, text_of

KEYS = ("land_zoning", "air_permit", "utility_agreement", "gas_or_grid", "onsite_generation", "shells",
        "first_hall", "rent_start")
LABELS = {
    "land_zoning": "Land and zoning",
    "air_permit": "Air permit",
    "utility_agreement": "Utility or large-load agreement",
    "gas_or_grid": "Gas supply / grid interconnection in service",
    "onsite_generation": "On-site generation in service",
    "shells": "Building shells",
    "first_hall": "First hall energized",
    "rent_start": "Rent start",
}
CATEGORY = {"land_zoning": "permits", "air_permit": "permits", "utility_agreement": "power", "gas_or_grid": "power",
            "onsite_generation": "power", "shells": "build", "first_hall": "build", "rent_start": "build"}
DRIVER = {"land_zoning": "Permits", "air_permit": "Permits", "utility_agreement": "Power", "gas_or_grid": "Power",
          "onsite_generation": "Power", "shells": "Build", "first_hall": "Build", "rent_start": "Delivery"}

DONE_WORDS = re.compile(r"\b(live|delivered|energized|accepted|became operational|operational in|commenced|"
                        r"achieved ready|ready[- ]for[- ]service|completed delivery|online)\b", re.I)
NOT_DONE = re.compile(r"\b(not yet|not disclosed|n/d|expected|targeted?|anticipate|scheduled|planned|will|"
                      r"remain(?:ed)? undelivered)\b", re.I)
PHASED = re.compile(r"first (?:phase|hall|tranche)|horizon 1|remaining|phased|hall[- ]by[- ]hall|per hall|"
                    r"by building|lease \d|tranche", re.I)
SHELL_DONE = re.compile(r"shells? (?:up|roofed|complete)|roofed", re.I)
RISK_STAGE = re.compile(r"behind schedule|halted|stalled|paused", re.I)
STAGE_WORDS = re.compile(r"built|shell|early works|behind|on track|halted|stalled|paused|expanding", re.I)
DELIVERY_WORDS = re.compile(r"deliver|operation|ready|energiz|online|live|rent|complet|in service", re.I)


@dataclass
class Milestone:
    key: str
    status: str = "Not started"
    applicable: bool = True
    done: cd.When | None = None
    planned: cd.When | None = None
    events: list[dict[str, str]] = field(default_factory=list)
    evidence: Row | None = None
    evidence_text: str | None = None
    preds: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def is_done(self) -> bool:
        return self.status == "Done"

    def add_event(self, day: date | None, kind: str) -> None:
        if day is not None and {"date": day.isoformat(), "kind": kind} not in self.events:
            self.events.append({"date": day.isoformat(), "kind": kind})


def first_day(when: cd.When) -> date:
    return cd.month_start(when.lo)


def done_when(text: str, as_of: date, fallback: date | None = None) -> cd.When | None:
    """The completion date a 'delivered / live / accepted ...' statement gives (on or before as_of)."""
    if not text or not DONE_WORDS.search(text) or re.search(r"\bnot yet\b", text, re.I):
        return None
    past = [w for w in cd.find_dates(text) if cd.month_start(w.lo) <= as_of]
    if past:
        return min(past, key=lambda w: (w.lo, w.rank))
    if fallback is not None and fallback <= as_of:
        m = cd.month_of(fallback)
        return cd.When(m, m, "day", 0, 0, fallback.isoformat())
    return None


def _set_evidence(ms: Milestone, row: Row | None, text: str | None = None) -> None:
    if row is not None:
        ms.evidence, ms.evidence_text = row, text if text is not None else text_of(row)


def land_zoning(site: SiteInputs) -> Milestone:
    ms = Milestone("land_zoning")
    row = site.current("land")
    if row is not None:
        ms.status = "Done"
        _set_evidence(ms, row)
        dated = [w for w in cd.find_dates(text_of(row)) if cd.month_start(w.lo) <= site.as_of]
        if dated:
            ms.done = dated[0]
            ms.add_event(first_day(dated[0]), "done")
    return ms


def shells(site: SiteInputs, first_hall_done: bool) -> Milestone:
    ms = Milestone("shells", preds=["land_zoning"])
    stage, pct = site.current("construction_stage"), site.current("pct_complete")
    for r in site.rows("schedule_change"):
        if SHELL_DONE.search(text_of(r)):
            ms.status, ms.done = "Done", cd.When(cd.month_of(r["valid_from"]), cd.month_of(r["valid_from"]), "day",
                                                 0, 0, r["valid_from"].isoformat())
            ms.add_event(r["valid_from"], "done")
            _set_evidence(ms, r)
            return ms
    if pct is not None and isinstance(pct["value"], int | float) and pct["value"] >= 100:
        ms.status = "Done"
        _set_evidence(ms, pct, f"{pct['value']}% complete")
        return ms
    if stage is not None and not STAGE_WORDS.search(text_of(stage)):
        stage = None  # e.g. 'location approximate': not a construction stage
    if (stage is not None and re.search(r"\bbuilt\b|shell up", text_of(stage), re.I)) or first_hall_done:
        ms.status = "Done"
        _set_evidence(ms, stage)
        return ms
    expected = site.current("expected_energization")
    text = text_of(expected)
    shell_dates = [w for w in cd.targets(text) if cd.clause_kind(text, w) == "shell"]
    if shell_dates:
        ms.planned, ms.status = cd.earliest(shell_dates), "Projected"
        _set_evidence(ms, expected)
    if stage is not None:
        if re.search(r"early works", text_of(stage), re.I):
            ms.status = "Early works"
        elif re.search(r"on track|expanding", text_of(stage), re.I) and ms.status == "Not started":
            ms.status = "Projected"  # under way, no dated shell milestone
        elif RISK_STAGE.search(text_of(stage)):
            ms.status = "At risk"
        if ms.evidence is None:
            _set_evidence(ms, stage)
    return ms


def first_hall(site: SiteInputs) -> Milestone:
    ms = Milestone("first_hall", preds=["shells"])
    as_of = site.as_of
    fd = site.current("first_delivery")
    when = done_when(text_of(fd), as_of)
    if when is not None and not re.search(r"not disclosed", text_of(fd), re.I):
        ms.status, ms.done = "Done", when
        ms.add_event(first_day(when), "done")
        _set_evidence(ms, fd)
        return ms
    for r in sorted(site.rows("rfs_statement"), key=lambda r: r["valid_from"]):
        text = text_of(r)
        if NOT_DONE.search(text) or any(cd.month_start(w.hi) > r["valid_from"] for w in cd.targets(text)):
            continue  # a statement that still looks ahead is not a delivery
        when = done_when(text, as_of, fallback=r["valid_from"])
        if when is not None:
            ms.status, ms.done = "Done", when
            ms.add_event(first_day(when), "done")
            _set_evidence(ms, r)
            return ms
    expected = site.current("expected_energization")
    text = text_of(expected)
    candidates = [w for w in cd.targets(text) if cd.clause_kind(text, w) in ("hall", "rent")]
    rent_row = site.current("rent_start")
    rent_first = cd.leading(cd.targets(text_of(rent_row)))
    if rent_first is not None:
        candidates.append(rent_first)
    planned = cd.earliest(candidates)
    if planned is None:
        latest = _latest_statement_target(site)
        if latest is not None:
            planned, row = latest
            _set_evidence(ms, row)
    else:
        _set_evidence(ms, expected if any(w is not rent_first for w in candidates) else rent_row)
    if planned is not None:
        ms.planned, ms.status = planned, "Projected"
        _mark_slips(ms, site, planned)
        if cd.month_start(planned.hi + 1) <= as_of:
            ms.status, ms.note = "At risk", "planned date passed with no delivery recorded"
    stage = site.current("construction_stage")
    if stage is not None and RISK_STAGE.search(text_of(stage)) and ms.status == "Projected":
        ms.status = "At risk"
    return ms


def _latest_statement_target(site: SiteInputs) -> tuple[cd.When, Row] | None:
    """The newest dated statement's delivery date (a clause about delivery, not a contract term)."""
    for r in sorted(site.rows("rfs_statement"), key=lambda r: r["valid_from"], reverse=True):
        text = text_of(r)
        dates = [w for w in cd.targets(text) if DELIVERY_WORDS.search(cd.clause_of(text, w))
                 and cd.clause_kind(text, w) in ("hall", "rent")]
        when = cd.leading(dates)
        if when is not None:
            return when, r
    return None


def _mark_slips(ms: Milestone, site: SiteInputs, planned: cd.When) -> None:
    """Slipped when an earlier statement gave an earlier date (original_energization, the dated
    ready-for-service statements). A 'slipped' event is dated at each statement that moved the date later."""
    stated: list[tuple[date, cd.When]] = []
    orig = site.current("original_energization")
    if orig is not None:
        first = cd.leading(cd.targets(text_of(orig)))
        said = cd.statement_dates(text_of(orig))
        if first is not None and said:
            stated.append((cd.month_start(said[0].lo), first))
    for r in site.rows("rfs_statement"):
        text = text_of(r)
        when = cd.leading([w for w in cd.targets(text) if cd.clause_kind(text, w) in ("hall", "rent")])
        if when is not None:
            stated.append((r["valid_from"], when))
    stated.sort(key=lambda s: s[0])
    for (_, before), (day, after) in pairwise(stated):
        if after.lo > before.hi:
            ms.add_event(day, "slipped")
    first_plan = cd.leading(cd.targets(text_of(orig))) if orig is not None else None
    if ms.events or (first_plan is not None and first_plan.hi < planned.lo):
        ms.status = "Slipped"
        if not ms.events and ms.evidence is not None:
            ms.add_event(ms.evidence["valid_from"], "slipped")


def rent_start(site: SiteInputs, hall: Milestone) -> Milestone:
    ms = Milestone("rent_start", preds=["first_hall"])
    row = site.current("rent_start")
    text = text_of(row)
    exprs = cd.targets(text)
    if row is None or not exprs:
        ms.note = "no rent commencement date in any source: rent start follows the first hall"
        ms.status = "Done" if hall.is_done else "Projected" if hall.planned else "Not started"
        ms.done = hall.done if hall.is_done else None
        return ms
    _set_evidence(ms, row)
    phased = bool(PHASED.search(text))
    if phased:
        expected = cd.leading(cd.targets(site.text("expected_energization")))
        planned = cd.latest(exprs + ([expected] if expected else []))
        ms.note = "phased lease: rent start of the last phase"
    else:
        planned = cd.leading(exprs)
    assert planned is not None
    commenced = re.search(r"commenced|accepted", text, re.I) is not None
    if cd.month_start(planned.hi) <= site.as_of and (hall.is_done or commenced):
        ms.status, ms.done = "Done", planned
        ms.add_event(cd.month_start(planned.hi), "done")
        return ms
    for w in exprs:
        if re.search(r"commenced|accepted", cd.clause_of(text, w), re.I) and cd.month_start(w.lo) <= site.as_of:
            ms.add_event(first_day(w), "done")
    ms.planned, ms.status = planned, "Projected"
    if cd.month_start(planned.hi + 1) <= site.as_of:
        ms.status = "At risk"
        ms.note = (ms.note + "; " if ms.note else "") + "planned date passed with no delivery recorded"
    return ms


def milestone_doc(ms: Milestone, projected: dict[str, str] | None) -> dict[str, Any]:
    """types.ts Milestone."""
    doc: dict[str, Any] = {"key": ms.key, "label": LABELS[ms.key], "status": ms.status,
                           "events": sorted(ms.events, key=lambda e: e["date"])}
    if projected is not None:
        doc["projected"] = projected
    if ms.evidence is not None:
        doc["latestEvidence"] = evidence_doc(ms.evidence, ms.evidence_text)
    return doc


def evidence_doc(row: Row, text: str | None = None) -> dict[str, Any]:
    """types.ts Evidence for a fact or event row."""
    day = row.get("event_date") or row.get("valid_from")
    body = (text if text is not None else row.get("title") or text_of(row)).strip()
    return {
        "date": day.isoformat() if hasattr(day, "isoformat") else str(day),
        "text": body if len(body) <= 240 else body[:237].rstrip() + "...",
        "tag": row["tag"],
        "sourceName": row["source_name"],
        "sourceUrl": row.get("source_url") or "",
    }
