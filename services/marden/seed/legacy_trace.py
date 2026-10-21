"""Seed loader ``legacy_trace``: the archive's finra.org TRACE pull -> price_obs kind 'trade' (tag F).

    cd services && uv run python -m marden seed legacy_trace

Reads (each stored raw as a seed document first; MARDEN_SEED_DIR):

- trace/eod.json: end-of-day last sale price and yield per CUSIP and trade date (2026-01-02..10-19), plus
  each CUSIP's security master record;
- trace/trace_rows.json: per instrument, the CUSIP, its series and the last sale at the 2026-10-20 10:55 ET
  refresh (prices rounded to 3 decimals);
- trace/spreads.json: per instrument and date the yield, interpolated UST par yield and G-spread (bp) of the
  TRACE price (rounded to 3 decimals): the yield and G-spread go on the matching price row (yield_pct,
  spread_bp; the UST yield is yield minus spread);
- trace/secs2.json, trace/secs.json, trace/sec_search.json: security master searches (2026-10-17) with the
  last sale of each bond;
- aiwc/pxsrc/case_px.json: the 'TRACE last sale' rows (one per case, with the CUSIP in the URL);
- aidebt.json: instrument px values that copy a TRACE row (same date, price within the 2-decimal rounding).

All copies of one print collapse to one observation per (CUSIP, trade date): the unrounded eod.json price
wins and a copy matches it within half a unit of its rounding. The archive was refreshed at 10:55 ET on
2026-10-20 (refresh_state.json), so a print dated that day is an intraday last sale, not an end-of-day
price: it is written with obs_at = the refresh time (the print is at or before it), apart from the
end-of-day rows (obs_at NULL) the collector writes for that date. Each observation is written for every
instrument whose identifiers name the CUSIP (finra_trace_store.watched_universe); a CUSIP no identifier names
is reported and not written. An aidebt px that copies TRACE is written as that same observation, so it adds
no row. source_name 'FINRA TRACE (finra.org)', source_url the CUSIP's finra.org bond page. Security master
fields the instrument lacks become facts (coupon_rate_pct, maturity, is_144a, issuer_name). Idempotent.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from marden import cli
from marden.collectors.finra_trace_parse import (
    ET,
    ParseError,
    Security,
    number,
    parse_envelope,
    parse_eod,
    parse_security,
    yield_pct,
)
from marden.collectors.finra_trace_store import TraceCounts, TraceWriter, Universe, watched_universe
from marden.core import db, heartbeat
from marden.core.sources import get_spec, upsert_spec
from marden.seed.common import SEED_AS_OF, SEED_SOURCE_ID, load_json, parse_date, producer, seed_document

LOADER = "legacy_trace"
PRODUCER = producer(LOADER)
EOD, TRACE_ROWS, SPREADS = "trace/eod.json", "trace/trace_rows.json", "trace/spreads.json"
SECS = ("trace/secs2.json", "trace/secs.json")
SEC_SEARCH, MATCH, REFRESH = "trace/sec_search.json", "trace/match.json", "trace/refresh_state.json"
CASE_PX, AIDEBT = "aiwc/pxsrc/case_px.json", "aidebt.json"
ROUNDED_3DP = Decimal("0.0005") + Decimal("1e-9")  # copies rounded to 3 decimals
ROUNDED_2DP = Decimal("0.0051")  # aidebt px (2 decimals), as seed/instruments_prices.TOLERANCE
CUSIP_IN_URL = re.compile(r"cusip=([0-9A-Z]{9})")


@dataclass
class Obs:
    cusip: str
    day: date
    price: Decimal
    relpath: str
    yield_pct: Decimal | None = None
    spread_bp: Decimal | None = None
    origins: set[str] = field(default_factory=set)
    obs_at: datetime | None = None  # set for an intraday last sale (a print dated the refresh day)


@dataclass
class Book:
    obs: dict[tuple[str, date], Obs] = field(default_factory=dict)
    outcome: Counter[str] = field(default_factory=Counter)
    conflicts: list[str] = field(default_factory=list)

    def add(self, cusip: str, day: date, price: Decimal, *, relpath: str, origin: str, tol: Decimal,
            yield_pct: Decimal | None = None, spread_bp: Decimal | None = None) -> Obs | None:
        key = (cusip, day)
        cur = self.obs.get(key)
        if cur is None:
            cur = self.obs[key] = Obs(cusip, day, price, relpath, yield_pct, spread_bp, {origin})
            self.outcome[f"{origin}.new"] += 1
            return cur
        if abs(cur.price - price) > tol:
            self.outcome[f"{origin}.conflict"] += 1
            self.conflicts.append(f"{cusip} {day} {origin} {price} vs {cur.price} ({'/'.join(sorted(cur.origins))})")
            return None
        cur.yield_pct = cur.yield_pct if cur.yield_pct is not None else yield_pct
        cur.spread_bp = cur.spread_bp if cur.spread_bp is not None else spread_bp
        cur.origins.add(origin)
        self.outcome[f"{origin}.matched"] += 1
        return cur

    def find(self, cusip: str, day: date, price: Decimal, tol: Decimal) -> Obs | None:
        cur = self.obs.get((cusip, day))
        return cur if cur is not None and abs(cur.price - price) <= tol else None


@dataclass
class Report:
    counts: Counter[str] = field(default_factory=Counter)
    trace: TraceCounts = field(default_factory=TraceCounts)
    unmapped: dict[str, dict[str, Any]] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    aidebt_unmatched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"counts": dict(sorted(self.counts.items())), **self.trace.as_meta(),
                "unmapped_cusips": len(self.unmapped), "unmapped": self.unmapped, "conflicts": self.conflicts[:40],
                "aidebt_unmatched": self.aidebt_unmatched[:40]}


def _dec(value: Any, what: str) -> Decimal | None:
    return number(value, what)


def _read_books() -> tuple[Book, dict[str, tuple[Security, date, str]]]:
    """Every TRACE print and security record in the archive, collapsed (module docstring)."""
    book = Book()
    secs: dict[str, tuple[Security, date, str]] = {}
    refresh = load_json(REFRESH)
    refreshed = parse_date(refresh.get("when")) or SEED_AS_OF
    for cusip, entry in sorted(load_json(EOD).items()):
        for row in parse_eod(entry.get("eod") or [], cusip):
            book.add(cusip, row.trade_date, row.price, relpath=EOD, origin="eod", tol=Decimal(0),
                     yield_pct=row.yield_pct)
        for rec in entry.get("sec") or []:
            sec = parse_security(rec)
            secs.setdefault(sec.cusip, (sec, refreshed, EOD))
    for iid, t in sorted(load_json(TRACE_ROWS).items()):
        cusip = str(t["cusip"])
        for d, p, y in t.get("ser") or []:
            book.add(cusip, date.fromisoformat(d), _dec(p, f"{iid} ser"), relpath=TRACE_ROWS, origin="trace_rows.ser",
                     tol=ROUNDED_3DP, yield_pct=yield_pct(y, f"{iid} ser yield"))
        if t.get("d") and t.get("px") is not None:
            book.add(cusip, date.fromisoformat(t["d"]), _dec(t["px"], f"{iid} px"), relpath=TRACE_ROWS,
                     origin="trace_rows.last", tol=ROUNDED_3DP, yield_pct=yield_pct(t.get("y"), f"{iid} y"))
    for iid, s in sorted(load_json(SPREADS)["b"].items()):
        for d, p, y, _ust, bp in s.get("ser") or []:
            book.add(str(s["c"]), date.fromisoformat(d), _dec(p, f"{iid} spread px"), relpath=SPREADS,
                     origin="spreads", tol=ROUNDED_3DP, yield_pct=yield_pct(y, f"{iid} spread y"),
                     spread_bp=_dec(bp, f"{iid} spread bp"))
    searches: list[tuple[str, list[dict[str, Any]]]] = [
        (rel, [r for recs in load_json(rel).values() for r in recs]) for rel in SECS
    ]
    searches.append((SEC_SEARCH, [r for v in load_json(SEC_SEARCH).values() if v.get("status") == 200
                                  for r in parse_envelope(v["body"])]))
    for rel, records in searches:
        for rec in records:
            sec = parse_security(rec)
            asof = sec.last_trade_date or SEED_AS_OF
            secs.setdefault(sec.cusip, (sec, asof, rel))
            if sec.last_trade_date and sec.last_sale_price is not None:
                book.add(sec.cusip, sec.last_trade_date, sec.last_sale_price, relpath=rel, origin="security_search",
                         tol=ROUNDED_3DP, yield_pct=sec.last_sale_yield)
    for case, rec in sorted(load_json(CASE_PX).items()):
        m = CUSIP_IN_URL.search(str(rec.get("url") or ""))
        if rec.get("kind") != "TRACE last sale" or not m or rec.get("px") is None or not rec.get("date"):
            continue
        book.add(m.group(1), date.fromisoformat(rec["date"]), _dec(rec["px"], f"{case} px"), relpath=CASE_PX,
                 origin="case_px", tol=ROUNDED_3DP)
    refreshed_at = refresh_time(refresh)
    for obs in book.obs.values():
        if refreshed_at is not None and obs.day == refreshed_at.astimezone(ET).date():
            obs.obs_at = refreshed_at
            book.outcome["intraday_at_refresh"] += 1
    return book, secs


def refresh_time(refresh: dict[str, Any]) -> datetime | None:
    """When the archive's last TRACE refresh ran (refresh_state.json: epoch 't', else ET wall clock 'when')."""
    if isinstance(refresh.get("t"), int | float):
        return datetime.fromtimestamp(refresh["t"], UTC).replace(microsecond=0)
    if refresh.get("when"):
        return datetime.fromisoformat(str(refresh["when"])).replace(tzinfo=ET).astimezone(UTC)
    return None


def _aidebt_copies(book: Book, universe: Universe, report: Report) -> list[tuple[str, Obs]]:
    """(instrument id, the TRACE observation) for every aidebt px that copies one."""
    aidebt = load_json(AIDEBT)
    cols = aidebt["cols"].split(",") if isinstance(aidebt["cols"], str) else aidebt["cols"]
    by_iid: dict[str, list[str]] = defaultdict(list)
    for cusip in universe.cusips():
        for iid in universe.instruments(cusip):
            by_iid[iid].append(cusip)
    out: list[tuple[str, Obs]] = []
    for r in aidebt["rows"]:
        row = dict(zip(cols, r, strict=True))
        px, day = row.get("px"), parse_date(row.get("pxd"))
        if px is None or day is None:
            continue
        hits = [o for c in by_iid.get(row["id"], []) if (o := book.find(c, day, Decimal(str(px)), ROUNDED_2DP))]
        if hits:
            out.extend((row["id"], o) for o in hits)
            report.counts["aidebt_px.trace_copy"] += 1
        elif any((c, day) in book.obs for c in by_iid.get(row["id"], [])):
            report.aidebt_unmatched.append(f"{row['id']} {px} {day}")
    return out


def load() -> Report:
    upsert_spec(get_spec(SEED_SOURCE_ID))
    report = Report()
    with heartbeat.run(SEED_SOURCE_ID) as hb:
        with db.transaction() as conn:
            files = (EOD, TRACE_ROWS, SPREADS, *SECS, SEC_SEARCH, MATCH, REFRESH, CASE_PX, AIDEBT)
            docs = {rel: seed_document(rel, conn=conn).id for rel in files}
            universe = watched_universe(conn)
        book, secs = _read_books()
        report.counts.update(book.outcome)
        report.conflicts = book.conflicts
        copies = _aidebt_copies(book, universe, report)
        hints = load_json(MATCH)
        by_cusip: dict[str, list[Obs]] = defaultdict(list)
        for obs in book.obs.values():
            by_cusip[obs.cusip].append(obs)
        for cusip in sorted(by_cusip):
            iids = universe.instruments(cusip)
            if not iids:
                report.unmapped[cusip] = {"prints": len(by_cusip[cusip]),
                                          "archive_match": sorted(i for i, cs in hints.items() if cusip in cs)}
                continue
            with db.transaction() as conn:
                writer = TraceWriter(conn, producer=PRODUCER, counts=report.trace)
                for iid in iids:
                    for o in sorted(by_cusip[cusip], key=lambda x: x.day):
                        writer.trade(iid, cusip, o.day, o.price, yield_pct=o.yield_pct, spread_bp=o.spread_bp,
                                     obs_at=o.obs_at, document_id=docs[o.relpath])
                    if cusip in secs and universe.facts_from(iid, cusip):
                        sec, asof, rel = secs[cusip]
                        writer.security_facts(iid, sec, valid_from=asof, document_id=docs[rel])
        before = report.trace.prices_new
        with db.transaction() as conn:
            writer = TraceWriter(conn, producer=PRODUCER, counts=report.trace)
            for iid, o in copies:
                writer.trade(iid, o.cusip, o.day, o.price, yield_pct=o.yield_pct, spread_bp=o.spread_bp,
                             obs_at=o.obs_at, document_id=docs[o.relpath])
        report.counts["aidebt_px.rows_added"] = report.trace.prices_new - before
        report.counts["observations"] = len(book.obs)
        report.counts["cusips_in_archive"] = len(by_cusip)
        report.counts["cusips_watched"] = len(universe.cusips())
        hb.seen(report.trace.prices_seen + report.trace.facts_seen)
        hb.new(report.trace.prices_new + report.trace.facts_new)
        hb.meta = {"loader": LOADER, **{k: v for k, v in report.as_dict().items() if k != "unmapped"}}
    return report


@cli.action("seed", LOADER, help="Load the archive's finra.org TRACE pull as price_obs (FINRA TRACE (finra.org))")
def run_cli(args: argparse.Namespace) -> int:
    try:
        report = load()
    except ParseError as exc:
        print(f"legacy_trace: archive file not in the known shape: {exc}")
        return 1
    print(json.dumps(report.as_dict(), indent=2, default=str))
    return 0
