"""Deal parties of an instrument from the issuer's own filings: initial purchasers, underwriters, arrangers,
agents, lenders, the trustee and capped-call counterparties (spec 07 holders section, "Lenders, arrangers
and agents"), and why a role stays unnamed.

    python -m marden facilities parties SPEC.json            dry run: check every quote and rule, write nothing
    python -m marden facilities parties SPEC.json --write    store any source not stored yet, then write

SPEC: {"instruments": [{"id": ..., "parties": [{name, role, tag, url, quote, valid_from, source?,
                        commitment_usd?}], "facts": [terms facts, e.g. issue_date], "not_disclosed": {role: reason}}]}

Each party is one `lender_<slug>` fact ({name, role, commitment_usd?}), the lender fact field the holders
section reads, written through `facilities.terms.add_term`: tag F only for an SEC filing or exhibit, R for a
press release or report (used only when no filing names the party), and every quote must be in the stored
copy of its source. One entity in two roles (a bank that is both initial purchaser and capped-call
counterparty) gets one fact per role. A party already named on the instrument in the same role group (by an
earlier spec) is skipped, not written twice. `not_disclosed` writes one fact `parties_not_disclosed` (tag M):
role -> why no party is named (e.g. "initial purchasers: the purchase agreement is not filed").

Sources: a URL not stored yet is fetched (SEC fair-access User-Agent and rate limit, `core.http`) and, with
--write, stored raw first (source `agent_url`, not queued for the agents); the dry run checks its quotes on
the fetched text without storing it. Safe to run on production: the dry run writes nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

import psycopg

from marden import cli
from marden.core import db
from marden.core.business_day import business_today
from marden.core.checks import ValidationError
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.facilities import terms
from marden.facilities.terms import LENDER_FIELD, add_term, is_filed

PRODUCER = terms.PRODUCER
PARTIES_ND_METHOD = "parties-nd v0.1"
URL_SOURCE_ID = "agent_url"  # marden.agents.intake URL_SOURCE: staff-requested single documents
WRITE_ATTEMPTS = 4

# Role groups the holders section shows, in order. A role is matched by the first group whose words it holds.
ROLE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Arrangers and bookrunners", ("arranger", "bookrunner", "structuring", "syndication agent")),
    ("Initial purchasers", ("initial purchaser",)),
    ("Underwriters", ("underwriter",)),
    ("Placement agents", ("placement agent", "dealer manager", "sales agent")),
    ("Capped-call counterparties", ("capped call", "capped-call", "option counterpart", "hedge counterpart")),
    ("Trustee", ("trustee",)),
    ("Agents", ("agent", "depositary", "registrar")),
    ("Lenders", ("lender", "investor", "purchaser", "noteholder", "holder", "financier")),
)
OTHER_GROUP = "Other parties"
GROUP_ORDER = tuple(g for g, _ in ROLE_GROUPS) + (OTHER_GROUP,)


def role_group(role: str | None) -> str:
    """'representative of the initial purchasers' -> 'Initial purchasers'; 'collateral agent' -> 'Agents'."""
    text = (role or "").lower()
    for group, words in ROLE_GROUPS:
        if any(w in text for w in words):
            return group
    return "Lenders" if not text else OTHER_GROUP


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def party_field(name: str, role: str | None) -> str:
    """One fact per entity and role group: 'lender_goldman_sachs_co_llc' for the first group a name takes
    keeps the terms loader's field; a second role adds the group ('..._capped_call_counterparties')."""
    base = terms.lender_field(name)
    group = role_group(role)
    if group in ("Lenders", "Arrangers and bookrunners", "Agents"):
        return base
    return f"lender_{_slug(name)[:44]}_{_slug(group)[:24]}".rstrip("_")


_NAME_NOISE = re.compile(r"\b(the|inc|llc|l l c|n a|plc|ltd|limited|corporation|co|company|national association)\b")


def _norm_name(name: str) -> str:
    """'Goldman Sachs & Co. LLC' and 'Goldman Sachs & Co LLC' compare equal."""
    return " ".join(_NAME_NOISE.sub(" ", re.sub(r"[^a-z0-9 ]+", " ", name.lower())).split())


def existing_parties(conn: Conn, instrument_id: str) -> dict[str, set[str]]:
    """Normalized party name -> role groups already published on the instrument (lender_* facts)."""
    rows = conn.execute(
        "SELECT field, value FROM fact WHERE subject_type = 'instrument' AND subject_id = %s AND status = 'published'"
        " AND field LIKE 'lender\\_%%'",
        (instrument_id,),
    ).fetchall()
    out: dict[str, set[str]] = {}
    for r in rows:
        v = r["value"]
        if isinstance(v, dict) and v.get("name"):
            out.setdefault(_norm_name(v["name"]), set()).add(role_group(v.get("role")))
    return out


# ---------------------------------------------------------------- sources


def _stored(conn: Conn, url: str) -> bool:
    try:
        terms.stored_text_document(conn, url)
        return True
    except ValidationError:
        return False


def store_source(url: str, instrument_id: str) -> int:
    """Fetch and store one source raw (its own transaction, as `agents ingest` does, but not queued)."""
    from marden.agents.intake import ensure_intake_sources
    from marden.core.http import fetch_document

    with db.transaction() as conn:
        ensure_intake_sources(conn)
    doc = fetch_document(URL_SOURCE_ID, url, doc_type="url_document",
                         meta={"links": {"instrument": instrument_id}, "source_url": url, "filed": is_filed(url),
                               "stored_by": PRODUCER})
    return doc.id


_FETCHED: dict[str, Any] = {}


def check_unstored_quote(url: str, quote: str | list[str]) -> str:
    """Dry run: the quote checked on a fetched (not stored) copy of `url`."""
    from marden.agents.text import find_quote, read_text
    from marden.core.http import default_fetcher

    if url not in _FETCHED:
        resp = default_fetcher().get(url)
        _FETCHED[url] = read_text(resp.content, resp.content_type, url)
    parts = [quote] if isinstance(quote, str) else [p for p in quote if isinstance(p, str) and p.strip()]
    for part in parts:
        if find_quote(_FETCHED[url], part.strip()) is None:
            raise ValidationError(f"quote not in the fetched copy of {url}: {part[:120]!r}")
    return " ... ".join(p.strip() for p in parts)


# ---------------------------------------------------------------- spec


def _party_fact(p: dict[str, Any]) -> dict[str, Any]:
    for key in ("name", "role", "tag", "url", "quote", "valid_from"):
        if not p.get(key):
            raise ValidationError(f"party {p.get('name')!r}: {key} is required")
    value: dict[str, Any] = {"name": p["name"].strip(), "role": p["role"].strip()}
    if p.get("commitment_usd") is not None:
        value["commitment_usd"] = p["commitment_usd"]
    fact = {"field": party_field(p["name"], p["role"]), "value": value, "tag": p["tag"], "url": p["url"],
            "quote": p["quote"], "valid_from": p["valid_from"], "source": p.get("source")}
    if not LENDER_FIELD.match(fact["field"]):
        raise ValidationError(f"{p['name']}: bad field {fact['field']}")
    return fact


def sources_of(spec: dict[str, Any]) -> list[tuple[str, str]]:
    """(url, instrument id) of every cited source, once each."""
    seen: dict[str, str] = {}
    for item in spec.get("instruments", []):
        for f in [*item.get("parties", []), *item.get("facts", [])]:
            seen.setdefault(f["url"], item["id"])
    return list(seen.items())


class _DryRun(Exception):
    """Raised inside the transaction to roll a dry run back."""


def apply_parties(spec: dict[str, Any], *, write: bool = False) -> dict[str, Any]:
    stored: list[int] = []
    if write:
        with db.connection() as conn:
            missing = [(u, i) for u, i in sources_of(spec) if not _stored(conn, u)]
        stored = [store_source(u, i) for u, i in missing]
    for attempt in range(WRITE_ATTEMPTS):
        out: dict[str, Any] = {"instruments": 0, "with_parties": 0, "parties": 0, "skipped_existing": 0,
                               "facts": 0, "not_disclosed": 0, "by_group": {}, "stored_sources": len(stored),
                               "checked_on_fetched_copy": 0}
        try:
            _apply(spec, out, write)
        except _DryRun:
            out["dry_run"] = True
        except psycopg.errors.DeadlockDetected:
            if attempt == WRITE_ATTEMPTS - 1:
                raise
            time.sleep(0.5 * (attempt + 1))
            continue
        return out
    raise AssertionError("unreachable")


def _check_fact(conn: Conn, iid: str, fact: dict[str, Any], out: dict[str, Any]) -> None:
    """Dry run on a source not stored yet: the terms rules plus the quote on the fetched copy."""
    terms._check(fact)
    if _stored(conn, fact["url"]):
        terms.checked_quote(conn, fact["url"], fact["quote"])
    else:
        check_unstored_quote(fact["url"], fact["quote"])
        out["checked_on_fetched_copy"] += 1


def _write_or_check(conn: Conn, iid: str, fact: dict[str, Any], out: dict[str, Any], write: bool) -> bool:
    """Write the fact (--write: any refusal stops the run, nothing is written), or check it (dry run: every
    refusal is listed, so one run reports them all)."""
    if write:
        add_term(conn, iid, fact)
        return True
    try:
        _check_fact(conn, iid, fact, out)
        return True
    except ValidationError as exc:
        out.setdefault("refused", []).append(f"{iid} {fact['field']}: {exc}")
        return False


def _apply(spec: dict[str, Any], out: dict[str, Any], write: bool) -> None:
    with db.transaction() as conn:
        for item in spec.get("instruments", []):
            iid = item["id"]
            if conn.execute("SELECT 1 FROM instrument WHERE id = %s", (iid,)).fetchone() is None:
                raise ValidationError(f"instrument {iid!r} does not exist")
            have = existing_parties(conn, iid)
            wrote = 0
            for p in item.get("parties", []):
                fact = _party_fact(p)
                group = role_group(p["role"])
                if group in have.get(_norm_name(p["name"]), set()) and not item.get("rewrite"):
                    out["skipped_existing"] += 1
                    continue
                if not _write_or_check(conn, iid, fact, out, write):
                    continue
                have.setdefault(_norm_name(p["name"]), set()).add(group)
                out["by_group"][group] = out["by_group"].get(group, 0) + 1
                out["parties"] += 1
                wrote += 1
            for fact in item.get("facts", []):
                if fact["field"].startswith("lender_"):
                    raise ValidationError(f"{iid}: name parties under 'parties', not 'facts'")
                if _write_or_check(conn, iid, fact, out, write):
                    out["facts"] += 1
            nd = item.get("not_disclosed") or {}
            if nd:
                if not all(isinstance(r, str) and r.strip() for r in nd.values()):
                    raise ValidationError(f"{iid}: not_disclosed reasons must be plain words")
                FactWriter(conn).add(
                    "instrument", iid, "parties_not_disclosed", nd, tag="M", method_version=PARTIES_ND_METHOD,
                    source_name="Marden deal-parties research", valid_from=item.get("checked_on") or business_today(),
                    producer=PRODUCER, status="published",
                )
                out["not_disclosed"] += len(nd)
            out["instruments"] += 1
            out["with_parties"] += 1 if wrote or any(have.values()) else 0
        if not write:
            raise _DryRun


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("spec", type=Path)
    p.add_argument("--write", action="store_true", help="store missing sources and write (default: dry run)")


@cli.action("facilities", "parties", help="Deal parties from filings: purchasers, trustee, capped calls (F/R)",
            configure=_args)
def parties_command(args: argparse.Namespace) -> int:
    try:
        out = apply_parties(json.loads(args.spec.read_text()), write=args.write)
    except ValidationError as exc:
        print(f"refused: {exc}")
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 1 if out.get("refused") else 0
