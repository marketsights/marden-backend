"""`python -m marden <command>`: built-ins plus commands registered by plugin modules.

Built-ins: `worker`, `rebuild <view|all|dirty>`, `sources [--sync]`. Modules add commands at
import time (loaded by marden.plugins):

    from marden import cli

    @cli.action("collect", "sec_edgar", help="Fetch new EDGAR filings")
    def collect_sec_edgar(args): ...                 # python -m marden collect sec_edgar

Groups `seed <loader>`, `collect <source>` and `model <name>` appear once something registers.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass

Handler = Callable[[argparse.Namespace], int | None]
Configure = Callable[[argparse.ArgumentParser], None]

GROUP_HELP = {
    "seed": "Load a seed dataset from MARDEN_SEED_DIR",
    "collect": "Run one collector now",
    "model": "Run one model now",
}


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    handler: Handler
    configure: Configure | None = None


_commands: dict[str, Command] = {}
_groups: dict[str, dict[str, Command]] = {}
_group_help: dict[str, str] = dict(GROUP_HELP)


def _check_new(table: dict[str, Command], name: str, cmd: Command) -> None:
    existing = table.get(name)
    if existing is not None and existing.handler is not cmd.handler:
        raise ValueError(f"command {name!r} is already registered")


def command(name: str, *, help: str, configure: Configure | None = None) -> Callable[[Handler], Handler]:
    """Register a top-level command."""

    def decorate(fn: Handler) -> Handler:
        cmd = Command(name, help, fn, configure)
        if name in _groups:
            raise ValueError(f"{name!r} is a command group")
        _check_new(_commands, name, cmd)
        _commands[name] = cmd
        return fn

    return decorate


def group(name: str, *, help: str) -> None:
    """Declare (or re-describe) a command group such as `collect`."""
    if name in _commands:
        raise ValueError(f"{name!r} is already a command")
    _group_help[name] = help
    _groups.setdefault(name, {})


def action(
    group_name: str, name: str, *, help: str, configure: Configure | None = None
) -> Callable[[Handler], Handler]:
    """Register `python -m marden <group_name> <name>`."""

    def decorate(fn: Handler) -> Handler:
        if group_name in _commands:
            raise ValueError(f"{group_name!r} is already a command")
        actions = _groups.setdefault(group_name, {})
        cmd = Command(name, help, fn, configure)
        _check_new(actions, name, cmd)
        actions[name] = cmd
        return fn

    return decorate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marden", description="Marden services")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for name in sorted(_commands):
        cmd = _commands[name]
        p = sub.add_parser(name, help=cmd.help, description=cmd.help)
        if cmd.configure:
            cmd.configure(p)
        p.set_defaults(_handler=cmd.handler)
    for group_name in sorted(_groups):
        actions = _groups[group_name]
        if not actions:
            continue
        gp = sub.add_parser(group_name, help=_group_help.get(group_name, group_name))
        gsub = gp.add_subparsers(dest="action", metavar="<name>", required=True)
        for name in sorted(actions):
            cmd = actions[name]
            p = gsub.add_parser(name, help=cmd.help, description=cmd.help)
            if cmd.configure:
                cmd.configure(p)
            p.set_defaults(_handler=cmd.handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    from marden.core.logs import setup_logging

    setup_logging()  # MARDEN_LOG_FORMAT (text | json), MARDEN_LOG_LEVEL; secrets redacted
    from marden import commands  # noqa: F401  (built-in commands)
    from marden.plugins import load_plugins

    load_plugins()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler | None = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args) or 0)
