"""The watched CUSIP universe and the TRACE writers shared by `finra_trace` and `seed legacy_trace`.

- Universe: every instrument's CUSIPs, from `instrument.cusip`, the CUSIP body of a US/CA `instrument.isin`,
  and the `identifiers` facts (type cusip, and the CUSIP body of US/CA ISINs). Nothing else maps a CUSIP to
  an instrument: a TRACE CUSIP no identifier names is reported, not guessed onto an instrument.
- Prices: price_obs kind 'trade', tag F, source_name 'FINRA TRACE (finra.org)', source_url = the finra.org
  bond page of the CUSIP. One end-of-day row per instrument, CUSIP and trade date (obs_at NULL), plus the last
  print with its time (obs_at, UTC) when the bond page gives one. Reruns write nothing new (price_obs dedupe).
- Security master: coupon_rate_pct, maturity, is_144a and issuer_name instrument facts (tag F), written
  only when the instrument has no current fact for that field (from any source).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from marden.collectors.finra_trace_parse import SOURCE_NAME, Security, bond_page_url
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.records import PriceWriter
from marden.seed.instruments_parse import cusip_valid

CUSIP_ISIN_COUNTRIES = ("US", "CA")  # ISINs whose national number is the CUSIP
FLOATING_FIELDS = ("coupon_benchmark", "coupon_spread_bp")  # a floater's coupon is not a fixed rate


def cusip_of_isin(isin: str | None) -> str | None:
    if isin and len(isin) == 12 and isin[:2] in CUSIP_ISIN_COUNTRIES and cusip_valid(isin[2:11]):
        return isin[2:11]
    return None


@dataclass
class Universe:
    """CUSIP -> instrument ids, primary-first order per instrument kept for the security-master facts."""

    by_cusip: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    primary: dict[str, str] = field(default_factory=dict)  # instrument id -> its primary CUSIP
    isins: set[str] = field(default_factory=set)

    def add(self, cusip: str | None, iid: str, *, primary: bool = False) -> None:
        if cusip and cusip_valid(cusip):
            self.by_cusip[cusip].add(iid)
            if primary:
                self.primary.setdefault(iid, cusip)

    def instruments(self, cusip: str) -> list[str]:
        return sorted(self.by_cusip.get(cusip, ()))

    def cusips(self) -> list[str]:
        return sorted(self.by_cusip)

    def facts_from(self, iid: str, cusip: str) -> bool:
        """Security-master facts come from the instrument's primary CUSIP (any CUSIP if it has none)."""
        return self.primary.get(iid, cusip) == cusip


def watched_universe(conn: Conn) -> Universe:
    u = Universe()
    for r in conn.execute("SELECT id, cusip, isin FROM instrument ORDER BY id").fetchall():
        u.add(r["cusip"], r["id"], primary=True)
        u.add(cusip_of_isin(r["isin"]), r["id"], primary=True)
        if r["isin"]:
            u.isins.add(r["isin"])
    for r in conn.execute(
        """SELECT subject_id, value FROM fact_current WHERE subject_type = 'instrument' AND field = 'identifiers'
           ORDER BY subject_id"""
    ).fetchall():
        for item in r["value"] if isinstance(r["value"], list) else []:
            kind, value = item.get("type"), str(item.get("value") or "")
            if kind == "cusip":
                u.add(value, r["subject_id"])
            elif kind == "isin":
                u.isins.add(value)
                u.add(cusip_of_isin(value), r["subject_id"])
    return u


@dataclass
class TraceCounts:
    prices_seen: int = 0
    prices_new: int = 0
    facts_seen: int = 0
    facts_new: int = 0
    instruments_priced: set[str] = field(default_factory=set)
    cusips_priced: set[str] = field(default_factory=set)
    latest: dict[str, list[Any]] = field(default_factory=dict)  # cusip -> [date, price]

    def as_meta(self) -> dict[str, Any]:
        return {"prices_seen": self.prices_seen, "prices_new": self.prices_new, "facts_seen": self.facts_seen,
                "facts_new": self.facts_new, "instruments_priced": len(self.instruments_priced),
                "cusips_priced": len(self.cusips_priced)}


class TraceWriter:
    def __init__(self, conn: Conn, *, producer: str, counts: TraceCounts | None = None):
        self.conn = conn
        self.prices = PriceWriter(conn)
        self.facts = FactWriter(conn)
        self.producer = producer
        self.counts = counts or TraceCounts()
        self._present: dict[str, set[str]] = {}

    def trade(self, iid: str, cusip: str, trade_date: date, price: Decimal | float, *,
              yield_pct: Decimal | float | None = None, spread_bp: Decimal | float | None = None,
              obs_at: datetime | None = None, document_id: int | None = None) -> bool:
        res = self.prices.add(
            iid, trade_date, "trade", price, tag="F", source_name=SOURCE_NAME, source_url=bond_page_url(cusip),
            obs_at=obs_at, yield_pct=yield_pct, spread_bp=spread_bp, document_id=document_id,
        )
        c = self.counts
        c.prices_seen += 1
        c.prices_new += int(res.created)
        c.instruments_priced.add(iid)
        c.cusips_priced.add(cusip)
        newest = c.latest.get(cusip)
        if newest is None or newest[0] <= trade_date.isoformat():
            c.latest[cusip] = [trade_date.isoformat(), float(price)]
        return res.created

    def _has(self, iid: str, fields: Iterable[str]) -> bool:
        if iid not in self._present:
            rows = self.conn.execute(
                "SELECT field FROM fact_current WHERE subject_type = 'instrument' AND subject_id = %s", (iid,)
            ).fetchall()
            self._present[iid] = {r["field"] for r in rows}
        return any(f in self._present[iid] for f in fields)

    def security_facts(self, iid: str, sec: Security, *, valid_from: date, document_id: int | None) -> int:
        """Security-master facts the instrument lacks. Returns the number written."""
        wanted: list[tuple[str, Any, str | None, tuple[str, ...]]] = []
        if sec.coupon_rate_pct is not None:
            wanted.append(("coupon_rate_pct", sec.coupon_rate_pct, "pct", ("coupon_rate_pct", *FLOATING_FIELDS)))
        if sec.maturity is not None:
            wanted.append(("maturity", sec.maturity.isoformat(), "date", ("maturity", "maturity_text")))
        if sec.is_144a is not None:
            wanted.append(("is_144a", sec.is_144a, None, ("is_144a",)))
        if sec.issuer_name:
            wanted.append(("issuer_name", sec.issuer_name, "text", ("issuer_name",)))
        written = 0
        for name, value, unit, blockers in wanted:
            if self._has(iid, blockers):
                continue
            res = self.facts.add(
                "instrument", iid, name, value, tag="F", source_name=SOURCE_NAME, source_url=bond_page_url(sec.cusip),
                valid_from=min(valid_from, date.today()), producer=self.producer, status="published", unit=unit,
                document_id=document_id,
            )
            self.counts.facts_seen += 1
            self.counts.facts_new += int(res.created)
            written += int(res.created)
            self._present[iid].add(name)
        return written


def instruments_with_prints(conn: Conn, cusip: str) -> bool:
    """Whether Marden already holds TRACE rows for this CUSIP's bond page (from the archive or a past run)."""
    row = conn.execute(
        "SELECT 1 FROM price_obs WHERE source_name = %s AND source_url = %s LIMIT 1",
        (SOURCE_NAME, bond_page_url(cusip)),
    ).fetchone()
    return row is not None
