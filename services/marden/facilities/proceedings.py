"""Staff-curated facts on court and docket proceedings (the site Opposition tab's rows and cards).

A proceeding is a `docket` entity whose `linked_sites` names the financed site (seed/facilities_events.py).
The archive that seeded them gives each one a name and a link, dated the day it was recorded; the case's own
dates and status come from here, each quoted from a document Marden stored raw first (same rule as
facilities/curate.py: a quote not in the stored copy is refused).

Entity fields (all optional; the Opposition tab reads them, sites_tabs.opposition_items):

    summary        what the case is about, in plain words ('Appeals of the MPSC approval of ...')
    case_numbers   docket / case numbers and who filed each ('Nos. 380362, 380364 (Attorney General) ...')
    court          where it sits ('Michigan Court of Appeals')
    case_date      the case's own date: {"d": "YYYY-MM-DD", "kind": "filed" | "entered" | "decided" | "voted",
                   "note": optional hover text}
    status         'Pending (briefing)', 'Settled by consent judgment', 'Decided: petitions denied' ...
    status_note    why the status reads so, or what is not known (hover)
    rulings        [{"d": "YYYY-MM-DD", "what": "...", "short": "..."}]: the latest one is the tab's 'Latest
                   ruling' (`short` on the tile, `what` under it)
    next_dates     [{"d": "YYYY-MM-DD", "what": "..."}]: the next one is the tab's 'Next hearing'

Site field: `opposition_if_voided` (tag M): what happens to the project and its debt if the challenged
ordinances or approvals are voided, stated from the quoted consent-judgment / ordinance facts, as
'<the answer in a few words>: <why>' (the tile shows the words, the why under them).

A re-run replaces the curation's own earlier value of a field (that row is superseded, never deleted).

    python -m marden facilities proceedings SPEC.json

    {"entities": [{"id": "docket-mi-coa", "facts": [{field, value, tag, url, quote, valid_from}]},
                  {"create": {"key": "WASHTENAW_CC_MANDAMUS", "aliases": [...]}, "linked_sites": ["the-barn"],
                   "facts": [...]}],
     "sites": [{"slug": "the-barn", "facts": [...]}]}
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from marden import cli
from marden.core import db
from marden.core.checks import ValidationError
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.facilities.curate import PRODUCER, add_fact

ENTITY_FIELDS = frozenset({
    "summary", "case_numbers", "court", "case_date", "status", "status_note", "rulings", "next_dates",
    "proceeding", "proceeding_kind", "linked_sites", "list_url",
})
SITE_FIELDS = frozenset({"opposition_if_voided"})
DATE_KINDS = frozenset({"filed", "entered", "decided", "voted"})
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
IMPACT_METHOD = "opposition-impact v0.1"


def _day(value: Any, what: str) -> str:
    if not (isinstance(value, str) and DAY.fullmatch(value)):
        raise ValidationError(f"{what}: a YYYY-MM-DD day is required, got {value!r}")
    date.fromisoformat(value)
    return value


def check_value(field: str, value: Any) -> Any:
    """The shape each proceeding field must have (module doc); a malformed value is refused."""
    if field == "case_date":
        if not isinstance(value, dict) or value.get("kind") not in DATE_KINDS:
            raise ValidationError(f"case_date needs a kind in {sorted(DATE_KINDS)}: {value!r}")
        _day(value.get("d"), "case_date.d")
    elif field in ("rulings", "next_dates"):
        if not isinstance(value, list) or not value:
            raise ValidationError(f"{field} is a non-empty list of {{d, what}}")
        for item in value:
            if not isinstance(item, dict) or not str(item.get("what") or "").strip():
                raise ValidationError(f"{field}: each item needs d and what: {item!r}")
            if "short" in item and not (isinstance(item["short"], str) and 0 < len(item["short"]) <= 40):
                raise ValidationError(f"{field}: short is a few words (40 characters at most): {item!r}")
            _day(item.get("d"), f"{field}.d")
    elif field == "linked_sites":
        if not isinstance(value, list) or not all(isinstance(s, str) and s for s in value):
            raise ValidationError("linked_sites is a list of site slugs")
    elif not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is a non-empty text")
    return value


def _facts(conn: Conn, subject_type: str, subject_id: str, facts: Sequence[dict[str, Any]],
           allowed: frozenset[str]) -> int:
    for f in facts:
        if f["field"] not in allowed:
            raise ValidationError(f"{subject_type}.{f['field']} is not a proceeding field")
        value = check_value(f["field"], f["value"])
        tag = f["tag"]
        if f["field"] == "opposition_if_voided" and tag != "M":
            raise ValidationError("opposition_if_voided is Marden's analysis: tag M")
        fact_id = add_fact(conn, subject_type, subject_id, f["field"], value, tag=tag, url=f["url"],
                           quote=f["quote"], valid_from=f["valid_from"], source=f.get("source"),
                           method_version=f.get("method_version") or (IMPACT_METHOD if tag == "M" else None))
        earlier = [r["id"] for r in conn.execute(
            """SELECT id FROM fact WHERE subject_type = %s AND subject_id = %s AND field = %s AND producer = %s
                 AND status = 'published' AND id <> %s""",
            (subject_type, subject_id, f["field"], PRODUCER, fact_id))]
        if earlier:
            FactWriter(conn).supersede(earlier, "replaced by a re-run of the proceedings curation",
                                       decided_by=PRODUCER)
    return len(facts)


def _entity(conn: Conn, item: dict[str, Any]) -> str:
    """The proceeding's entity id: an existing docket entity, or one created as the seed names them."""
    if "id" in item:
        if conn.execute("SELECT 1 FROM entity WHERE id = %s AND kind = 'docket'", (item["id"],)).fetchone() is None:
            raise ValidationError(f"docket entity {item['id']!r} does not exist")
        return str(item["id"])
    create = item.get("create") or {}
    key = str(create.get("key") or "").strip()
    if not key:
        raise ValidationError("a new proceeding needs create.key ('WASHTENAW_CC_MANDAMUS')")
    from marden.seed.common import ensure_entity

    return ensure_entity(conn, f"Docket {key.replace('_', ' ')}", "docket", aliases=create.get("aliases") or [],
                         meta={"proceeding_key": key})


def apply_proceedings(spec: dict[str, Any]) -> dict[str, Any]:
    """Entities (created when new) and their facts, then site facts, in one transaction."""
    out: dict[str, Any] = {"entities": [], "facts": 0}
    with db.transaction() as conn:
        for item in spec.get("entities", []):
            eid = _entity(conn, item)
            out["facts"] += _facts(conn, "entity", eid, item.get("facts", []), ENTITY_FIELDS)
            out["entities"].append(eid)
        for s in spec.get("sites", []):
            if conn.execute("SELECT 1 FROM site WHERE slug = %s", (s["slug"],)).fetchone() is None:
                raise ValidationError(f"site {s['slug']!r} does not exist")
            out["facts"] += _facts(conn, "site", s["slug"], s.get("facts", []), SITE_FIELDS)
    return out


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("spec", type=Path, help="JSON: entities [{id | create, facts}], sites [{slug, facts}]")


@cli.action("facilities", "proceedings", help="Apply quoted court / docket proceeding facts (Opposition tab)",
            configure=_args)
def proceedings_command(args: argparse.Namespace) -> int:
    try:
        spec = json.loads(args.spec.read_text())
        if not isinstance(spec, dict):
            raise ValidationError("the spec is a JSON object")
        out = apply_proceedings(spec)
    except ValidationError as exc:
        print(f"refused: {exc}")
        return 1
    print(json.dumps(out, indent=2))
    return 0
