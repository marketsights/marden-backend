"""`python -m marden agents research | watchdog | note`: the operations agents from a terminal (docs/agents.md).

    agents research [--max-queries N] [--max-ferc N] [--max-docs N] [--no-collectors] [--plan]
    agents watchdog [--no-model]
    agents note [--week YYYY-MM-DD] [--redraft]

Each prints its run report (also stored on agent_ops_run). Exit 0 on a finished run, 3 when part of it could
not run (a refused search, a failed check, a model error).
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from marden import cli
from marden.agents import commands as _agents  # noqa: F401  (declares the 'agents' group first)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _status(report: dict[str, Any]) -> int:
    from marden.core import db

    with db.transaction() as conn:
        row = conn.execute("SELECT status FROM agent_ops_run WHERE id = %s", (report["run_id"],)).fetchone()
    return 0 if row is not None and row["status"] == "ok" else 3


def _research_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--max-queries", type=int, help="EDGAR full-text searches this run (default 120)")
    p.add_argument("--max-ferc", type=int, help="eLibrary text searches this run (default 20)")
    p.add_argument("--max-docs", type=int, help="new documents fetched this run (default 25)")
    p.add_argument("--no-collectors", action="store_true", help="do not run a collector that missed its cadence")
    p.add_argument("--plan", action="store_true", help="print the gaps and planned searches only (no network)")


@cli.action("agents", "research", help="Search for fields still not disclosed; queue what is found",
            configure=_research_args)
def research_command(args: argparse.Namespace) -> int:
    if args.plan:
        from marden.agents.ops import gaps
        from marden.agents.ops.research_plan import plan
        from marden.core import db

        with db.transaction() as conn:
            found = gaps.collect(conn)
        p = plan(found)
        _print({"gaps": len(found), "edgar": [q.query for q in p.edgar[:40]], "edgar_total": len(p.edgar),
                "ferc": [q.query for q in p.ferc], "collector_items": len(p.collector_gaps),
                "not_searched": [{"item": n["item"], "why": n["why"], "subjects": len(n["subjects"])}
                                 for n in p.not_searched]})
        return 0
    from marden.agents.ops.researcher import run

    report = run(max_queries=args.max_queries, max_ferc=args.max_ferc, max_docs=args.max_docs,
                 collectors=not args.no_collectors)
    _print(report)
    return _status(report)


def _watchdog_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-model", action="store_true", help="keep every code-written title (no model call)")


@cli.action("agents", "watchdog", help="Run the consistency checks; issues go to the review queue",
            configure=_watchdog_args)
def watchdog_command(args: argparse.Namespace) -> int:
    from marden.agents.ops.watchdog import run

    report = run(use_model=not args.no_model)
    _print(report)
    return _status(report)


def _note_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--week", type=date.fromisoformat, help="any day of the week (default: this week)")
    p.add_argument("--redraft", action="store_true", help="draft again after a rejection, same figures")


@cli.action("agents", "note", help="Draft the weekly note from the risk list's figures (waits for approval)",
            configure=_note_args)
def note_command(args: argparse.Namespace) -> int:
    from marden.agents.ops.note import draft

    report = draft(args.week, redraft=args.redraft)
    _print(report)
    return _status(report)
