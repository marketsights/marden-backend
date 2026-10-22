"""One status per campus and per building, decided from the strongest, newest record (spec 14, 16).

Campus records and building records often disagree: Epoch AI's campus series counts "4 of 5 buildings
operational" in May while its footprint map, traced in October, marks none of the five complete; the
research archive's campus status says "Under construction" while a footprint is marked complete; a
company filing puts the first data hall in Q4 2026 while imagery shows the shell finished. Showing both
leaves the reader to pick. This module picks, by a fixed rule, and says which record won:

1. Strength of the record (strongest first):
   4  a certificate of occupancy, energization or in-service record (`co_date`, `energized_date`,
      `in_service_date` on the building);
   3  a utility or permit record (a filed status whose source is a permit office, county, city, utility
      or commission; a state construction registration such as Texas TDLR TABS: a completed state
      inspection says construction is finished, a completion date still ahead says it is not);
   2  a company statement (a status or operational count the operator, tenant or developer filed or
      published: SEC filings, press releases), filed before reported on the same day;
   1  a third-party tracker (Epoch AI): its campus count of operational buildings, a footprint it marks
      complete (construction finished in its imagery; Marden's rule reads that as Operating);
   0  Marden's own rule with no document behind it: the research archive's campus status, and a
      footprint Epoch has not marked complete (no completion mark is not a record that it is unfinished).
2. Within one strength, the newest record wins (its `valid_from`).

Claims are about how many of the campus's buildings are operating (K):
- a campus count (`buildings_operational`) states K exactly;
- the building records give a lower bound: the buildings with an operating record;
- a campus status from a document (tag F/R) of Planned / Under construction states K = 0; Operating
  states K >= 1 (on a one-building campus, K = 1).
A newer building-level operating record of at least the winner's strength adds to an older count (a
building finished after the count was made). Then each building gets one status: its own operating
record when K covers it, Operating for all when K = n, its own non-operating status when K covers
none of the rest, and n/d with the reason when the record says how many but not which. The campus
status follows its most advanced building: Operating when K > 0; otherwise the strongest non-operating
status (Paused and Cancelled campus statuses are kept). `note` is one plain line, set only when a
losing record disagrees with K ("1 of 1 buildings operating (Epoch AI, Oct 8, 2026); Epoch AI's Jul 30,
2026 count of 0 is older"), with every record compared in `sources`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows
from marden.readmodels.views.facilities_common import DC_STATUSES, campus_buildings
from marden.readmodels.views.sites_common import Doc, nd, with_value

TRACKERS = ("epoch ai",)
_PERMIT = re.compile(
    r"permit|county|parish|city of|township|village of|planning|zoning|tdlr|licensing and regulation|"
    r"utility|electric|power company|commission|\bpsc\b|\bpuc\b|energy|entergy|ercot|\bpjm\b|\bmiso\b",
    re.I,
)
IN_SERVICE_FIELDS = ("co_date", "energized_date", "in_service_date")
REGISTRATION = "state_registration"
BUILDING_FIELDS = ("status", "construction_complete", REGISTRATION, *IN_SERVICE_FIELDS)
CAMPUS_FIELDS = ("status", "buildings_operational")
NOT_OPERATING = ("Planned", "Under construction")
RANK_WORDS = {4: "certificate of occupancy or energization record", 3: "utility or permit record",
              2: "company statement", 1: "third-party tracker", 0: "Marden rule"}


@dataclass(frozen=True)
class Claim:
    """One record about the operating state: `kind` 'count' (K exactly), 'floor' (K >= value), or
    'building' (one building operating or not)."""

    kind: str
    value: int
    rank: int
    day: date
    row: Row
    status: str | None = None  # the stated status ('Under construction', 'Planned', 'Operating', ...)
    building: str | None = None

    @property
    def strength(self) -> tuple[int, date, int, int]:
        tag = self.row.get("tag") or "M"
        return (self.rank, self.day, {"F": 2, "R": 1}.get(tag, 0), int(self.row.get("id") or 0))


@dataclass
class Resolution:
    total: int
    operating: int | None
    status: Doc
    buildings: dict[str, Doc] = field(default_factory=dict)  # building id -> Sourced<DcStatus>
    building_notes: dict[str, str] = field(default_factory=dict)
    note: str | None = None
    sources: list[Doc] = field(default_factory=list)
    winner: Claim | None = None


def _source(row: Row) -> str:
    return str(row.get("source_name") or "")


def is_tracker(row: Row) -> bool:
    return _source(row).lower().startswith(TRACKERS)


def rank_of(row: Row, *, operating: bool) -> int:
    """Strength of a status or count record (module docstring, rule 1)."""
    if row.get("field") in IN_SERVICE_FIELDS:
        return 4
    tag = row.get("tag") or "M"
    if is_tracker(row):
        if tag == "M" and not operating:
            return 0  # no completion mark on a traced footprint
        return 1
    if tag == "M":
        return 0
    if tag == "F" and _PERMIT.search(f"{_source(row)} {row.get('source_url') or ''}") and "sec.gov" not in str(
        row.get("source_url") or ""
    ):
        return 3
    return 2


def _day(row: Row) -> date:
    v = row["valid_from"]
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def building_claims(bid: str, rows: dict[str, list[Row]]) -> list[Claim]:
    out: list[Claim] = []
    for f in IN_SERVICE_FIELDS:
        for r in rows.get(f, []):
            if isinstance(r["value"], str) and re.match(r"^\d{4}-\d{2}-\d{2}", r["value"]):
                out.append(Claim("building", 1, 4, _day(r), r, "Operating", bid))
    for r in rows.get("status", []):
        if r["value"] in DC_STATUSES:
            op = r["value"] == "Operating"
            out.append(Claim("building", int(op), rank_of(r, operating=op), _day(r), r, r["value"], bid))
    if (reg := registration_claim(bid, rows.get(REGISTRATION, []))) is not None:
        out.append(reg)
    return out


def registration_claim(bid: str, rows: list[Row]) -> Claim | None:
    """A state construction registration (Texas TDLR TABS) as a permit record: a scope still due to finish
    after the day it was read (completion date ahead, inspection not complete) says the building is under
    construction; otherwise a completed state inspection says its construction is finished."""
    regs = [(r, r["value"]) for r in rows if isinstance(r["value"], dict)]
    if not regs:
        return None
    pending = [(r, v) for r, v in regs if v.get("status") != "Inspection Complete"
               and str(v.get("completion") or "") > _day(r).isoformat()]
    if pending:
        r, _ = max(pending, key=lambda rv: str(rv[1].get("completion")))
        return Claim("building", 0, rank_of(r, operating=False), _day(r), r, "Under construction", bid)
    done = [(r, v) for r, v in regs if v.get("status") == "Inspection Complete"]
    if done:
        r, _ = max(done, key=lambda rv: _day(rv[0]))
        return Claim("building", 1, rank_of(r, operating=True), _day(r), r, "Operating", bid)
    return None


def newest_per_source(rows: list[Row]) -> list[Row]:
    """The newest row of each source: older rows of one source are its history, not a second record."""
    best: dict[str, Row] = {}
    for r in rows:
        key = _source(r).lower()
        if key not in best or (_day(r), int(r.get("id") or 0)) > (_day(best[key]), int(best[key].get("id") or 0)):
            best[key] = r
    return list(best.values())


def campus_claims(rows: dict[str, list[Row]], total: int) -> list[Claim]:
    rows = {f: newest_per_source(rs) for f, rs in rows.items()}
    out: list[Claim] = []
    for r in rows.get("buildings_operational", []):
        v = r["value"]
        if isinstance(v, int | float) and not isinstance(v, bool) and v >= 0:
            out.append(Claim("count", int(v), rank_of(r, operating=v > 0), _day(r), r))
    for r in rows.get("status", []):
        v = r["value"]
        if v not in DC_STATUSES:
            continue
        rank = rank_of(r, operating=v == "Operating")
        if v == "Operating":
            out.append(Claim("count" if total == 1 else "floor", 1, rank, _day(r), r, v))
        elif v in NOT_OPERATING:
            out.append(Claim("count", 0, rank, _day(r), r, v))
    return out


def _date_text(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _evidence(c: Claim) -> Doc:
    if c.kind == "count":
        what = f"{c.status} (campus)" if c.status else f"{c.value} buildings operational"
    elif c.kind == "floor":
        what = "Operating (campus)"
    elif c.row.get("field") == REGISTRATION:
        v = c.row["value"]
        what = (f"construction complete (state inspection, {v.get('number')})" if c.value else
                f"under construction ({v.get('number')}: completion {v.get('completion')})")
    else:
        what = f"{c.status}" + (" (in service)" if c.rank == 4 else "")
    doc = with_value(c.row, what)
    doc["strength"] = RANK_WORDS[c.rank]
    if c.building:
        doc["building"] = c.building
    return doc


def decide(candidates: list[Claim], n: int) -> tuple[Claim | None, int | None]:
    """(the record the count follows, K). Only the strongest records count. Their newest exact count is
    the base (each source's older counts are its history); a newer record of the same strength raises it
    (buildings come into service, they do not leave it). A newer, lower floor (fewer footprints marked
    complete than the count says operate) is only a lower bound, not a contradiction."""
    if not candidates:
        return None, (0 if n else None)
    top = max(c.rank for c in candidates)
    pool = [c for c in candidates if c.rank == top]
    exact = [c for c in pool if c.kind == "count"]
    if exact:
        base = max(exact, key=lambda c: c.strength)
        later = [c for c in pool if c.day > base.day]
        k = max([base.value, *(c.value for c in later)])
        winner = max([base, *later], key=lambda c: (c.value, c.strength)) if later else base
        if winner.value < k:
            winner = base
    else:
        winner = max(pool, key=lambda c: (c.value, c.strength))
        k = winner.value
    return winner, (min(k, n) if n else k)


def _pick_status(claims: list[Claim]) -> Claim | None:
    return max(claims, key=lambda c: c.strength) if claims else None


def resolve(labels: dict[str, str], per_building: dict[str, list[Claim]], campus: list[Claim],
            campus_status: Row | None) -> Resolution:
    """The decision for one campus (pure: tested without a database). `labels` id -> 'Building 1' in
    display order; `per_building` each building's own claims; `campus` the campus-level claims."""
    n = len(labels)
    own_best = {bid: _pick_status(cs) for bid, cs in per_building.items()}
    positives = [c for c in own_best.values() if c is not None and c.value == 1]
    floor = None
    if positives:
        lead = max(positives, key=lambda c: c.strength)
        weakest = min(c.rank for c in positives)
        floor = Claim("floor", len(positives), weakest, max(c.day for c in positives), lead.row, "Operating",
                      None if len(positives) > 1 else lead.building)
    exact_zero_buildings = [c for c in own_best.values() if c is not None and c.value == 0 and c.rank >= 2]
    candidates = list(campus) + ([floor] if floor else [])
    if n and len(exact_zero_buildings) == n:
        lead = max(exact_zero_buildings, key=lambda c: c.strength)
        candidates.append(Claim("count", 0, min(c.rank for c in exact_zero_buildings), lead.day, lead.row,
                                lead.status, lead.building if n == 1 else None))
    winner, k = decide(candidates, n)
    if k is not None and n:
        k = min(k, n)
    res = Resolution(total=n, operating=k, status=nd("M"), winner=winner)

    # ---- each building's one status
    positive_ids = [bid for bid, c in own_best.items() if c is not None and c.value == 1]
    positive_ids.sort(key=lambda b: own_best[b].strength, reverse=True)  # type: ignore[union-attr]
    for bid in labels:
        own = own_best.get(bid)
        if k is None:
            res.buildings[bid] = with_value(own.row, own.status) if own else nd("R")
            continue
        if bid in positive_ids:
            if positive_ids.index(bid) < k:
                res.buildings[bid] = with_value(own.row, "Operating")  # type: ignore[union-attr]
                continue
            if k == 0 and winner is not None:
                res.buildings[bid] = with_value(winner.row, _not_operating(winner, own))
                res.building_notes[bid] = _overruled(own, winner)  # type: ignore[arg-type]
                continue
            res.buildings[bid] = nd("R")
            res.building_notes[bid] = _which_unknown(k, n, winner)
            continue
        if k == n and winner is not None:
            res.buildings[bid] = with_value(winner.row, "Operating")
            continue
        if k > len(positive_ids):
            res.buildings[bid] = nd("R")
            res.building_notes[bid] = _which_unknown(k, n, winner)
            continue
        if own is not None:
            res.buildings[bid] = with_value(own.row, own.status)
            beaten = [c for c in per_building.get(bid, []) if c.value == 1 and c is not own]
            if beaten and own.value == 0:
                res.building_notes[bid] = _overruled(max(beaten, key=lambda c: c.strength), own)
        elif winner is not None:
            res.buildings[bid] = with_value(winner.row, _not_operating(winner, None))
        else:
            res.buildings[bid] = nd("R")

    # ---- the campus status follows its most advanced building
    res.status = campus_status_doc(k, n, winner, campus_status, res.buildings)
    res.sources = [_evidence(c) for c in sorted(_compared(campus, own_best), key=lambda c: c.strength, reverse=True)]
    beaten = [c for cs in per_building.values() for c in cs if c.value == 1 and c.rank >= 1]
    shown = floor
    if beaten and (floor is None or floor.value < len({c.building for c in beaten})):
        lead = max(beaten, key=lambda c: c.strength)
        shown = Claim("floor", len({c.building for c in beaten}), min(c.rank for c in beaten),
                      max(c.day for c in beaten), lead.row, "Operating", lead.building)
    known = sum(1 for d in res.buildings.values() if d.get("value") == "Operating")
    known_labels = [labels[b] for b, d in res.buildings.items() if d.get("value") == "Operating"]
    res.note = _note(k, n, winner, campus, shown, labels, known if k != n else k, known_labels)
    return res


def _compared(campus: list[Claim], own_best: dict[str, Claim | None]) -> list[Claim]:
    out = list(campus)
    out += [c for c in own_best.values() if c is not None and c.rank >= 1]
    return out


def _not_operating(winner: Claim, own: Claim | None) -> str:
    if winner.status in NOT_OPERATING:
        return winner.status
    if own is not None and own.status in NOT_OPERATING:
        return own.status
    return "Under construction"


def _iso_text(v: Any) -> str:
    try:
        return _date_text(date.fromisoformat(str(v)[:10]))
    except ValueError:
        return str(v)


def _names(labels: list[str]) -> str:
    """'Building 1', 'Buildings 1 and 2', 'Buildings 1, 2 and 5' (other labels as they are)."""
    nums = [lb.removeprefix("Building ") for lb in labels]
    if all(lb.startswith("Building ") for lb in labels) and len(nums) > 1:
        return "Buildings " + (", ".join(nums[:-1]) + " and " + nums[-1])
    return " and ".join(labels) if len(labels) <= 2 else ", ".join(labels[:-1]) + " and " + labels[-1]


SHORT_SOURCES = {"texas department of licensing and regulation (tabs)": "Texas TDLR"}


def _who(c: Claim) -> str:
    name = _source(c.row)
    return SHORT_SOURCES.get(name.lower(), name) or "the record"


def _which_unknown(k: int, n: int, winner: Claim | None) -> str:
    src = f" ({_who(winner)}, {_date_text(winner.day)})" if winner else ""
    return f"{k} of {n} buildings operating{src}; which ones is not public."


def _overruled(own: Claim, winner: Claim) -> str:
    what = "Construction complete in imagery" if own.rank <= 1 else "Operating per its record"
    if winner.row.get("field") == REGISTRATION:
        v = winner.row["value"]
        return (f"{what} ({_who(own)}, {_date_text(own.day)}); the state registration {v.get('number')} "
                f"({str(v.get('status') or '').lower()}) still dates its completion {_iso_text(v.get('completion'))}.")
    return (f"{what} ({_who(own)}, {_date_text(own.day)}); {_who(winner)} ({_date_text(winner.day)}, "
            f"{RANK_WORDS[winner.rank]}) says {_not_operating(winner, own).lower()}.")


def campus_status_doc(k: int | None, n: int, winner: Claim | None, stated: Row | None,
                      buildings: dict[str, Doc]) -> Doc:
    """Operating when any building operates; else the strongest non-operating status. A stated Paused or
    Cancelled campus status is kept; with no building records the stated status stands."""
    if stated is not None and stated["value"] in ("Paused", "Cancelled"):
        return with_value(stated, stated["value"])
    if not n or k is None:
        return with_value(stated, stated["value"]) if stated is not None and stated["value"] in DC_STATUSES \
            else nd("M")
    if k > 0:
        if stated is not None and stated["value"] == "Operating":
            return with_value(stated, "Operating")
        lead = next((d for d in buildings.values() if d.get("value") == "Operating"), None)
        if lead is not None:
            return {**lead, "value": "Operating"}
        assert winner is not None
        return with_value(winner.row, "Operating")
    statuses = {d.get("value") for d in buildings.values()}
    value = "Under construction" if "Under construction" in statuses else "Planned" if "Planned" in statuses \
        else "Under construction"
    if stated is not None and stated["value"] == value:
        return with_value(stated, value)
    if winner is not None and winner.status == value:
        return with_value(winner.row, value)
    lead = next((d for d in buildings.values() if d.get("value") == value), None)
    return {**lead, "value": value} if lead else (with_value(winner.row, value) if winner else nd("M"))


def _count_of(c: Claim, n: int, labels: dict[str, str]) -> str:
    if c.kind == "floor":
        if is_tracker(c.row):
            return f"marks {c.value} complete" if n > 1 else "marks it complete"
        return f"{c.value} with an operating record" if n > 1 else "operating"
    if c.status in NOT_OPERATING:
        return c.status.lower()
    return f"{c.value} operating" if n > 1 else ("operating" if c.value else "not operating")


def _note(k: int | None, n: int, winner: Claim | None, campus: list[Claim], floor: Claim | None,
          labels: dict[str, str], known: int = 0, known_labels: list[str] | None = None) -> str | None:
    """One plain line when a current record of another source (or strength) disagrees with the decision,
    or when the record says how many buildings operate but not which; None otherwise."""
    if k is None or winner is None or not n:
        return None
    losers = []
    for c in campus + ([floor] if floor else []):
        if c is winner or c.row is winner.row or c.rank < 1:
            continue
        agrees = (c.kind == "floor" and c.value <= k) or (c.kind == "count" and c.value == k)
        # the same source's older, lower count is growth, not a disagreement
        growth = c.kind == "count" and c.value < k and _who(c) == _who(winner) and c.day <= winner.day
        if not agrees and not growth:
            losers.append(c)
    unknown = k > known and k < n
    if not losers and not unknown:
        return None
    s = "s" if n != 1 else ""
    head = f"{k} of {n} building{s} operating"
    if winner.rank >= 1:
        head += f" ({_who(winner)}, {_date_text(winner.day)})"
    if winner.row.get("field") == REGISTRATION:
        v = winner.row["value"]
        if k:
            head = (f"{k} of {n} building{s} finished (state inspection{'s' if k != 1 else ''} complete, "
                    f"{_who(winner)}, read {_date_text(winner.day)})")
        else:
            head = (f"{'Under construction' if n == 1 else f'0 of {n} buildings finished'}: the state registration "
                    f"{v.get('number')} dates completion {_iso_text(v.get('completion'))} ({_who(winner)}, read "
                    f"{_date_text(winner.day)})")
    parts = []
    for c in sorted(losers, key=lambda c: c.strength, reverse=True)[:2]:
        why = "is older" if c.rank == winner.rank and c.day <= winner.day else \
            f"is a weaker record ({RANK_WORDS[c.rank]})" if c.rank < winner.rank else "is set aside"
        what = "footprint map" if c.kind == "floor" and is_tracker(c.row) else "building records" \
            if c.kind == "floor" else "count" if c.status is None else "status"
        parts.append(f"{_who(c)}'s {_date_text(c.day)} {what} ({_count_of(c, n, labels)}) {why}")
    if unknown:
        rest = k - known
        parts.append(f"{_names(known_labels)} {'has' if known == 1 else 'have'} a completion record; which other "
                     f"{rest} {'operates' if rest == 1 else 'operate'} is not public" if known else
                     "which ones is not public")
    return f"{head}; " + "; ".join(parts) + "."


# ---------------------------------------------------------------- reads
def load(conn: Conn, campus_id: str, buildings: list[Row] | None = None) -> Resolution:
    """The decision for a campus as known on the build date."""
    rows = buildings if buildings is not None else campus_buildings(conn, campus_id)
    labels = {b["id"]: b["label"] or b["id"] for b in rows}
    per_building = {bid: building_claims(bid, _visible(known_fact_rows(conn, "building", bid, BUILDING_FIELDS)))
                    for bid in labels}
    crow = _visible(known_fact_rows(conn, "campus", campus_id, CAMPUS_FIELDS))
    stated = _current_status(crow.get("status", []))
    return resolve(labels, per_building, campus_claims(crow, len(labels)), stated)


def _visible(rows: dict[str, list[Row]]) -> dict[str, list[Row]]:
    from marden.readmodels.views.sites_common import day

    return {f: [r for r in rs if r["valid_from"] <= day()] for f, rs in rows.items()}


def _current_status(rows: list[Row]) -> Row | None:
    """The campus's stated status as fact_current would pick it (model values last, newest, filed first)."""
    if not rows:
        return None
    return min(rows, key=lambda r: (r["tag"] == "M", -_day(r).toordinal(), {"F": 0, "R": 1}.get(r["tag"], 2),
                                    -int(r["id"])))


def status_note_doc(res: Resolution) -> Doc | None:
    """types.ts StatusNote: the one plain line and the records compared, or None."""
    if not res.note:
        return None
    return {"text": res.note, "sources": res.sources}


def summary(res: Resolution) -> dict[str, Any]:
    return {"operating": res.operating, "total": res.total, "status": res.status.get("value"), "note": res.note}
