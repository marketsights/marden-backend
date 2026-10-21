"""Database side of the `sec_xbrl` collector.

- `targets`: the operating companies to collect: every entity with a CIK whose kind is a payer, neocloud,
  developer, chipmaker, power or other operating company (funds, BDCs, lenders and SPVs are left out),
  grouped by CIK. `ensure_issuers` first adds the issuers in scope that the seed data lacks (Broadcom).
- `name_matches`: the filer name in companyfacts must match the entity's name or an alias, so a wrong
  CIK in our entity table never puts another company's financials on it.
- `write_observations`: published, tag F entity facts (valid_from = period end) with the filing index as
  source_url, the XBRL concept in quote_loc and the record itself as the quote; already-written facts are
  found by dedupe key in one query, so a rerun costs one SELECT.
- `due_ciks`: the CIKs a new 10-Q/10-K makes due: sec_index publishes a `filing` event on the filer's
  entity; the CIK is due until a sec_xbrl run has seen that accession in the filer's companyfacts. A run
  that did not find it (the XBRL API lags, or the filing has no financial data) is retried every
  RETRY_HOURS while the event is younger than DUE_DAYS.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from marden.collectors.sec_index_parse import filing_index_url, normalize_cik
from marden.collectors.sec_xbrl_parse import Observation
from marden.core.db import Conn
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter, fact_dedupe_key
from marden.core.jsonutil import to_jsonable
from marden.seed.common import ensure_entity

SOURCE_ID = "sec_xbrl"
PRODUCER = "collector:sec_xbrl"
TAG = "F"
TARGET_KINDS = ("cloud", "neocloud", "ai_lab", "chipmaker", "developer", "company", "power_provider", "utility")
# Issuers in scope (spec 24 payers and their suppliers) that the seed entity table does not have yet:
# (name, kind, CIK, ticker). The CIK is checked against the filer name like every other target.
ISSUERS = (("Broadcom", "chipmaker", "1730168", "AVGO"),)
PERIODIC_EVENT = re.compile(r"^(10-Q|10-K|10-KT|10-QT)(/A)?(\s|:|$)")
DUE_DAYS = 10
RETRY_HOURS = 6
RECENT_ACCESSION_DAYS = 45  # accessions a run records per CIK (the due check reads them)
_SUFFIXES = frozenset(
    {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "lp", "plc", "nv",
     "n", "v", "sa", "ag", "group", "holdings", "holding", "the", "tx", "de", "com"}
)


@dataclass
class Target:
    cik: str
    entity_ids: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # entity names and aliases


def ensure_issuers(conn: Conn) -> list[str]:
    return [ensure_entity(conn, name, kind, cik=cik, ticker=ticker) for name, kind, cik, ticker in ISSUERS]


def targets(conn: Conn, only: Iterable[str] | None = None) -> list[Target]:
    wanted = {c for c in (normalize_cik(x) for x in only) if c} if only is not None else None
    out: dict[str, Target] = {}
    rows = conn.execute(
        "SELECT id, name, aliases, cik FROM entity WHERE cik IS NOT NULL AND kind = ANY(%s) ORDER BY id",
        (list(TARGET_KINDS),),
    ).fetchall()
    for row in rows:
        cik = normalize_cik(row["cik"])
        if cik is None or (wanted is not None and cik not in wanted):
            continue
        t = out.setdefault(cik, Target(cik))
        t.entity_ids.append(row["id"])
        t.names += [row["name"], *(row["aliases"] or [])]
    return [out[c] for c in sorted(out, key=int)]


def _tokens(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\(.*?\)", " ", text)  # "Amazon (AWS)", "Hut 8 Corp. (assumed)"
    return [t for t in re.split(r"[^a-z0-9]+", text) if t and t not in _SUFFIXES]


def name_matches(filer_name: str, names: Iterable[str]) -> bool:
    """The filer is one of `names`: same words once corporate suffixes are dropped, or the same first word
    ('RIOT PLATFORMS, INC.' ~ 'Riot', 'AMAZON COM INC' ~ 'Amazon (AWS)')."""
    filer = _tokens(filer_name)
    if not filer:
        return False
    for name in names:
        mine = _tokens(name or "")
        if mine and (mine == filer or mine[0] == filer[0]):
            return True
    return False


def source_name(filer_name: str, form: str) -> str:
    return f"SEC XBRL: {filer_name} {form}"


def _fact_args(obs: Observation, cik: str, filer_name: str, doc: DocumentRow) -> dict[str, Any]:
    rec = obs.record
    return {
        "tag": TAG, "source_name": source_name(filer_name, rec.form),
        "source_url": filing_index_url(cik, rec.accession), "valid_from": obs.valid_from, "producer": PRODUCER,
        "status": "published", "unit": obs.unit, "document_id": doc.id, "quote": rec.raw,
        "quote_loc": obs.quote_loc(),
    }


def write_observations(
    conn: Conn, cik: str, entity_ids: list[str], observations: list[Observation], *, filer_name: str,
    doc: DocumentRow,
) -> int:
    """Write every observation on every entity carrying the CIK, oldest filing first. Returns new facts."""
    planned = []
    for entity_id in entity_ids:
        for obs in observations:
            args = _fact_args(obs, cik, filer_name, doc)
            key = fact_dedupe_key("entity", entity_id, obs.field, to_jsonable(obs.value), TAG, args["source_url"],
                                  obs.valid_from, PRODUCER)
            planned.append((key, entity_id, obs, args))
    if not planned:
        return 0
    have = {r["dedupe_key"] for r in conn.execute(
        "SELECT dedupe_key FROM fact WHERE dedupe_key = ANY(%s)", ([p[0] for p in planned],)).fetchall()}
    writer, new = FactWriter(conn), 0
    for key, entity_id, obs, args in planned:
        if key in have:
            continue
        new += int(writer.add("entity", entity_id, obs.field, obs.value, **args).created)
    return new


# ---------------------------------------------------------------- new 10-Q / 10-K
@dataclass(frozen=True)
class DueFiling:
    cik: str
    entity_id: str
    accession: str
    title: str
    event_date: date
    learned_at: datetime


def _attempts(conn: Conn, since: datetime) -> dict[str, tuple[datetime, set[str]]]:
    """CIK -> (start of the latest sec_xbrl run that fetched it, accessions that run saw)."""
    out: dict[str, tuple[datetime, set[str]]] = {}
    rows = conn.execute(
        """SELECT started_at, meta FROM source_run WHERE source_id = %s AND started_at >= %s
           ORDER BY started_at DESC, id DESC""",
        (SOURCE_ID, since),
    ).fetchall()
    for row in rows:
        for cik, info in ((row["meta"] or {}).get("fetched") or {}).items():
            if cik not in out:
                out[cik] = (row["started_at"], set((info or {}).get("recent_accessions") or []))
    return out


def due_filings(conn: Conn, *, today: date, now: datetime | None = None) -> list[DueFiling]:
    """10-Q/10-K filing events (sec_index) on target entities whose accession no sec_xbrl run has seen."""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """SELECT ev.subject_id, ent.cik, d.external_id AS accession, ev.title, ev.event_date, ev.learned_at
           FROM event ev JOIN entity ent ON ent.id = ev.subject_id JOIN document d ON d.id = ev.document_id
           WHERE ev.kind = 'filing' AND ev.subject_type = 'entity' AND ev.status = 'published'
             AND ev.event_date >= %s AND ent.cik IS NOT NULL AND ent.kind = ANY(%s)
           ORDER BY ev.event_date, ev.id""",
        (today - timedelta(days=DUE_DAYS), list(TARGET_KINDS)),
    ).fetchall()
    attempts = _attempts(conn, now - timedelta(days=DUE_DAYS + RECENT_ACCESSION_DAYS))
    out = []
    for row in rows:
        cik = normalize_cik(row["cik"])
        if cik is None or not row["accession"] or not PERIODIC_EVENT.match(row["title"]):
            continue
        tried = attempts.get(cik)
        if tried is not None:
            started, seen = tried
            if row["accession"] in seen:
                continue
            if started >= row["learned_at"] and started > now - timedelta(hours=RETRY_HOURS):
                continue  # looked after the filing was announced and not long ago: wait before retrying
        out.append(DueFiling(cik, row["subject_id"], row["accession"], row["title"], row["event_date"],
                             row["learned_at"]))
    return out
