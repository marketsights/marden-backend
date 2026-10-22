"""Power milestones of a site (utility agreement, gas supply / grid in service, on-site generation, air
permit) and the assembly of all eight milestones with their critical-path predecessors.

Inputs: the site's power_supply / power_status / power_contract / onsite_generation / air_permits facts,
its expected_energization 'grid' clauses and dated statements, the plants serving it (a lateral or
pipeline is gas supply, anything else generation: status, expected and original COD, latest change,
air permit) and its catalyst / permit / opposition events.
"""

from __future__ import annotations

import re

from marden.core.db import Row
from marden.models import cushion_dates as cd
from marden.models import cushion_milestones as cm
from marden.models.cushion_inputs import Plant, SiteInputs, text_of
from marden.readmodels.context import business_day

AGREEMENT = re.compile(r"\besa\b|electric service agreement|interconnection agreement|\bppa\b|supply deal|"
                       r"service agreement|agreement with|contracts approved|\bia signed", re.I)
UNKNOWN = re.compile(r"not disclosed|status n/d|terms not public", re.I)
UNRESOLVED = re.compile(r"unresolved|pending|denied|restarted|contested", re.I)
ENERGIZED = re.compile(r"(?<!un)energized", re.I)
CONTESTED = re.compile(r"contest|pause|lawsuit|suit\b|appeal|motions? to dismiss|enforcement|injunction|"
                       r"protest|unconfirmed", re.I)
PUSHED = re.compile(r"pushed|out of reach|delayed|slipp", re.I)
PERMIT_WORDS = re.compile(r"permit|hearing|nmed|tceq|air", re.I)


def power_mode(site: SiteInputs) -> str:
    """'grid' | 'onsite' | 'hybrid' | 'unknown' from the site's power_supply text."""
    text = site.text("power_supply").lower()
    if not text:
        return "unknown"
    onsite = bool(re.search(r"on-site|onsite|behind-the-meter|bridge generation|off-grid", text))
    if "off-grid" in text or (onsite and "grid" not in text.replace("off-grid", "")):
        return "onsite"
    if onsite:
        return "hybrid"
    return "grid"


def utility_agreement(site: SiteInputs) -> cm.Milestone:
    ms = cm.Milestone("utility_agreement")
    status, contract = site.current("power_status"), site.current("power_contract")
    s_text, c_text = text_of(status), text_of(contract)
    if re.search(r"esa/ia signed|\bia signed", s_text, re.I) or ENERGIZED.search(s_text):
        ms.status = "Done"
        cm._set_evidence(ms, status)
    elif AGREEMENT.search(c_text) and not re.search(r"agreement status not disclosed", c_text, re.I):
        ms.status = "Done"
        cm._set_evidence(ms, contract)
    elif UNRESOLVED.search(s_text):
        ms.status = "At risk"
        cm._set_evidence(ms, status)
    else:
        cm._set_evidence(ms, contract or status)
    if ms.is_done and ms.evidence is not None:
        dated = [w for w in cd.find_dates(ms.evidence_text) if cd.month_start(w.lo) <= site.as_of]
        if dated:
            ms.done = dated[0]
            ms.add_event(cm.first_day(dated[0]), "done")
    return ms


def _plant_dates(plant: Plant) -> tuple[cd.When | None, cd.When | None]:
    exp, orig = plant.current("expected_cod"), plant.current("original_cod")
    return cd.leading(cd.targets(text_of(exp))), cd.leading(cd.targets(text_of(orig)))


def _from_plants(ms: cm.Milestone, site: SiteInputs, plants: list[Plant]) -> bool:
    """Status and plan from the serving plants (all must be in service). False when no plant decides it."""
    if not plants:
        return False
    statuses = {p.id: text_of(p.current("status")) for p in plants}
    if all(s == "Operating" for s in statuses.values()):
        ms.status = "Done"
        cm._set_evidence(ms, plants[0].current("status"))
        return True
    planned, slipped, risk, evidence = None, False, False, None
    for p in plants:
        exp, orig = _plant_dates(p)
        change = p.current("latest_change")
        if exp is not None and (planned is None or exp.hi > planned.hi):
            planned, evidence = exp, change or p.current("expected_cod")
        if exp is not None and orig is not None and exp.lo > orig.hi:
            slipped = True
            ms.add_event((change or p.current("expected_cod"))["valid_from"], "slipped")
        if statuses[p.id] in ("Denied", "Withdrawn"):
            denial = [w for w in cd.find_dates(text_of(change)) if w.role != "other"] if change else []
            ms.add_event(cm.first_day(denial[0]) if denial else p.current("status")["valid_from"], "denied")
            if exp is None:
                ms.status = statuses[p.id]
                cm._set_evidence(ms, change or p.current("status"))
                return True
        if change is not None and CONTESTED.search(text_of(change)):
            risk = True
        if evidence is None:
            evidence = change or p.current("status")
    cm._set_evidence(ms, evidence)
    if planned is not None:
        ms.planned = planned
        ms.status = "Slipped" if slipped else "At risk" if risk else "Projected"
    else:
        ms.status = "At risk" if risk or any(s == "Permit pending" for s in statuses.values()) else "Projected"
    return True


def gas_or_grid(site: SiteInputs, mode: str) -> cm.Milestone:
    ms = cm.Milestone("gas_or_grid", preds=["utility_agreement"])
    lines = [p for p in site.plants if p.is_supply_line]
    if _from_plants(ms, site, lines):
        _schedule_change_slips(ms, site, r"pipeline|gas|lateral|fuel supply")
        return ms
    status = site.current("power_status")
    if mode in ("grid", "hybrid") and ENERGIZED.search(text_of(status)):
        ms.status = "Done"
        cm._set_evidence(ms, status)
        return ms
    expected = site.current("expected_energization")
    text = text_of(expected)
    grid = [w for w in cd.targets(text) if cd.clause_kind(text, w) == "grid"]
    if grid:
        ms.planned, ms.status = cd.earliest(grid), "Projected"
        cm._set_evidence(ms, expected)
    else:
        for r in sorted(site.rows("rfs_statement"), key=lambda r: r["valid_from"], reverse=True):
            t = text_of(r)
            dates = [w for w in cd.targets(t) if re.search(r"energiz", cd.clause_of(t, w), re.I)]
            if dates:
                ms.planned, ms.status = cd.earliest(dates), "Projected"
                cm._set_evidence(ms, r)
                break
    if ms.status == "Not started" and UNRESOLVED.search(text_of(status)):
        ms.status = "At risk"
        cm._set_evidence(ms, status)
    return ms


def _schedule_change_slips(ms: cm.Milestone, site: SiteInputs, words: str) -> None:
    for r in site.rows("schedule_change"):
        t = text_of(r)
        if re.search(words, t, re.I) and PUSHED.search(t):
            ms.add_event(r["valid_from"], "slipped")
            if ms.status == "Projected":
                ms.status = "Slipped"


def onsite_generation(site: SiteInputs, mode: str) -> cm.Milestone:
    ms = cm.Milestone("onsite_generation", preds=["air_permit"])
    if mode == "onsite":
        ms.preds.append("gas_or_grid")  # an off-grid plant needs its fuel supply in service
    gen = [p for p in site.plants if not p.is_supply_line]
    text_row = site.current("onsite_generation")
    text = text_of(text_row)
    if re.search(r"^\s*none\b", text, re.I) or (mode == "grid" and not gen):
        ms.applicable = False
        return ms
    if mode in ("grid", "unknown") and not gen and not text:
        ms.applicable = False
        return ms
    # the site's own record of generation already in service wins over later expansions of its plants
    when = cm.done_when(re.sub(r"\bcompleted\b", "delivered", text), site.as_of) if "complet" in text else None
    if when is not None:
        ms.status, ms.done = "Done", when
        ms.add_event(cm.first_day(when), "done")
        cm._set_evidence(ms, text_row)
        return ms
    if _from_plants(ms, site, gen):
        return ms
    cm._set_evidence(ms, text_row)
    return ms


def air_permit(site: SiteInputs, onsite: cm.Milestone) -> cm.Milestone:
    ms = cm.Milestone("air_permit")
    if not onsite.applicable:
        ms.applicable = False
        return ms
    if onsite.is_done:
        ms.status = "Done"
        cm._set_evidence(ms, onsite.evidence, onsite.evidence_text)
        return ms
    gen = [p for p in site.plants if not p.is_supply_line]
    statuses = {text_of(p.current("status")) for p in gen}
    permits: list[Row] = [r for p in gen if (r := p.current("air_permit")) is not None]
    permits += [r for r in site.rows("air_permits")]
    if statuses & {"Permitted", "Operating"}:
        ms.status = "Done"
        cm._set_evidence(ms, next(p.current("status") for p in gen if text_of(p.current("status"))
                                  in ("Permitted", "Operating")))
        return ms
    if "Denied" in statuses or "Withdrawn" in statuses:
        ms.status = "Denied" if "Denied" in statuses else "Withdrawn"
    if permits:
        cm._set_evidence(ms, permits[0], _permit_text(permits[0]))
    future: list[cd.When] = []
    for e in site.events:
        title = e["title"] or ""
        if not PERMIT_WORDS.search(title):
            continue
        if e["kind"] == "catalyst":
            dates = [w for w in cd.targets(title) if PERMIT_WORDS.search(cd.clause_of(title, w))]
            day = cd.month_of(e["event_date"])
            future += [w for w in dates if cd.month_start(w.lo) > site.as_of]
            if e["event_date"] > site.as_of:
                future.append(cd.When(day, day, "day", 0, 0, e["event_date"].isoformat()))
            if PUSHED.search(title):
                ms.add_event(min(e["event_date"], business_day(e["learned_at"]), site.as_of), "slipped")
                ms.status = "Slipped"
            elif CONTESTED.search(title) and ms.status == "Not started":
                ms.status = "At risk"
            if ms.evidence is None or e["event_date"] >= ms.evidence.get("event_date", e["event_date"]):
                cm._set_evidence(ms, e)
    if future and ms.status not in ("Denied", "Withdrawn"):
        ms.planned = max(future, key=lambda w: w.hi)
        if ms.status == "Not started":
            ms.status = "Projected"
    elif ms.status == "Not started" and (permits or "Permit pending" in statuses):
        ms.status = "At risk"
        if ms.evidence is None:
            pending = next(p for p in gen if text_of(p.current("status")) == "Permit pending") if not permits else None
            if pending is not None:
                cm._set_evidence(ms, pending.current("status"), f"{pending.name}: permit pending")
    return ms


def _permit_text(row: Row) -> str:
    v = row["value"]
    items = v if isinstance(v, list) else [v]
    parts = [f"{i.get('agency', '')} {i.get('number', '')} ({i.get('type', '')}) status {i.get('status', 'n/d')}"
             for i in items if isinstance(i, dict)]
    return "; ".join(p.strip() for p in parts) or text_of(row)


def build(site: SiteInputs) -> tuple[list[cm.Milestone], str]:
    """All eight milestones in spec order, with predecessors set for the site's power mode."""
    mode = power_mode(site)
    land = cm.land_zoning(site)
    util = utility_agreement(site)
    grid = gas_or_grid(site, mode)
    gen = onsite_generation(site, mode)
    permit = air_permit(site, gen)
    hall = cm.first_hall(site)
    shell = cm.shells(site, hall.is_done)
    rent = cm.rent_start(site, hall)
    if mode == "onsite":
        hall.preds.append("onsite_generation")
    elif mode == "hybrid":
        hall.preds += ["gas_or_grid", "onsite_generation"]
    else:
        hall.preds.append("gas_or_grid")
        if gen.applicable:
            hall.preds.append("onsite_generation")
    by_key = {m.key: m for m in (land, permit, util, grid, gen, shell, hall, rent)}
    for m in by_key.values():
        m.preds = [p for p in m.preds if by_key[p].applicable]
    _implied_done(by_key)
    return [by_key[k] for k in cm.KEYS], mode


def _implied_done(by_key: dict[str, cm.Milestone]) -> None:
    """A milestone that is done needs its predecessors done (a delivered hall had land, shells and power):
    they become Done, undated, with the reason in their note."""
    for key in reversed(cm.KEYS):
        ms = by_key[key]
        if not ms.is_done:
            continue
        for p in ms.preds:
            pred = by_key[p]
            if pred.applicable and not pred.is_done and pred.status in ("Not started", "Projected", "At risk"):
                pred.status, pred.planned = "Done", None
                pred.note = f"implied: {key} is done"
