"""Watchdog checks on time: stale sources, read models and model outputs older than their inputs.

    stale_source      a source whose last good run is older than its schedule allows: it missed its last two
                      scheduled runs (the collector's periodic task crons, `collect.<source id>`), or, for a
                      source without its own task, more than twice its cadence; a source that never ran well
                      once two cadences have passed since it was declared. Blocked sources are known and left
                      out; manual sources have no cadence.
    stale_read_model  dirty marks the 30-second rebuild has not consumed for 10 minutes (one issue per view);
                      a Site detail or Instrument detail snapshot built before a fact or event it reads was
                      published, with no dirty mark waiting (a missed dependency)
    stale_model       model dirty marks the per-minute model runner has not consumed for an hour (per model)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from croniter import croniter

from marden.agents.ops.watch_checks import Issue
from marden.core.db import Conn

READ_MODEL_GRACE = timedelta(minutes=10)
MODEL_GRACE = timedelta(hours=1)
CRON_GRACE = timedelta(minutes=30)  # a scheduled run may start late and take a while


def source_crons() -> dict[str, list[str]]:
    """source id -> the crons of its periodic task `collect.<id>` (tasks registered by the loaded plugins)."""
    from marden.core.queue import app

    out: dict[str, list[str]] = {}
    for (task_name, _pid), periodic in app.periodic_registry.periodic_tasks.items():
        if task_name.startswith("collect."):
            out.setdefault(task_name.removeprefix("collect."), []).append(periodic.cron)
    return out


def expected_by(crons: list[str], now: datetime, runs: int = 2) -> datetime | None:
    """The time of the `runs`-th most recent scheduled run before now (less a grace for late starts)."""
    fires: list[datetime] = []
    for cron in crons:
        fields = cron.split()
        if len(fields) != 5:  # a seconds field ('* * * * * */30'): the cadence check covers it
            return None
        it = croniter(cron, now - CRON_GRACE)
        fires += [it.get_prev(datetime) for _ in range(runs)]
    fires = sorted(set(fires), reverse=True)
    return fires[runs - 1] if len(fires) >= runs else None


def stale_sources(conn: Conn, now: datetime | None = None, crons: dict[str, list[str]] | None = None) -> list[Issue]:
    now = now or datetime.now(UTC)
    crons = source_crons() if crons is None else crons
    rows = conn.execute(
        """SELECT id, name, cadence_minutes, last_ok_at, last_error_at, last_error, created_at FROM source
           WHERE NOT blocked AND cadence_minutes IS NOT NULL ORDER BY id""").fetchall()
    out = []
    for r in rows:
        cadence = timedelta(minutes=r["cadence_minutes"])
        due = expected_by(crons[r["id"]], now) if r["id"] in crons else None
        rule = "missed its last two scheduled runs" if due is not None else "more than twice its cadence"
        due = due or (now - 2 * cadence)
        if r["last_ok_at"] is None:
            if r["created_at"] > now - 2 * cadence:
                continue
            title = f"Source {r['id']} has never run successfully"
        elif r["last_ok_at"] >= due:
            continue
        else:
            title = f"Source {r['id']} is stale: last good run {r['last_ok_at']:%Y-%m-%d %H:%M} UTC"
        ev = {"source": r["id"], "name": r["name"], "cadence_minutes": r["cadence_minutes"],
              "schedule": crons.get(r["id"]), "rule": rule, "expected_by": due.isoformat(),
              "last_ok_at": r["last_ok_at"].isoformat() if r["last_ok_at"] else None,
              "last_error_at": r["last_error_at"].isoformat() if r["last_error_at"] else None,
              "last_error": (r["last_error"] or "")[:300] or None}
        out.append(Issue("stale_source", f"source:{r['id']}", title, ev, None,
                         "high" if r["cadence_minutes"] <= 1440 else "medium",
                         {"evidence_key": [r["id"], ev["last_ok_at"], ev["last_error_at"]]}))
    return out


def dirty_backlog(conn: Conn, now: datetime | None = None) -> list[Issue]:
    now = now or datetime.now(UTC)
    out = []
    rows = conn.execute(
        """SELECT view, count(*) AS n, min(marked_at) AS oldest, (array_agg(key ORDER BY marked_at))[1:5] AS keys
           FROM read_model_dirty WHERE marked_at < %s GROUP BY view ORDER BY view""",
        (now - READ_MODEL_GRACE,)).fetchall()
    for r in rows:
        out.append(Issue("stale_read_model", f"view:{r['view']}:backlog",
                         f"Read model {r['view']}: {r['n']} key(s) waiting to rebuild since "
                         f"{r['oldest']:%Y-%m-%d %H:%M} UTC",
                         {"view": r["view"], "waiting": r["n"], "oldest_mark": r["oldest"].isoformat(),
                          "keys": r["keys"]}, None, "high",
                         {"evidence_key": [r["view"], r["oldest"].isoformat()]}))
    rows = conn.execute(
        """SELECT model, count(*) AS n, min(marked_at) AS oldest, (array_agg(key ORDER BY marked_at))[1:5] AS keys
           FROM model_dirty WHERE marked_at < %s GROUP BY model ORDER BY model""", (now - MODEL_GRACE,)).fetchall()
    for r in rows:
        out.append(Issue("stale_model", f"model:{r['model']}:backlog",
                         f"Model {r['model']}: {r['n']} input change(s) not run since {r['oldest']:%Y-%m-%d %H:%M} UTC",
                         {"model": r["model"], "waiting": r["n"], "oldest_mark": r["oldest"].isoformat(),
                          "keys": r["keys"]}, None, "medium",
                         {"evidence_key": [r["model"], r["oldest"].isoformat()]}))
    return out


MISSED_SQL = """
    SELECT r.key, r.built_at, x.at, x.what FROM latest r
    CROSS JOIN LATERAL (
      SELECT max(at) AS at, (array_agg(what ORDER BY at DESC))[1] AS what FROM (
        SELECT f.learned_at AS at, 'fact ' || f.field || ' #' || f.id AS what FROM fact f
          WHERE f.subject_type = %(kind)s AND f.subject_id = r.key AND f.status = 'published'
            AND f.valid_from <= r.as_of
        UNION ALL
        SELECT e.learned_at, 'event ' || e.kind || ' #' || e.id FROM event e
          WHERE e.subject_type = %(kind)s AND e.subject_id = r.key AND e.status = 'published'
            AND e.event_date <= r.as_of) i) x
    WHERE x.at > r.built_at"""
DETAIL_VIEWS = {"site_detail": "site", "instrument_detail": "instrument"}


def missed_inputs(conn: Conn) -> list[Issue]:
    """Detail snapshots built before a fact or event they read was published, with no dirty mark waiting (the
    writers mark every dependent view when they publish, so this is a missed dependency). Model outputs are
    left out: a model marks the views only when its output changed."""
    out = []
    for view, kind in DETAIL_VIEWS.items():
        rows = conn.execute(
            f"""WITH latest AS (SELECT DISTINCT ON (key) key, as_of, built_at FROM read_model WHERE view = %(view)s
                                ORDER BY key, as_of DESC)
                SELECT * FROM ({MISSED_SQL}) m
                WHERE NOT EXISTS (SELECT 1 FROM read_model_dirty d WHERE d.view = %(view)s AND d.key = m.key)
                ORDER BY m.key""", {"view": view, "kind": kind}).fetchall()
        for r in rows:
            out.append(Issue("stale_read_model", f"{view}:{r['key']}:missed_input",
                             f"{view} {r['key']} was built before {r['what']} and is not marked for a rebuild",
                             {"view": view, "key": r["key"], "built_at": r["built_at"].isoformat(),
                              "input": r["what"], "input_at": r["at"].isoformat()},
                             {"type": kind, "id": r["key"]}, "medium",
                             {"evidence_key": [view, r["key"], r["what"]]}))
    return out


def summary(issues: list[Issue]) -> dict[str, Any]:
    return {"issues": len(issues)}
