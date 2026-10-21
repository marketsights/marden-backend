"""Database reads and writes for the `sec_nport` collector (medians are in sec_nport_marks).

- `watched_identifiers`: the join keys of our instruments: `instrument.cusip` / `isin` and the
  published `identifiers` facts (CUSIP, ISIN, LoanX), plus the CUSIP inside a US/CA ISIN.
- `ensure_fund`: one `entity` (kind 'fund') per N-PORT series, found by its EDGAR series id, else by
  the entity the holdings seed loader made for the same series (registrant CIK + series name), else
  created with the series id, registrant name and CIK in `meta`. A new series entity is not given
  `entity.cik` and no registrant entity is created for it: every entity with a CIK is polled by
  `sec_index` every 10 minutes, and a fund complex files hundreds of series under one CIK.
- `write_filing`: per matched position a `holding` row (line_key shared with the holdings seed
  loader, so the same filed line is one row), its attribute facts on subject 'holding' and a
  `fund_mark` price; the series' net assets as entity fact `net_assets_usd`.
Everything is tag F (filed); reruns write nothing new (dedupe keys and the holding unique key).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.collectors.sec_nport_parse import NportHeader, Position, id_keys, mark_price
from marden.core.db import Conn
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter
from marden.core.jsonutil import jsonb
from marden.core.records import HoldingWriter, PriceWriter
from marden.seed.common import slugify
from marden.seed.holdings_lines import nport_line_key
from marden.seed.instruments_nport import nport_source_name
from marden.seed.instruments_parse import LOANX_IN_TEXT, core_name, cusip_valid, isin_valid, normalize_name

FILING_TYPE = "N-PORT"
FUND_KIND = "fund"
_STRONG_OTHER = re.compile(r"^(LX\d{6}|BL\d{7})$")  # LoanX and Bloomberg loan ids
_UNFUNDED = re.compile(r"unfunded", re.I)


@dataclass
class Watched:
    """Join key -> instrument ids (instruments whose own cusip/isin column carries the key first)."""

    keys: dict[str, list[str]] = field(default_factory=dict)

    def add(self, key: str | None, instrument_id: str) -> None:
        for k in id_keys(key):
            ids = self.keys.setdefault(k, [])
            if instrument_id not in ids:
                ids.append(instrument_id)

    def instruments(self, keys: Iterable[str]) -> list[str]:
        out: list[str] = []
        for k in sorted(keys):
            out += [i for i in self.keys.get(k, ()) if i not in out]
        return out

    def ambiguous(self) -> dict[str, list[str]]:
        return {k: v for k, v in sorted(self.keys.items()) if len(v) > 1}


def watched_identifiers(conn: Conn) -> Watched:
    out = Watched()
    rows = conn.execute("SELECT id, cusip, isin FROM instrument ORDER BY id").fetchall()
    known = {r["id"] for r in rows}
    for r in rows:
        out.add(r["cusip"], r["id"])
        out.add(r["isin"], r["id"])
    facts = conn.execute(
        """SELECT subject_id, value FROM fact_current
           WHERE subject_type = 'instrument' AND field = 'identifiers' ORDER BY subject_id"""
    ).fetchall()
    for r in facts:
        if r["subject_id"] not in known or not isinstance(r["value"], list):
            continue
        for item in r["value"]:
            if isinstance(item, dict) and item.get("type") in ("cusip", "isin", "loanx"):
                out.add(str(item.get("value") or ""), r["subject_id"])
    return out


# ---------------------------------------------------------------- funds
def _seeded_fund(conn: Conn, h: NportHeader, name: str) -> str | None:
    """The entity the holdings seed loader made for this series: same registrant CIK and the series
    name (or 'name (registrant)'); for a single-series registrant, the registrant entity itself."""
    if not h.reg_cik:
        return None
    rows = conn.execute(
        """SELECT id, name, aliases, cik FROM entity WHERE kind = %(kind)s AND meta->>'series_id' IS NULL
           AND (meta->>'registrant_cik' = %(cik)s OR cik = %(cik)s) ORDER BY id""",
        {"kind": FUND_KIND, "cik": h.reg_cik},
    ).fetchall()
    wanted = {normalize_name(name), normalize_name(f"{name} ({h.reg_name})")}
    for r in rows:
        if {normalize_name(n) for n in [r["name"], *(r["aliases"] or [])]} & wanted:
            return r["id"]
    if core_name(h.reg_name) == core_name(name):
        return next((r["id"] for r in rows if r["cik"] == h.reg_cik), None)
    return None


def ensure_fund(conn: Conn, h: NportHeader) -> str:
    """The entity id of the filing's series (created on first sight)."""
    name = " ".join(h.fund_name.split())
    found = None
    if h.series_id:
        row = conn.execute(
            "SELECT id FROM entity WHERE kind = %s AND meta->>'series_id' = %s ORDER BY id LIMIT 1",
            (FUND_KIND, h.series_id),
        ).fetchone()
        found = row["id"] if row else None
    if found is None and (found := _seeded_fund(conn, h, name)) is not None and h.series_id:
        conn.execute("UPDATE entity SET meta = meta || %s WHERE id = %s",
                     (jsonb({"series_id": h.series_id, "series_lei": h.series_lei}), found))
    if found is not None:
        conn.execute(
            """UPDATE entity SET aliases = array_append(aliases, %(n)s)
               WHERE id = %(id)s AND name <> %(n)s AND NOT (%(n)s = ANY(aliases))""",
            {"n": name, "id": found},
        )
        return found
    suffix = h.series_id.lower() if h.series_id else f"cik{h.reg_cik}"
    eid = f"{slugify(name)}-{suffix}"
    parent = conn.execute(  # a registrant entity that already exists (never created here)
        "SELECT id FROM entity WHERE kind = %s AND cik = %s ORDER BY id LIMIT 1", (FUND_KIND, h.reg_cik)
    ).fetchone() if h.reg_cik else None
    meta = {"role": "nport_series", "series_id": h.series_id, "series_lei": h.series_lei,
            "registrant_cik": h.reg_cik, "registrant_name": h.reg_name, "registrant_lei": h.reg_lei}
    conn.execute(
        """INSERT INTO entity (id, name, kind, parent_id, lei, aliases, meta) VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO NOTHING""",
        (eid, name, FUND_KIND, parent["id"] if parent else None, h.series_lei,
         [h.series_id] if h.series_id else [], jsonb(meta)),
    )
    return eid


# ---------------------------------------------------------------- one filing
@dataclass(frozen=True)
class Match:
    position: Position
    keys: tuple[str, ...]
    instrument_ids: tuple[str, ...]


@dataclass
class FilingWrite:
    holdings_new: int = 0
    holdings_seen: int = 0
    facts_new: int = 0
    prices_new: int = 0
    marks: int = 0
    no_mark: int = 0


def matches(positions: Iterable[Position], watched: Watched) -> list[Match]:
    out = []
    for p in positions:
        hit = sorted(k for k in p.keys if k in watched.keys)
        if hit:
            out.append(Match(p, tuple(hit), tuple(watched.instruments(hit))))
    return out


def accession_written(conn: Conn, accession: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM holding WHERE accession = %s AND filing_type = %s LIMIT 1", (accession, FILING_TYPE)
    ).fetchone()
    return row is not None


def line_code(p: Position) -> str:
    """The line's identifier, chosen as the holdings seed loader chooses it (seed/holdings_archive.py):
    a valid CUSIP, else a valid ISIN, else a strong 'other' id (LoanX, Bloomberg loan id, CUSIP, ISIN),
    else a LoanX id in the title or name, else the 'other' id, else the normalized name."""
    first_other = p.others[0][1] if p.others else None
    if p.cusip_valid or p.isin_valid:
        return p.cusip_valid or p.isin_valid or ""
    if first_other and (_STRONG_OTHER.match(first_other) or cusip_valid(first_other) or isin_valid(first_other)):
        return first_other
    for text in (p.title, p.name):
        if m := LOANX_IN_TEXT.search(text or ""):
            return m.group(0)
    return first_other or f"name:{normalize_name(p.name or p.title)[:60]}"


def unfunded(p: Position) -> bool:
    """An unfunded delayed-draw commitment line: its value is not a price (seed/holdings_lines.py rule)."""
    return bool(_UNFUNDED.search(f"{p.title} {p.name}"))


def _holding_facts(p: Position, filing_date: date) -> dict[str, Any]:
    out: dict[str, Any] = {"public_date": filing_date.isoformat()}
    for name, value in (("units", p.units), ("cur_cd", p.cur_cd), ("exchange_rt", p.exchange_rt),
                        ("fair_value_level", p.fair_value_level), ("asset_cat", p.asset_cat)):
        if value is not None:
            out[name] = value
    if unfunded(p):
        out["unfunded_commitment"] = True
    return out


def write_filing(
    conn: Conn, doc: DocumentRow, h: NportHeader, found: list[Match], *, filing_date: date, producer: str
) -> FilingWrite:
    """Holdings, holding facts, per-position marks and the fund's net assets for one filing."""
    out = FilingWrite()
    facts, prices, holdings = FactWriter(conn), PriceWriter(conn), HoldingWriter(conn)
    fund_id = ensure_fund(conn, h)
    source_name = nport_source_name(h.fund_name, h.period)
    common = {"tag": "F", "source_name": source_name, "source_url": doc.url, "valid_from": h.period,
              "producer": producer, "status": "published", "document_id": doc.id}
    if h.net_assets is not None:
        out.facts_new += facts.add("entity", fund_id, "net_assets_usd", h.net_assets, unit="usd", **common).created
    accession = str(doc.external_id)
    for m in found:
        p = m.position
        price = None if unfunded(p) else mark_price(p)
        res = holdings.add(
            fund_id, accession, nport_line_key(line_code(p), p.units, p.balance, p.val_usd), period_end=h.period,
            filing_type=FILING_TYPE, filing_url=doc.url, filed_at=filing_date, instrument_id=m.instrument_ids[0],
            issuer_name=p.name or None, title=p.title or None, cusip=p.cusip_valid, isin=p.isin_valid,
            par=p.balance if p.units == "PA" else None, value_usd=p.val_usd, mark_price=price, pct_nav=p.pct_val,
            fund_net_assets=h.net_assets, document_id=doc.id,
        )
        out.holdings_seen += 1
        out.holdings_new += int(res.created)
        for name, value in _holding_facts(p, filing_date).items():
            out.facts_new += facts.add("holding", str(res.id), name, value, **common).created
        if price is None:
            out.no_mark += 1
            continue
        out.marks += 1
        for iid in m.instrument_ids:
            out.prices_new += prices.add(
                iid, h.period, "fund_mark", price, tag="F", source_name=source_name, source_url=doc.url,
                document_id=doc.id,
            ).created
    return out
