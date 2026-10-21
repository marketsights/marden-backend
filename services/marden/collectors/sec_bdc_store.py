"""Database writes for `sec_bdc`: BDC entities by CIK, holdings, holding facts, fund-mark prices.

Every value is as filed in the BDC's XBRL schedule of investments (tag F), dated by the schedule's
period end, with the filing's primary document as source_url and the data set zip as document_id.
The unfunded-adjusted mark is the one model value (tag M, sec_bdc_link.METHOD_VERSION).

Another loader may already hold the same filing line (the seed's fundpos.json import of the 30 Jun
2026 schedules writes holdings and marks under the same BDC entities and filing URLs, par and value
rounded to $k). Such a line is reused, not duplicated: the facts go on it, it is linked if it was
not, and no second mark is written where that loader already priced the instrument from the filing.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from marden.collectors import sec_bdc_borrowers as bb
from marden.collectors import sec_bdc_link as lk
from marden.collectors.sec_bdc_parse import SoiLine
from marden.core.db import Conn
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter
from marden.core.jsonutil import jsonb
from marden.core.records import HoldingWriter, PriceWriter
from marden.seed.common import ensure_entity

BDC_KIND = "bdc"
PRICE_KIND = "fund_mark"
MAX_LISTED = 60
SOURCE_ID = "sec_bdc"
MATCH_TOLERANCE_USD = 1000  # the seed's par and value are $k-rounded
_FOREIGN_LINE = """
SELECT h.id, h.instrument_id FROM holding h LEFT JOIN document d ON d.id = h.document_id
WHERE h.fund_entity_id = %(fund)s AND h.accession = %(accession)s AND h.period_end = %(period)s
  AND coalesce(d.source_id, '') <> %(source)s AND h.id <> ALL(%(claimed)s::bigint[])
  AND abs(h.par - %(par)s) <= %(tol)s AND abs(h.value_usd - %(fv)s) <= %(tol)s
ORDER BY h.id LIMIT 1"""
_FOREIGN_MARK = """
SELECT 1 FROM price_obs p LEFT JOIN document d ON d.id = p.document_id
WHERE p.instrument_id = %s AND p.obs_date = %s AND p.kind = 'fund_mark' AND p.tag = %s AND p.source_url = %s
  AND coalesce(d.source_id, '') <> %s LIMIT 1"""


@dataclass
class WriteStats:
    lines: int = 0
    positions: int = 0
    unfunded_rows: int = 0
    unfunded_assigned: int = 0
    unfunded_unpaired: int = 0
    not_positions: int = 0  # no par (equity, issuer subtotals, commitments without a funded line)
    holdings_new: int = 0
    holdings_reused: int = 0  # lines another loader already holds
    holdings_linked_later: int = 0
    marks_left_to_other_loader: int = 0
    facts_new: int = 0
    prices_f_new: int = 0
    prices_m_new: int = 0
    entities_new: int = 0
    linked: Counter = field(default_factory=Counter)  # method -> lines
    raw_refused: Counter = field(default_factory=Counter)  # reason -> lines
    unlinked: list[str] = field(default_factory=list)
    entity_conflicts: list[str] = field(default_factory=list)
    link_disagreements: list[str] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def merge(self, other: WriteStats) -> None:
        """Add a committed part (one filing's transaction) to the run totals."""
        for name, value in other.__dict__.items():
            mine = getattr(self, name)
            if isinstance(value, Counter):
                mine.update(value)
            elif isinstance(value, list):
                mine.extend(v for v in value if v not in mine)
            else:
                setattr(self, name, mine + value)

    def meta(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["linked"], out["raw_refused"] = dict(self.linked), dict(self.raw_refused)
        out["unlinked"], out["samples"] = self.unlinked[:MAX_LISTED], self.samples[:10]
        return out


def source_name(line: SoiLine) -> str:
    """Matches the seed's 'BDC 10-Q: <BDC> (<period>)' (marden.seed.instruments_nport.bdc_source_name)."""
    return f"BDC {line.form}: {line.bdc_name} ({line.period.isoformat()})"


def bdc_entity(conn: Conn, cik: str, name: str, stats: WriteStats) -> str:
    """The BDC's entity: one already carrying this CIK as a BDC (or under this name), else a new one.
    A CIK found on an entity that is not this BDC is a data error elsewhere: it is reported, not used."""
    cik = cik.lstrip("0")
    rows = conn.execute("SELECT id, name, kind FROM entity WHERE cik = %s ORDER BY id", (cik,)).fetchall()
    for r in rows:
        if r["kind"] == BDC_KIND or lk.norm_name(r["name"]) == lk.norm_name(name):
            return r["id"]
    before = conn.execute("SELECT count(*) AS n FROM entity").fetchone()["n"]
    meta = {"source": "SEC BDC Data Sets", "cik": cik}
    if not rows:
        eid = ensure_entity(conn, name, BDC_KIND, cik=cik, meta=meta)
    else:
        eid = ensure_entity(conn, name, BDC_KIND, meta=meta)  # by id or name only: the CIK points elsewhere
        conn.execute("UPDATE entity SET cik = %s WHERE id = %s AND cik IS NULL", (cik, eid))
        note = f"CIK {cik} ({name}) is also on entity {', '.join(r['id'] for r in rows)}"
        if note not in stats.entity_conflicts:
            stats.entity_conflicts.append(note)
    stats.entities_new += conn.execute("SELECT count(*) AS n FROM entity").fetchone()["n"] - before
    return eid


def _fact(facts: FactWriter, holding_id: int, field_name: str, value: Any, line: SoiLine, doc: DocumentRow,
          unit: str | None, stats: WriteStats) -> None:
    if value is None:
        return
    res = facts.add(
        "holding", str(holding_id), field_name, value, tag="F", source_name=source_name(line),
        source_url=line.filing_url, valid_from=line.period, producer="collector:sec_bdc", status="published",
        unit=unit, document_id=doc.id,
    )
    stats.facts_new += int(res.created)


def _price(prices: PriceWriter, instrument_id: str, line: SoiLine, price: Decimal, doc: DocumentRow,
           *, model: bool, stats: WriteStats) -> bool:
    tag = "M" if model else "F"
    if prices.conn.execute(_FOREIGN_MARK, (instrument_id, line.period, tag, line.filing_url, SOURCE_ID)).fetchone():
        stats.marks_left_to_other_loader += 1
        return False
    res = prices.add(
        instrument_id, line.period, PRICE_KIND, price, tag="M" if model else "F",
        source_name=source_name(line) + (" unfunded-adjusted" if model else ""), source_url=line.filing_url,
        method_version=lk.METHOD_VERSION if model else None, document_id=doc.id,
    )
    return res.created


def _holding(holdings: HoldingWriter, fund: str, line: SoiLine, link: lk.Link, raw: Decimal | None,
             doc: DocumentRow, claimed: list[int], stats: WriteStats) -> tuple[int, str | None]:
    """(holding id, the instrument it is linked to): this collector's own row, else another loader's row
    for the same filing line, else a new row."""
    conn = holdings.conn
    own = conn.execute("SELECT id FROM holding WHERE fund_entity_id = %s AND accession = %s AND line_key = %s",
                       (fund, line.adsh, line.line_key)).fetchone()
    other = None if own else conn.execute(_FOREIGN_LINE, {
        "fund": fund, "accession": line.adsh, "period": line.period, "source": SOURCE_ID, "claimed": claimed,
        "par": line.par, "fv": line.fv, "tol": MATCH_TOLERANCE_USD}).fetchone()
    if other is not None:
        claimed.append(other["id"])
        stats.holdings_reused += 1
        if other["instrument_id"] is None and link.instrument_id:
            stats.holdings_linked_later += int(holdings.link_instrument(other["id"], link.instrument_id))
            return other["id"], link.instrument_id
        if link.instrument_id and other["instrument_id"] != link.instrument_id:
            stats.link_disagreements.append(f"holding {other['id']}: other loader {other['instrument_id']}, "
                                            f"sec_bdc {link.instrument_id}")
            return other["id"], None  # unsure which is right: no marks from this line
        return other["id"], other["instrument_id"]
    res = holdings.add(
        fund, line.adsh, line.line_key, period_end=line.period, filing_type=line.form, filing_url=line.filing_url,
        filed_at=line.filed, instrument_id=link.instrument_id, issuer_name=bb.BY_KEY[line.borrower].name,
        title=line.identifier[:1000], par=line.par, value_usd=line.fv, mark_price=raw,
        pct_nav=line.pct_nav * 100 if line.pct_nav is not None else None, document_id=doc.id,
    )
    stats.holdings_new += int(res.created)
    if not res.created and link.instrument_id and holdings.link_instrument(res.id, link.instrument_id):
        stats.holdings_linked_later += 1
    return res.id, link.instrument_id


def write_lines(conn: Conn, lines: list[SoiLine], instruments: dict[str, lk.InstrumentInfo], doc: DocumentRow,
                stats: WriteStats) -> None:
    """Holdings, their facts and prices for one filing's followed lines (caller's transaction)."""
    holdings, facts, prices = HoldingWriter(conn), FactWriter(conn), PriceWriter(conn)
    unfunded, unpaired = lk.assign_unfunded(lines)
    stats.unfunded_unpaired += unpaired
    entities: dict[str, str] = {}
    claimed: list[int] = []
    for line in lines:
        stats.lines += 1
        stats.unfunded_rows += int(lk.is_unfunded_row(line))
        if not lk.is_position(line):
            stats.not_positions += 1
            continue
        stats.positions += 1
        if line.cik not in entities:
            entities[line.cik] = bdc_entity(conn, line.cik, line.bdc_name, stats)
        link = lk.link_line(line, instruments)
        raw, refused = lk.raw_mark(line)
        if refused:
            stats.raw_refused[refused] += 1
        u = unfunded.get((line.adsh, line.line_key))
        stats.unfunded_assigned += int(u is not None)
        holding_id, linked_to = _holding(holdings, entities[line.cik], line, link, raw, doc, claimed, stats)
        if link.instrument_id:
            stats.linked[link.method] += 1
        elif len(stats.unlinked) < MAX_LISTED * 4:
            stats.unlinked.append(f"{line.bdc_name} {line.period}: {line.identifier[:120]} -> {link.reason}")
        _fact(facts, holding_id, "cost_usd", line.cost, line, doc, "usd", stats)
        _fact(facts, holding_id, "spread_bp", lk.tagged_spread_bp(line), line, doc, "bp", stats)
        _fact(facts, holding_id, "interest_rate_pct", lk.tagged_rate_pct(line), line, doc, "pct", stats)
        maturity = line.maturity.isoformat() if line.maturity else None
        _fact(facts, holding_id, "maturity", maturity, line, doc, None, stats)
        _fact(facts, holding_id, "unfunded_commitment_usd", u.amount if u else None, line, doc, "usd", stats)
        adjusted = lk.adjusted_mark(line, u) if (u is not None and raw is not None) else None
        if link.priced and linked_to == link.instrument_id and linked_to and raw is not None:
            stats.prices_f_new += int(_price(prices, linked_to, line, raw, doc, model=False, stats=stats))
            if adjusted is not None:
                stats.prices_m_new += int(_price(prices, linked_to, line, adjusted, doc, model=True, stats=stats))
        if len(stats.samples) < 10 and link.priced and adjusted is not None:
            stats.samples.append({"bdc": line.bdc_name, "instrument": link.instrument_id, "period": str(line.period),
                                  "par": str(line.par), "fv": str(line.fv), "unfunded": str(u.amount if u else None),
                                  "raw": str(raw), "adjusted": str(adjusted)})


# ---------------------------------------------------------------- parse marker on the zip's document
PARSED_KEY = "sec_bdc_parsed"


def is_parsed(doc: DocumentRow) -> bool:
    return bool(doc.meta.get(PARSED_KEY))


def mark_parsed(conn: Conn, doc: DocumentRow, summary: dict[str, Any]) -> None:
    """Record that this exact file (sha256) was parsed and written, so an unchanged file (304) is skipped."""
    marker = {PARSED_KEY: {"at": datetime.now(UTC).isoformat(), **summary}}
    conn.execute("UPDATE document SET meta = meta || %s WHERE id = %s", (jsonb(marker), doc.id))
