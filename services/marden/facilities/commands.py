"""`python -m marden facilities <action>`: staff curation of campus financing (marden.facilities.curate).

    facilities link propose --site S --campus C --url U --quote Q [--quote Q2] --tag F|R|M --basis B
    facilities link approve --site S --campus C --by claude-review --reason R
    facilities note --campus C --kind owner_funded|not_found --note N --url U --quote Q [--owner O]
                    [--payer P] [--searched S ...]
    facilities apply SPEC.json        sites, instruments, facts, notes and links from one file

Every source is stored raw first (`agents ingest <url>`); a quote that is not in the stored copy is refused.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from marden import cli

cli.group("facilities", help="Curate campus financing: site links, financed sites, financing notes")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _link_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("step", choices=("propose", "approve"))
    p.add_argument("--site", required=True)
    p.add_argument("--campus", required=True)
    p.add_argument("--url", help="the stored document the evidence quote is from (propose)")
    p.add_argument("--quote", action="append", help="verbatim quote (repeatable: fragments of one passage)")
    p.add_argument("--tag", choices=("F", "R", "M"), help="F filed, R reported, M Marden's inference (propose)")
    p.add_argument("--basis", help="why the site finances this campus (propose)")
    p.add_argument("--by", default="cli", help="who approves (approve)")
    p.add_argument("--reason", help="the reviewer's evidence note (approve)")


@cli.action("facilities", "link", help="Propose or approve a financed-site -> campus link", configure=_link_args)
def link_command(args: argparse.Namespace) -> int:
    from marden.core import db
    from marden.core.checks import ValidationError
    from marden.facilities.curate import approve_link, propose_link

    try:
        if args.step == "propose":
            if not (args.url and args.quote and args.tag and args.basis):
                print("propose needs --url, --quote, --tag and --basis")
                return 2
            with db.transaction() as conn:
                out = propose_link(conn, args.site, args.campus, url=args.url, quote=args.quote, tag=args.tag,
                                   basis=args.basis)
        else:
            out = approve_link(args.site, args.campus, by=args.by, reason=args.reason or "")
    except ValidationError as exc:
        print(f"refused: {exc}")
        return 1
    _print(out)
    return 0 if out.get("ok", True) else 1


def _note_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--campus", required=True)
    p.add_argument("--kind", required=True, choices=("owner_funded", "not_found"))
    p.add_argument("--note", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--quote", action="append", required=True)
    p.add_argument("--owner")
    p.add_argument("--payer", help="the owner's payer page id, when Marden has one")
    p.add_argument("--searched", action="append", default=[], help="what was searched (repeatable)")


@cli.action("facilities", "note", help="Write a campus financing note (owner-funded / not found)",
            configure=_note_args)
def note_command(args: argparse.Namespace) -> int:
    from marden.core import db
    from marden.core.checks import ValidationError
    from marden.facilities.curate import add_financing_note

    try:
        with db.transaction() as conn:
            fid = add_financing_note(conn, args.campus, kind=args.kind, note=args.note, url=args.url,
                                     quote=args.quote, owner=args.owner, payer_id=args.payer, searched=args.searched)
    except ValidationError as exc:
        print(f"refused: {exc}")
        return 1
    _print({"fact_id": fid})
    return 0


def _apply_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("spec", type=Path, help="JSON: sites, instruments, notes, links, approve_by")


@cli.action("facilities", "apply", help="Apply a curation spec (one transaction, then link approvals)",
            configure=_apply_args)
def apply_command(args: argparse.Namespace) -> int:
    from marden.core.checks import ValidationError
    from marden.facilities.curate import apply_spec, load_spec

    try:
        out = apply_spec(load_spec(args.spec))
    except ValidationError as exc:
        print(f"refused: {exc}")
        return 1
    _print(out)
    failed = [lk for lk in out["links"] if lk.get("decision", {}).get("ok") is False]
    return 1 if failed else 0
