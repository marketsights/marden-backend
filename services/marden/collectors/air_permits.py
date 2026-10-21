"""State air permits for our on-site power plants (spec 15 'Air permit card', spec 20): one collector, one adapter
per state permit system that Marden may read, the conservative match of air_permits_match, the writes of
air_permits_store, and a run report per state.

States and systems (checked 2026-10-21; `air_permits_systems` has every state's system, terms and status):

- TX: TCEQ Central Registry on data.texas.gov + TCEQ Records Online (air_permits_tx). TCEQ's own permit query
  hosts are robots-disallowed and not used.
- NM: NMED Air Quality Bureau public notices and the Office of Public Facilitation docket (air_permits_nm).
- IA: Iowa DNR Air Quality construction permit search (AQRR) (air_permits_ia).
- LA: LDEQ public notice listing (air_permits_la); LDEQ's EDMS is robots-disallowed and not used.
- Other states with plants: the system, why it is not collected (robots, bot challenge, TLS, no public search
  of permits) or that no adapter exists yet, in the report and in docs/collectors.md.

Each adapter run is one heartbeat on the adapter's discovery source (a second source it reads, such as Records
Online, gets its own). A refusal (401/403/429, challenge page, robots) stops that adapter, is recorded on its
source and writes nothing in place of the record. Matched permits become facts and events on the plant;
unmatched candidates (with the reason each failed) are only in the run report (source_run.meta and the CLI).

Cadence: weekly, Wednesdays 12:29 UTC (`collect.air_permits`). `python -m marden collect air_permits [--state TX]`.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from marden import cli
from marden.collectors import air_permits_ia, air_permits_la, air_permits_nm, air_permits_tx
from marden.collectors.air_permits_match import Match, PlantProfile, candidate, load_profiles, match_all, report_row
from marden.collectors.air_permits_store import write_plant
from marden.collectors.air_permits_systems import SYSTEMS, blocked_specs, systems_for
from marden.collectors.rates_common import RunStats, collect, print_stats
from marden.core import db
from marden.core.http import Fetcher
from marden.core.queue import app
from marden.core.sources import SourceSpec, upsert_spec

CRON_UTC = "29 12 * * 3"  # Wednesdays 12:29 UTC
Matcher = Callable[[list[Any]], list[Match]]
Runner = Callable[[list[PlantProfile], Fetcher, RunStats, dict[str, Any], Matcher], list[Match]]
ADAPTERS: dict[str, tuple[SourceSpec, Runner]] = {
    "TX": (air_permits_tx.SOURCE_CR, air_permits_tx.run_tx),
    "NM": (air_permits_nm.SOURCE, air_permits_nm.run_nm),
    "IA": (air_permits_ia.SOURCE, air_permits_ia.run_ia),
    "LA": (air_permits_la.SOURCE, air_permits_la.run_la),
}


def make_fetcher() -> Fetcher:
    """No retries: a 429 or a server error ends the adapter's run like a refusal."""
    return Fetcher(max_retries=0, timeout=90.0)


def summarize(matches: list[Match], written: list[dict[str, Any]], plants: list[PlantProfile]) -> dict[str, Any]:
    candidates = [m for m in matches if candidate(m)]
    matched = [m for m in matches if m.plant is not None]
    return {
        "plants": len(plants),
        "facilities_found": len(candidates),
        "permits_found": sum(len(m.facility.permits) for m in candidates),
        "search_results_without_evidence": len(matches) - len(candidates),
        "matched_facilities": len(matched),
        "matched_plants": sorted({m.plant.id for m in matched if m.plant}),
        "matched_permits": sum(len(m.facility.permits) for m in matched),
        "matched": [report_row(m) for m in matched],
        "unmatched": [report_row(m) for m in candidates if m.plant is None],
        "written": written,
    }


def run_state(state: str, plants: list[PlantProfile], fetcher: Fetcher) -> dict[str, Any]:
    spec, runner = ADAPTERS[state]

    def body(stats: RunStats) -> None:
        info: dict[str, Any] = {}
        stats.extra["adapter"] = info
        matches = runner(plants, fetcher, stats, info, lambda found: match_all(found, plants))
        by_plant: dict[str, list[Match]] = defaultdict(list)
        for m in matches:
            if m.plant is not None:
                by_plant[m.plant.id].append(m)
        written = []
        with db.transaction() as conn:
            for plant_id in sorted(by_plant):
                written.append(write_plant(conn, plant_id, by_plant[plant_id], stats))
        stats.extra["coverage"] = summarize(matches, written, plants)

    return collect(spec, body)


def run(*, states: list[str] | None = None, fetcher: Fetcher | None = None) -> dict[str, Any]:
    own = fetcher is None
    fetcher = fetcher or make_fetcher()
    try:
        for spec in blocked_specs():  # recorded on their source rows; never requested
            upsert_spec(spec)
        with db.transaction() as conn:
            profiles = load_profiles(conn, states)
        by_state: dict[str, list[PlantProfile]] = defaultdict(list)
        for p in profiles:
            by_state[p.state].append(p)
        report: dict[str, Any] = {"ok": True, "states": {}}
        for state in sorted(by_state):
            entry: dict[str, Any] = {"plants": len(by_state[state]), "systems": systems_for(state)}
            if state in ADAPTERS:
                result = run_state(state, by_state[state], fetcher)
                entry["run"] = result
                report["ok"] &= bool(result.get("ok"))
            report["states"][state] = entry
        report["totals"] = totals(report["states"])
        return report
    finally:
        if own:
            fetcher.close()


def totals(states: dict[str, Any]) -> dict[str, Any]:
    out = {"plants": 0, "states_with_adapter": 0, "permits_found": 0, "matched_plants": 0, "matched_permits": 0,
           "unmatched_candidates": 0, "facts_new": 0, "events_new": 0}
    for entry in states.values():
        out["plants"] += entry["plants"]
        run_ = entry.get("run") or {}
        cov = run_.get("coverage") or {}
        if run_:
            out["states_with_adapter"] += 1
        out["permits_found"] += cov.get("permits_found", 0)
        out["matched_plants"] += len(cov.get("matched_plants", []))
        out["matched_permits"] += cov.get("matched_permits", 0)
        out["unmatched_candidates"] += len(cov.get("unmatched", []))
        out["facts_new"] += run_.get("facts_new", 0)
        out["events_new"] += run_.get("events_new", 0)
    return out


@app.periodic(cron=CRON_UTC, periodic_id="weekly_wed")
@app.task(name="collect.air_permits", queue="collect", queueing_lock="collect.air_permits", lock="collect.air_permits")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


def _configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--state", action="append", help="two-letter state; repeat for several (default: every state)")


@cli.action("collect", "air_permits", configure=_configure,
            help="State air permits for our power plants (TX TCEQ, NM NMED, IA DNR, LA LDEQ)")
def collect_cli(args: argparse.Namespace) -> int:
    states = [s.upper() for s in args.state] if getattr(args, "state", None) else None
    return print_stats(run(states=states))


__all__ = ["SYSTEMS", "run", "run_state"]
