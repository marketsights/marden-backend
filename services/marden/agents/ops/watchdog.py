"""Watchdog (spec 22: "Each day, checks consistency: sums, contradictions between screens, stale sources, numbers
that moved without a cause" -> issues for the review queue). Daily on queue 'agents' (`agents.watchdog`), or
`python -m marden agents watchdog [--no-model]`.

Every check is code (`watch_checks`, `watch_staleness`, `watch_moves`); each finding becomes one review item
of kind 'issue' (ref_type 'watchdog:<check>', ref_id '<issue key>#<evidence hash>') with its evidence in the
payload. A model may only reword the titles (`watch_titles`), and a reworded title is kept only if it holds
exactly the code title's numbers and names.

Re-runs are idempotent. An open issue found again is refreshed (`last_seen`; new evidence replaces the old and
the ref_id follows it); an issue a person resolved or dismissed is raised again only when its evidence
changes; an open issue a check that ran this time no longer finds is closed as resolved by 'agent:watchdog'
(its evidence stays in the payload). A check that fails is reported and closes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from marden.agents.llm import Llm
from marden.agents.ops import runs, watch_titles
from marden.agents.ops.watch_checks import (
    Issue,
    payer_sums,
    reported_vs_filed,
    same_day_conflicts,
    screens,
    tranche_sums,
)
from marden.agents.ops.watch_moves import unexplained_moves
from marden.agents.ops.watch_staleness import dirty_backlog, missed_inputs, stale_sources
from marden.core import db
from marden.core.db import Conn
from marden.core.jsonutil import canonical_json, hash_parts, jsonb
from marden.readmodels.context import business_today

log = logging.getLogger(__name__)

BY = "agent:watchdog"
REASONS = {
    "sums": "a sum that does not add",
    "contradiction": "sources that contradict each other",
    "screens": "two screens showing the same value differently",
    "stale_source": "a source that missed its schedule",
    "stale_read_model": "a screen older than its inputs",
    "stale_model": "model outputs older than their inputs",
    "unexplained_move": "a number that moved without an input change",
}
Check = Callable[[Conn], list[Issue]]


def checks(today: date, now: datetime) -> dict[str, list[Check]]:
    """check group -> the functions that make its issues (stale_model issues come from dirty_backlog)."""
    moves_report: dict[str, Any] = {}

    def moves(conn: Conn) -> list[Issue]:
        found, rep = unexplained_moves(conn, today)
        moves_report.update(rep)
        return found

    moves.report = moves_report  # type: ignore[attr-defined]
    return {
        "sums": [tranche_sums, payer_sums],
        "contradiction": [reported_vs_filed, same_day_conflicts],
        "screens": [screens],
        "stale_source": [lambda c: stale_sources(c, now)],
        "stale_read_model": [lambda c: dirty_backlog(c, now), missed_inputs],
        "unexplained_move": [moves],
    }


def evidence_hash(issue: Issue) -> str:
    return hash_parts("watchdog", issue.check, issue.key, canonical_json(issue.extra.get("evidence_key",
                                                                                         issue.evidence)))[:12]


def payload(issue: Issue, code_title: str, writer: str, run_id: int, ehash: str) -> dict[str, Any]:
    return {"check": issue.check, "key": issue.key, "severity": issue.severity, "subject": issue.subject,
            "evidence": issue.evidence, "evidence_hash": ehash, "code_title": code_title, "title_writer": writer,
            "run_id": run_id, "last_seen": datetime.now(UTC).isoformat()}


def store(conn: Conn, issue: Issue, title: str, writer: str, run_id: int) -> tuple[str, int | None]:
    """Insert, refresh or skip one issue's review item; returns (what happened, review item id)."""
    ehash = evidence_hash(issue)
    ref_type, ref_id = f"watchdog:{issue.check}", f"{issue.key}#{ehash}"
    rows = conn.execute(
        """SELECT id, ref_id, status, payload FROM review_item WHERE kind = 'issue' AND ref_type = %s
           AND split_part(ref_id, '#', 1) = %s ORDER BY created_at DESC, id DESC""", (ref_type, issue.key)).fetchall()
    body = payload(issue, issue.title, writer, run_id, ehash)
    opened = next((r for r in rows if r["status"] == "open"), None)
    if opened is not None:
        if opened["ref_id"] == ref_id:
            first = (opened["payload"] or {}).get("first_seen")
            conn.execute("UPDATE review_item SET payload = %s WHERE id = %s",
                         (jsonb({**body, "first_seen": first, "title_writer": (opened["payload"] or {}).get(
                             "title_writer", writer)}), opened["id"]))
            return "still_open", int(opened["id"])
        conn.execute("UPDATE review_item SET ref_id = %s, title = %s, reason = %s, payload = %s WHERE id = %s",
                     (ref_id, title, f"watchdog: {REASONS[issue.check]} (evidence changed)",
                      jsonb({**body, "first_seen": (opened["payload"] or {}).get("first_seen"),
                             "previous_evidence": (opened["payload"] or {}).get("evidence")}), opened["id"]))
        return "updated", int(opened["id"])
    if any(r["ref_id"] == ref_id for r in rows):
        return "already_decided", None
    row = conn.execute(
        """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
           VALUES ('issue', %s, %s, %s, %s, %s) RETURNING id""",
        (ref_type, ref_id, title, f"watchdog: {REASONS[issue.check]}",
         jsonb({**body, "first_seen": body["last_seen"]}))).fetchone()
    assert row is not None
    return "new", int(row["id"])


def resolve_cleared(conn: Conn, groups: list[str], found: set[tuple[str, str]], run_id: int) -> list[int]:
    """Close the open issues of the checks that ran whose condition is gone."""
    rows = conn.execute(
        """SELECT id, ref_type, split_part(ref_id, '#', 1) AS key FROM review_item
           WHERE kind = 'issue' AND status = 'open' AND ref_type = ANY(%s)""",
        ([f"watchdog:{g}" for g in groups],)).fetchall()
    closed = []
    for r in rows:
        if (r["ref_type"].removeprefix("watchdog:"), r["key"]) in found:
            continue
        conn.execute(
            """UPDATE review_item SET status = 'approved', decided_by = %s, decided_at = now(),
                      payload = payload || jsonb_build_object('resolved', jsonb_build_object(
                          'by', %s::text, 'run_id', %s::bigint, 'reason', 'the check no longer finds it'))
               WHERE id = %s AND status = 'open'""", (BY, BY, run_id, r["id"]))
        conn.execute("INSERT INTO audit_log (user_id, action, target, meta) VALUES (%s, 'watchdog.resolve', %s, %s)",
                     (BY, f"review_item:{r['id']}", jsonb({"run_id": run_id, "check": r["ref_type"]})))
        closed.append(int(r["id"]))
    return closed


def new_titles(conn: Conn, issues: list[Issue]) -> list[Issue]:
    """The issues whose item would be created or changed (only these are worth a model's rewording)."""
    out = []
    for i in issues:
        ref_id = f"{i.key}#{evidence_hash(i)}"
        hit = conn.execute("SELECT 1 FROM review_item WHERE kind = 'issue' AND ref_type = %s AND ref_id = %s",
                           (f"watchdog:{i.check}", ref_id)).fetchone()
        if hit is None:
            out.append(i)
    return out


def run(*, today: date | None = None, now: datetime | None = None, llm: Llm | None = None, use_model: bool = True,
        trigger: str = "manual") -> dict[str, Any]:
    """One watchdog run (module docstring); returns the report stored on agent_ops_run."""
    from marden.plugins import load_plugins

    load_plugins(("marden.models", "marden.collectors"))  # model inputs (triggers) and collector schedules
    today, now = today or business_today(), now or datetime.now(UTC)
    run_id = runs.start("watchdog", trigger=trigger)
    report: dict[str, Any] = {"run_id": run_id, "today": today.isoformat(), "checks": {}}
    try:
        groups = checks(today, now)
        issues: list[Issue] = []
        ran: list[str] = []
        for group, fns in groups.items():
            entry: dict[str, Any] = {"found": 0}
            try:
                with db.transaction() as conn:
                    found = [i for fn in fns for i in fn(conn)]
                issues += found
                entry["found"] = len(found)
                ran.append(group)
                if group == "unexplained_move":
                    entry["models"] = fns[0].report  # type: ignore[attr-defined]
            except Exception as exc:  # one broken check must not stop the others, nor close their issues
                log.exception("watchdog check %s failed", group)
                entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            report["checks"][group] = entry
        if "stale_read_model" in ran:  # the backlog check also makes the stale_model issues
            ran.append("stale_model")
        model, status = runs.writer_llm(llm) if use_model else (None, None)
        with db.transaction() as conn:
            fresh = new_titles(conn, issues)
        items = [{"id": f"{i.check}|{i.key}", "check": REASONS[i.check], "title": i.title} for i in fresh]
        phrased, title_report = watch_titles.phrase(model, items)
        report["titles"] = title_report
        report["model_status"] = (status.kind if model is not None else
                                  "not_used" if not use_model else "not_configured")
        outcomes: dict[str, int] = {}
        listed = []
        with db.transaction() as conn:
            for i in issues:
                title = phrased.get(f"{i.check}|{i.key}", i.title)
                what, item_id = store(conn, i, title, "model" if title != i.title else "code", run_id)
                outcomes[what] = outcomes.get(what, 0) + 1
                listed.append({"check": i.check, "key": i.key, "title": title, "outcome": what,
                               "review_item_id": item_id, "severity": i.severity})
            resolved = resolve_cleared(conn, ran, {(i.check, i.key) for i in issues}, run_id)
        outcomes["resolved"] = len(resolved)
        report |= {"issues": outcomes, "items": listed[:200], "resolved_items": resolved[:200]}
        failed = [g for g, e in report["checks"].items() if "error" in e]
        runs.finish(run_id, "partial" if failed or title_report.get("error") else "ok", report=report,
                    model_status=report["model_status"])
        return report
    except Exception as exc:
        runs.finish(run_id, "failed", report=report, model_status="not_used", detail=f"{type(exc).__name__}: {exc}")
        raise
