"""Instrument terms from filed and reported documents: issuer, guarantors, collateral, covenants, issue date and
the lender group (spec 07 Terms card and holders section), and the reason a term stays n/d.

    python -m marden facilities terms SPEC.json [--dry-run]

SPEC: {"instruments": [{"id": ..., "facts": [{field, value, tag, url, quote, valid_from, source?}],
                        "replaces": {field: reason}, "not_disclosed": {field: reason}}]}

Rules (seed/cited.py, spec 22 "no quote, no fact"):
- every value cites a document stored raw first (`facilities.curate.stored_document`) and its quote must be in the
  stored text (fragments checked one by one, `facilities.curate.checked_quote`);
- tag F only for a filed document (an SEC EDGAR filing or exhibit, a county bond ordinance, a rating agency's own
  release is R); everything else is R with its source. A value no document states is not written;
- `replaces`: the current published fact of that field is superseded by the new one (bitemporal history kept,
  `FactWriter.supersede` with the replacement id), e.g. a FINRA TRACE abbreviation ('APLD COMPUTECO LLC') by the
  legal name the indenture gives;
- `not_disclosed`: field -> why it is n/d ("not disclosed in filings checked: ..."), one fact
  `terms_not_disclosed` (tag M, method TERMS_ND_METHOD) the Terms card shows next to the n/d.

Fields: issuer_name, backstop (Guarantees), collateral_text, covenants (the covenant package: the indenture's or
credit agreement's covenant headings), covenant_test (one key test), issue_date (closing / settlement / funding
date, ISO), and lender_<slug> ({name, role, commitment_usd?}: one fact per named lender, arranger or agent).
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg

from marden import cli
from marden.core import db, documents
from marden.core.business_day import business_today
from marden.core.checks import ValidationError
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.facilities.curate import source_name

PRODUCER = "curation:terms"
WRITE_ATTEMPTS = 4
TERMS_ND_METHOD = "terms-nd v0.1"
TERM_FIELDS = frozenset({
    "issuer_name", "backstop", "collateral_text", "covenants", "covenant_test", "issue_date", "tranche_of",
    "amortization", "commitment_usd", "ranking_text", "rating", "amount_usd",
})
LENDER_FIELD = re.compile(r"^lender_[a-z0-9_]+$")
FILED_HOSTS = ("sec.gov",)


def is_filed(url: str) -> bool:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in FILED_HOSTS)


def lender_field(name: str) -> str:
    """'JPMorgan Chase Bank, N.A.' -> 'lender_jpmorgan_chase_bank_n_a'."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"lender_{slug[:60]}"


def stored_text_document(conn: Conn, url: str) -> documents.DocumentRow:
    """The newest stored copy of `url` that has bytes (record-only rows, which the archive listed without
    bytes, are skipped as in seed/cited.py). A URL never stored is refused: store it raw first."""
    row = conn.execute(
        """SELECT * FROM document WHERE url = %s AND coalesce(meta->>'record_only', 'false') <> 'true'
             AND coalesce(bytes, 1) > 0 ORDER BY fetched_at DESC, id DESC LIMIT 1""",
        (url,),
    ).fetchone()
    if row is None:
        raise ValidationError(f"{url} is not stored with its text: fetch it raw first")
    return documents.DocumentRow.from_row(row, was_new=False)


_TEXT_CACHE: dict[int, Any] = {}


def checked_quote(conn: Conn, url: str, quote: str | list[str]) -> tuple[int, str]:
    """(document id, quote as stored) once every fragment is found verbatim in the stored text."""
    from marden.agents.text import find_quote, read_text

    parts = [quote] if isinstance(quote, str) else list(quote)
    parts = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
    if not parts:
        raise ValidationError(f"a quote from {url} is required")
    doc = stored_text_document(conn, url)
    if doc.id not in _TEXT_CACHE:
        _TEXT_CACHE[doc.id] = read_text(doc.read(), doc.content_type, doc.url)
    text = _TEXT_CACHE[doc.id]
    for part in parts:
        if find_quote(text, part) is None:
            raise ValidationError(f"quote not in the stored copy of {url} (document {doc.id}): {part[:120]!r}")
    return doc.id, " ... ".join(parts)


def _check(fact: dict[str, Any]) -> None:
    field, tag, url = fact["field"], fact["tag"], fact["url"]
    if field not in TERM_FIELDS and not LENDER_FIELD.match(field):
        raise ValidationError(f"{field}: not a terms field")
    if tag not in ("F", "R"):
        raise ValidationError(f"{field}: terms are filed (F) or reported (R)")
    if tag == "F" and not is_filed(url):
        raise ValidationError(f"{field}: F needs a filed document; {url} is reported (R)")
    if field == "issue_date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fact["value"])):
        raise ValidationError(f"issue_date must be YYYY-MM-DD, got {fact['value']!r}")
    if LENDER_FIELD.match(field) and not (isinstance(fact["value"], dict) and fact["value"].get("name")):
        raise ValidationError(f"{field}: a lender is {{name, role}}")


def add_term(conn: Conn, instrument_id: str, fact: dict[str, Any]) -> int:
    _check(fact)
    doc_id, quote = checked_quote(conn, fact["url"], fact["quote"])
    res = FactWriter(conn).add(
        "instrument", instrument_id, fact["field"], fact["value"], tag=fact["tag"],
        source_name=fact.get("source") or source_name(fact["url"]), source_url=fact["url"],
        valid_from=fact["valid_from"], producer=PRODUCER, status="published", document_id=doc_id, quote=quote,
    )
    return res.id


def _replace(conn: Conn, instrument_id: str, field: str, new_id: int, reason: str) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM fact WHERE subject_type = 'instrument' AND subject_id = %s AND field = %s"
        " AND status = 'published' AND id <> %s",
        (instrument_id, field, new_id),
    ).fetchall()
    old = [r["id"] for r in rows]
    return FactWriter(conn).supersede(old, reason, replaced_by={i: new_id for i in old}, decided_by=PRODUCER)


class _DryRun(Exception):
    """Raised inside the transaction to roll a dry run back."""


def apply_terms(spec: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    for attempt in range(WRITE_ATTEMPTS):
        out: dict[str, Any] = {"instruments": 0, "facts": 0, "superseded": 0, "not_disclosed": 0, "by_field": {}}
        try:
            _apply(spec, out, dry_run)
        except _DryRun:
            out["dry_run"] = True
        except psycopg.errors.DeadlockDetected:  # another writer marked the same read models dirty: run it again
            if attempt == WRITE_ATTEMPTS - 1:
                raise
            time.sleep(0.5 * (attempt + 1))
            continue
        return out
    raise AssertionError("unreachable")


def _apply(spec: dict[str, Any], out: dict[str, Any], dry_run: bool) -> None:
    with db.transaction() as conn:
        for item in spec.get("instruments", []):
            iid = item["id"]
            if conn.execute("SELECT 1 FROM instrument WHERE id = %s", (iid,)).fetchone() is None:
                raise ValidationError(f"instrument {iid!r} does not exist")
            new_ids: dict[str, int] = {}
            for fact in item.get("facts", []):
                new_ids[fact["field"]] = add_term(conn, iid, fact)
                key = "lender" if fact["field"].startswith("lender_") else fact["field"]
                out["by_field"][key] = out["by_field"].get(key, 0) + 1
                out["facts"] += 1
            for field, reason in (item.get("replaces") or {}).items():
                if field not in new_ids:
                    raise ValidationError(f"{iid}: replaces {field} without a new {field} fact")
                out["superseded"] += len(_replace(conn, iid, field, new_ids[field], reason))
            nd = item.get("not_disclosed") or {}
            if nd:
                FactWriter(conn).add(
                    "instrument", iid, "terms_not_disclosed", nd, tag="M", method_version=TERMS_ND_METHOD,
                    source_name="Marden terms research", valid_from=item.get("checked_on") or business_today(),
                    producer=PRODUCER, status="published",
                )
                out["not_disclosed"] += len(nd)
            out["instruments"] += 1
        if dry_run:
            raise _DryRun


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("spec", type=Path)
    p.add_argument("--dry-run", action="store_true", help="check every quote and rule, write nothing")


@cli.action("facilities", "terms", help="Instrument terms and lenders from stored documents (quoted, F/R)",
            configure=_args)
def terms_command(args: argparse.Namespace) -> int:
    try:
        out = apply_terms(json.loads(args.spec.read_text()), dry_run=args.dry_run)
    except ValidationError as exc:
        print(f"refused: {exc}")
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0

