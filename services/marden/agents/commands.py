"""`python -m marden agents <action>`: feed documents to the agents and run them from a terminal.

    agents config                          backend in use, models, budgets, tokens used today
    agents ingest <file|dir|url> [--site S] [--instrument I] [--entity E] [--filed|--reported] [--run]
    agents run [--doc ID ...] [--limit N] [--threads N]     sweep, then process queued jobs (or these documents)
    agents watch                           LISTEN for new documents / uploads / decisions and process them
    agents status [--job ID]               jobs by state, recent jobs, tokens
    agents review list | approve ID | reject ID --reason R | edit ID --value V --quote Q
    agents backfill [--limit N]            old documents through the Message Batches API (half price)
    agents eval [--area A] [--recorded]    precision / recall per area on the gold set
    agents recheck [--doc ID]              re-run the code checks on published / waiting agent facts (a
                                           published one that fails is withdrawn to review)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from marden import cli

cli.group("agents", help="Document agents: ingest, run, review, eval (docs/agents.md)")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


@cli.action("agents", "config", help="Backend, models, budgets and tokens used today")
def config_command(args: argparse.Namespace) -> int:
    from marden.agents.config import AgentSettings, detect_backend
    from marden.agents.llm import day_tokens_used

    settings = AgentSettings.from_env()
    status = detect_backend(settings)
    _print({"backend": status.kind, "detail": status.detail, "models": settings.models, "effort": settings.effort,
            "run_tokens": settings.run_tokens, "day_tokens": settings.day_tokens,
            "used_today": day_tokens_used(), "source_cap": settings.source_cap})
    return 0 if status.configured else 3


def _links(args: argparse.Namespace) -> dict[str, str]:
    return {k: v for k, v in (("site", args.site), ("instrument", args.instrument), ("entity", args.entity)) if v}


def _ingest_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", help="a file, a directory or an http(s) URL")
    p.add_argument("--site")
    p.add_argument("--instrument")
    p.add_argument("--entity")
    p.add_argument("--title")
    filed = p.add_mutually_exclusive_group()
    filed.add_argument("--filed", dest="filed", action="store_true", default=None, help="an official filing (tag F)")
    filed.add_argument("--reported", dest="filed", action="store_false", help="press or other report (tag R)")
    p.add_argument("--run", action="store_true", help="process the documents now")


@cli.action("agents", "ingest", help="Store a file, directory or URL raw and queue it", configure=_ingest_args)
def ingest_command(args: argparse.Namespace) -> int:
    from marden.agents.intake import ingest_path, ingest_url
    from marden.agents.pipeline import enqueue_document
    from marden.agents.runner import sweep, work

    links = _links(args)
    if args.target.startswith(("http://", "https://")):
        docs = [ingest_url(args.target, links=links, filed=args.filed, title=args.title)]
    else:
        docs = ingest_path(Path(args.target), links=links, filed=args.filed, title=args.title)
    status = sweep()
    jobs = []
    if status["status"] == "ok":
        jobs = [enqueue_document(d.id, priority=70, reason="staff upload", links=links or None) for d in docs]
    _print({"documents": [{"id": d.id, "new": d.was_new, "title": d.title, "bytes": d.bytes} for d in docs],
            "agents": status["status"], "detail": status["detail"], "jobs": jobs})
    if args.run and jobs:
        _print(work(job_ids=jobs))
    return 0 if status["status"] == "ok" else 3


def _run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--doc", type=int, action="append", help="document id to process now (repeatable)")
    p.add_argument("--limit", type=int, default=None, help="at most this many jobs")
    p.add_argument("--threads", type=int, default=None)


@cli.action("agents", "run", help="Sweep, then process queued jobs (or the given documents)", configure=_run_args)
def run_command(args: argparse.Namespace) -> int:
    from marden.agents.pipeline import enqueue_document
    from marden.agents.runner import sweep, work

    status = sweep()
    if status["status"] != "ok":
        _print(status)
        return 3
    job_ids = [enqueue_document(d, priority=70, reason="manual") for d in args.doc] if args.doc else None
    out = work(max_jobs=args.limit, threads=args.threads, job_ids=job_ids)
    _print(out)
    return 0


@cli.action("agents", "watch", help="LISTEN on marden_agents and process new work as it arrives")
def watch_command(args: argparse.Namespace) -> int:
    from marden.agents.runner import sweep, work
    from marden.core import db

    conn = db.connect(autocommit=True)
    conn.execute("LISTEN marden_agents")
    print("listening on marden_agents (Ctrl-C to stop)")
    try:
        while True:
            _print({"sweep": sweep(), "work": work()})
            for _ in conn.notifies(timeout=60, stop_after=1):
                pass
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()


def _status_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--job", type=int)


@cli.action("agents", "status", help="Jobs by state, recent jobs and tokens", configure=_status_args)
def status_command(args: argparse.Namespace) -> int:
    from marden.agents.llm import usage_summary
    from marden.core import db

    with db.transaction() as conn:
        if args.job:
            job = conn.execute("SELECT * FROM agent_job WHERE id = %s", (args.job,)).fetchone()
            cands = conn.execute(
                """SELECT id, area, field, subject_mention, subject_type, subject_id, value, verdict, outcome,
                          outcome_reason FROM agent_candidate WHERE job_id = %s ORDER BY id""", (args.job,)).fetchall()
            _print({"job": job, "candidates": cands, "usage": usage_summary(job_id=args.job)})
            return 0
        states = conn.execute("SELECT state, count(*) AS n FROM agent_job GROUP BY 1 ORDER BY 1").fetchall()
        recent = conn.execute(
            """SELECT j.id, j.document_id, j.state, j.stage, j.areas, j.stats, d.title FROM agent_job j
               JOIN document d ON d.id = j.document_id ORDER BY j.updated_at DESC LIMIT 15""").fetchall()
        status = conn.execute("SELECT * FROM agent_status").fetchone()
    _print({"status": status, "jobs_by_state": states, "recent": recent, "usage": usage_summary()["total"]})
    return 0


def _review_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("verb", choices=["list", "approve", "reject", "edit"])
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--reason")
    p.add_argument("--value", help="JSON value for edit")
    p.add_argument("--quote")
    p.add_argument("--by", default="cli")


@cli.action("agents", "review", help="The review queue: list, approve, reject, edit", configure=_review_args)
def review_command(args: argparse.Namespace) -> int:
    from marden.agents.review import decide
    from marden.core import db

    if args.verb == "list":
        with db.transaction() as conn:
            rows = conn.execute(
                """SELECT id, kind, ref_type, ref_id, title, reason FROM review_item WHERE status = 'open'
                   ORDER BY created_at DESC LIMIT 50""").fetchall()
        _print(rows)
        return 0
    if args.id is None:
        print("a review item id is required")
        return 2
    edit = None
    if args.verb == "edit":
        edit = {"value": json.loads(args.value) if args.value else None, "quote": args.quote}
    result = decide(args.id, args.verb, args.by, reason=args.reason, edit=edit)
    _print(result)
    return 0 if result.get("ok") else 1


def _backfill_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, default=500)


@cli.action("agents", "backfill", help="Process old documents through the Batch API", configure=_backfill_args)
def backfill_command(args: argparse.Namespace) -> int:
    from marden.agents.batch import backfill

    out = backfill(args.limit)
    _print(out)
    return 0 if out["status"] == "ok" else 3


def _eval_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--area", action="append")
    p.add_argument("--recorded", action="store_true", help="replay recorded model answers (no model calls)")
    p.add_argument("--record", action="store_true", help="save the live answers as the recorded set")


@cli.action("agents", "eval", help="Precision and recall per area on the gold set", configure=_eval_args)
def eval_command(args: argparse.Namespace) -> int:
    from marden.agents.evals import run_eval

    report = run_eval(areas=args.area, recorded=args.recorded, record=args.record)
    _print(report)
    return 0 if report.get("status") == "ok" else 3


def _recheck_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--doc", type=int, help="only this document's candidates")


@cli.action("agents", "recheck", help="Re-run the code checks on published and waiting agent facts",
            configure=_recheck_args)
def recheck_command(args: argparse.Namespace) -> int:
    from marden.agents.publish import recheck

    _print(recheck(document_id=args.doc))
    return 0
