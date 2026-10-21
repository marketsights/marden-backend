"""Built-in CLI commands: worker, rebuild, sources."""

from __future__ import annotations

import argparse

from marden import cli


def _worker_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--queue", action="append", dest="queues", help="queue to work (repeatable; default all)")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--once", action="store_true", help="exit when the queues are empty")


@cli.command("worker", help="Run the Procrastinate worker (tasks and periodic schedules)", configure=_worker_args)
def worker_command(args: argparse.Namespace) -> int:
    from marden.worker import run_worker

    run_worker(queues=args.queues, concurrency=args.concurrency, wait=not args.once)
    return 0


def _rebuild_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", help="a view name, 'all' (every view) or 'dirty' (only dirty keys)")


@cli.command("rebuild", help="Rebuild read models: <view|all|dirty>", configure=_rebuild_args)
def rebuild_command(args: argparse.Namespace) -> int:
    from marden.readmodels import rebuild_all, rebuild_dirty, rebuild_every_view, registered_views

    if args.target == "dirty":
        stats = {"dirty": rebuild_dirty()}
    elif args.target == "all":
        stats = rebuild_every_view()
        if not stats:
            print("no read-model builders are registered")
    elif args.target in registered_views():
        stats = {args.target: rebuild_all(args.target)}
    else:
        print(f"unknown view {args.target!r}; registered: {', '.join(registered_views()) or '(none)'}")
        return 2
    for view, s in stats.items():
        print(f"{view}: {s.summary()}")
        for err_view, key, err in s.errors:
            print(f"  error {err_view}/{key}: {err}")
    return 1 if any(s.errors for s in stats.values()) else 0


def _sources_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sync", action="store_true", help="upsert every declared source into the source table")


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")  # type: ignore[union-attr]
    return str(value)


@cli.command("sources", help="List declared sources and their heartbeat state", configure=_sources_args)
def sources_command(args: argparse.Namespace) -> int:
    from marden.core import sources

    if args.sync:
        rows = sources.sync_registered()
        print(f"synced {len(rows)} declared sources")
    declared = {s.id: s for s in sources.registered_sources()}
    in_db = {r["id"]: r for r in sources.list_sources()}
    ids = sorted(set(declared) | set(in_db))
    if not ids:
        print("no sources declared or stored yet")
        return 0
    header = ("id", "kind", "cadence_min", "blocked", "declared", "in_db", "last_ok_at", "last_error_at")
    lines = [header]
    for sid in ids:
        spec, row = declared.get(sid), in_db.get(sid) or {}
        kind = spec.kind if spec else row.get("kind")
        cadence = spec.cadence_minutes if spec else row.get("cadence_minutes")
        blocked = spec.blocked if spec else row.get("blocked")
        lines.append((
            sid, _fmt(kind), _fmt(cadence), "yes" if blocked else "no", "yes" if spec else "no",
            "yes" if row else "no", _fmt(row.get("last_ok_at")), _fmt(row.get("last_error_at")),
        ))
    widths = [max(len(str(line[i])) for line in lines) for i in range(len(header))]
    for line in lines:
        print("  ".join(str(cell).ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip())
    for sid in ids:
        row = in_db.get(sid) or {}
        if row.get("last_error"):
            print(f"{sid}: last error: {row['last_error']}")
    return 0
