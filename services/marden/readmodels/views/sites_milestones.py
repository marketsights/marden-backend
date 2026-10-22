"""Schedule tab milestone dates (spec 04 "Milestones"): what each milestone row needs beyond the cushion
model's stored output, read from the same facts the model read on its run day.

The stored `cushion` output carries each milestone's status, events, P10/P50/P90 and evidence, but not the
dates a source states for it. This module re-reads the run day's inputs (`cushion_inputs.load_sites`, point
in time through `marden.models.pit`) and the model's own milestone rules (`cushion_power.build`), and adds
per milestone:

- `target`: the plan a source states now (the window the simulation draws from), with the stated wording;
- `originalTarget`: the earliest plan a source stated, when one is on file (first hall: the dossier's
  original energization and the dated ready-for-service statements; gas supply and on-site generation:
  the serving plant's original COD);
- `actual`: the completion date when the milestone is done and a source dates it;
- `dateNotes`: why a date is n/d;
- `rentPath` (on the critical path to rent start: the predecessors of rent start for the site's power
  mode) and `controlsRent` (share of the simulated schedules in which this milestone set the rent-start
  date: the output's `basis.bindingShare`).

Nothing is estimated here: a date no source states stays absent, with its reason.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.core.db import Conn, Row

Doc = dict[str, Any]

NO_PLAN = "no dated plan in any source"
NO_ORIGINAL = "no earlier dated plan in any source"
UNDATED_DONE = "done, but no source dates the completion"


def _source(row: Row | None) -> Doc:
    if row is None:
        return {}
    day = row.get("event_date") or row.get("valid_from")
    out: Doc = {"tag": row["tag"], "sourceName": row["source_name"],
                "asOf": day.isoformat() if hasattr(day, "isoformat") else str(day)}
    if row.get("source_url"):
        out["sourceUrl"] = row["source_url"]
    return out


def _when(w: Any, row: Row | None = None) -> Doc:
    """A stated window: first and last month ('YYYY-MM'), the source's wording and, when known, its row."""
    from marden.models import cushion_dates as cd

    return {"from": cd.ym(w.lo), "to": cd.ym(w.hi), "text": w.text, **_source(row)}


def _cited(site: Any, ms: Any, w: Any) -> Row | None:
    """The row that states the window: the milestone's evidence first, then the site's dated plan fields,
    the serving plants' expected and original COD and the site's events. None when no row states it."""
    from marden.models.cushion_inputs import text_of

    if not w.text:
        return None
    rows: list[Row | None] = [ms.evidence]
    rows += [site.current(f) for f in ("expected_energization", "original_energization", "rent_start")]
    rows += [p.current(f) for p in site.plants for f in ("expected_cod", "original_cod", "latest_change")]
    rows += sorted(site.rows("rfs_statement"), key=lambda r: r["valid_from"], reverse=True)
    rows += sorted(site.events, key=lambda e: e["event_date"], reverse=True)
    for row in rows:
        if row is not None and w.text in (row.get("title") or text_of(row) or ""):
            return row
    return None


def _first_hall_original(site: Any) -> tuple[Any, Row] | None:
    """The earliest-stated delivery plan (as the D1 rule and `_mark_slips` read them)."""
    from marden.models import cushion_dates as cd
    from marden.models.cushion_inputs import text_of

    stated: list[tuple[date, Any, Row]] = []
    orig = site.current("original_energization")
    if orig is not None:
        first = cd.leading(cd.targets(text_of(orig)))
        said = cd.statement_dates(text_of(orig))
        if first is not None:
            stated.append((cd.month_start(said[0].lo) if said else orig["valid_from"], first, orig))
    for r in site.rows("rfs_statement"):
        text = text_of(r)
        w = cd.leading([w for w in cd.targets(text) if cd.clause_kind(text, w) in ("hall", "rent")])
        if w is not None:
            stated.append((r["valid_from"], w, r))
    if not stated:
        return None
    _, w, row = min(stated, key=lambda s: (s[0], s[1].lo))
    return w, row


def _plant_original(site: Any, key: str, planned: Any) -> tuple[Any, Row] | None:
    """The serving plant's original COD: the plant whose expected COD is the milestone's plan."""
    from marden.models import cushion_dates as cd
    from marden.models.cushion_inputs import text_of

    supply = key == "gas_or_grid"
    for p in site.plants:
        if p.is_supply_line != supply:
            continue
        exp = cd.leading(cd.targets(text_of(p.current("expected_cod"))))
        row = p.current("original_cod")
        orig = cd.leading(cd.targets(text_of(row)))
        if orig is not None and (planned is None or (exp is not None and exp.hi == planned.hi)):
            return orig, row
    return None


def _ancestors(by_key: dict[str, Any], root: str) -> set[str]:
    seen: set[str] = set()
    stack = [root]
    while stack:
        for p in by_key[stack.pop()].preds:
            if p not in seen:
                seen.add(p)
                stack.append(p)
    return seen


def milestone_dates(conn: Conn, slug: str, run_day: date, binding: dict[str, float]) -> dict[str, Doc]:
    """Per milestone key: target, originalTarget, actual, dateNotes, rentPath, controlsRent."""
    from marden.models.cushion_inputs import load_sites
    from marden.models.cushion_power import build

    sites = load_sites(conn, run_day, [slug])
    if not sites:
        return {}
    site = sites[0]
    milestones, _ = build(site)
    by_key = {m.key: m for m in milestones}
    path = _ancestors(by_key, "rent_start") | {"rent_start"}
    out: dict[str, Doc] = {}
    for ms in milestones:
        if not ms.applicable:
            continue
        doc: Doc = {"rentPath": ms.key in path}
        notes: Doc = {}
        if ms.key in binding:
            doc["controlsRent"] = binding[ms.key]
        if ms.planned is not None:
            doc["target"] = _when(ms.planned, _cited(site, ms, ms.planned))
        elif not ms.is_done:
            notes["target"] = ms.note if ms.key == "rent_start" and ms.note else NO_PLAN
        original = _first_hall_original(site) if ms.key == "first_hall" else \
            _plant_original(site, ms.key, ms.planned) if ms.key in ("gas_or_grid", "onsite_generation") else None
        if original is not None:
            doc["originalTarget"] = _when(*original)
        elif not ms.is_done:
            notes["originalTarget"] = NO_ORIGINAL
        if ms.is_done:
            if ms.done is not None:
                doc["actual"] = _when(ms.done, _cited(site, ms, ms.done))
            else:
                notes["actual"] = ms.note or UNDATED_DONE
        if notes:
            doc["dateNotes"] = notes
        out[ms.key] = doc
    return out


def with_dates(conn: Conn, slug: str, output_row: Row | None, milestones: list[Doc]) -> tuple[list[Doc], Doc | None]:
    """The stored milestones with their dates, and the model facts the tab names (run day, simulations,
    rent-start control shares). Without the models package or a run, the milestones are returned as stored."""
    if output_row is None or not milestones:
        return milestones, None
    try:
        import marden.models.cushion_power  # noqa: F401
    except ImportError:
        return milestones, None
    basis = (output_row["output"] or {}).get("basis") or {}
    binding = basis.get("bindingShare") if isinstance(basis.get("bindingShare"), dict) else {}
    dates = milestone_dates(conn, slug, output_row["as_of"], binding)
    model: Doc = {"runDate": output_row["as_of"].isoformat(), "simulations": basis.get("simulations"),
                  "methodVersion": basis.get("methodVersion"), "powerMode": basis.get("powerMode")}
    return [{**ms, **dates.get(str(ms.get("key")), {})} for ms in milestones], model
