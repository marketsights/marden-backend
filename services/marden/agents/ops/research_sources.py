"""Gaps an existing collector covers: the researcher checks that collector's source and, when it has missed
its cadence, runs it (it stores what it fetches raw and queues its documents itself).

    'Permits: Air permits'           collect.air_permits, per state with an adapter (TX, NM, IA, LA); other
                                     states: why the state's system is not read (air_permits_systems)
    'Construction: Satellite change' collect.sentinel2 (every pass); reported, never run from here
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from marden.agents.ops.gaps import Gap
from marden.core import db

log = logging.getLogger(__name__)

STATE = re.compile(r"\b[A-Z]{2}\b")


def _source(source_id: str) -> dict[str, Any] | None:
    with db.transaction() as conn:
        return conn.execute("SELECT id, cadence_minutes, last_ok_at, blocked FROM source WHERE id = %s",
                            (source_id,)).fetchone()


def missed_cadence(src: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if src is None or src["blocked"] or not src["cadence_minutes"]:
        return False
    if src["last_ok_at"] is None:
        return True
    return (now or datetime.now(UTC)) - src["last_ok_at"] > timedelta(minutes=src["cadence_minutes"])


def _site_states(site_ids: set[str]) -> dict[str, list[str]]:
    with db.transaction() as conn:
        rows = conn.execute("SELECT slug, state FROM site WHERE slug = ANY(%s)", (sorted(site_ids),)).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        for st in STATE.findall(r["state"] or ""):
            out.setdefault(st, []).append(r["slug"])
    return out


def check_collectors(gaps: list[Gap], *, run: bool = True) -> list[dict[str, Any]]:
    from marden.collectors import air_permits
    from marden.collectors.sentinel2 import SOURCE_ID as SENTINEL

    out: list[dict[str, Any]] = []
    air = {g.subject.id for g in gaps if g.plan.collector == "air_permits" and g.subject.type == "site"}
    for state, slugs in sorted(_site_states(air).items()):
        entry: dict[str, Any] = {"item": "Permits: Air permits", "collector": "collect.air_permits", "state": state,
                                 "subjects": sorted(slugs)}
        adapter = air_permits.ADAPTERS.get(state)
        if adapter is None:
            entry["action"] = "not covered: no adapter for this state's permit system"
            entry["systems"] = air_permits.systems_for(state)
        else:
            src = _source(adapter[0].id)
            entry["source"] = adapter[0].id
            entry["last_ok"] = src["last_ok_at"].isoformat() if src and src["last_ok_at"] else None
            if not missed_cadence(src):
                entry["action"] = "covered: the collector ran within its cadence"
            elif not run:
                entry["action"] = "the collector missed its cadence (not run: --no-collectors)"
            else:
                try:
                    result = air_permits.run(states=[state])
                    entry["action"] = "ran the collector (it had missed its cadence)"
                    entry["run"] = result.get("totals")
                except Exception as exc:  # recorded on its own source by the collector's heartbeat
                    log.exception("researcher: air permits for %s failed", state)
                    entry["action"] = f"ran the collector: failed ({type(exc).__name__})"
        out.append(entry)
    sat = sorted({g.subject.id for g in gaps if g.plan.collector == "sentinel2"})
    if sat:
        src = _source(SENTINEL)
        out.append({"item": "Construction: Satellite change", "collector": "collect.sentinel2", "source": SENTINEL,
                    "subjects": sat, "last_ok": src["last_ok_at"].isoformat() if src and src["last_ok_at"] else None,
                    "action": ("the collector missed its cadence (runs on its own schedule)" if missed_cadence(src)
                               else "covered: the collector ran within its cadence")})
    return out
