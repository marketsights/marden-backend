"""FINRA TRACE end-of-day prices from finra.org's public bond pages -> price_obs kind 'trade' (tag F).

For every watched CUSIP (finra_trace_store.watched_universe: instrument CUSIP/ISIN and `identifiers` facts)
the run makes the bond page's own requests (finra_trace_client) and writes, for each instrument the CUSIP
belongs to:

- one end-of-day row per trade date: last sale price and yield (obs_at NULL). A trade date of today (ET)
  is written only after 18:30 ET, when TRACE reporting has closed;
- the last print with its time (obs_at, UTC) from the security master's lastTradeDate/lastTradeTime;
- security-master facts the instrument lacks: coupon_rate_pct, maturity, is_144a, issuer_name.

source_name is exactly 'FINRA TRACE (finra.org)' (marden.core.licensing: unlicensed-commercial) and
source_url the bond page of the CUSIP. The bond page shows no high/low, volume or trade count, so Marden
records none. Reruns write nothing new. Schedule: weekdays 18:45 ET, once a day: a run before 18:30 ET, or
after an ok run that evening, is skipped unless forced. `python -m marden collect finra_trace [--cusip ...]
[--force]`.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from marden import cli
from marden.collectors.finra_trace_client import (
    BOND_TEMPLATE,
    PRICE_YIELD_TEMPLATE,
    AccessRefused,
    BondPageClient,
    bond_body,
    data_url,
    price_yield_body,
)
from marden.collectors.finra_trace_parse import (
    ET,
    SOURCE_NAME,
    ParseError,
    parse_envelope,
    parse_eod,
    parse_security,
    parse_template,
)
from marden.collectors.finra_trace_store import TraceCounts, TraceWriter, watched_universe
from marden.collectors.rates_common import due_et
from marden.core import db, heartbeat
from marden.core.http import FetchError
from marden.core.queue import app
from marden.core.sources import SourceBlocked, declare_source, upsert_spec

log = logging.getLogger(__name__)

SOURCE_ID = "finra_trace"
PRODUCER = f"collector:{SOURCE_ID}"
RUN_AT_ET = (18, 45)
CRON_UTC = "45 22,23 * * 1-5"  # 18:45 ET is 22:45 UTC (EDT) or 23:45 UTC (EST); due_et keeps the right one
TRACE_CLOSE_ET = (18, 30)
BACKFILL_DAYS = 1825  # what the bond page itself asks for
UPDATE_DAYS = 14

TERMS_URL = "https://www.finra.org/terms-of-use"
TERMS_NOTE = (
    f"Checked 2026-10-21 ({TERMS_URL}, \"Last modified: November 9, 2023\"). Permitted Uses: \"the content and "
    "material provided through the FINRA Website shall be used ONLY for your own non-commercial personal or "
    "professional use.\" Restrictions: \"In no case may you: ... d. develop or create a database of data using "
    "the FINRA Website, except as expressly permitted by any other terms of use on the FINRA Website; e. use any "
    "process to monitor or copy the FINRA Website in bulk, or use any data mining, scraping or harvesting tools "
    "(including robots), or any similar data-gathering or extraction tools; f. use any device, software, or "
    "routine to bypass any software or hardware that limits the volume or frequency of requests for the FINRA "
    "Website; ... m. use any portion of the FINRA Website in the development of any software program or in "
    "conjunction with any machine learning, neural network, deep learning, predictive analytics or other "
    "artificial intelligence computer or software program ...\" The bond pages say: \"FINRA Data provides "
    "non-commercial use of data\". Marden's daily automated, commercial use is outside these terms. The business "
    "owner decided on 2026-10-21 to collect anyway and accepts the risk; every row is flagged "
    "'unlicensed-commercial' (marden.core.licensing, docs/methods/licensing.md). Licensed route: a FINRA Rule "
    "7730 TRACE market data subscription (Corporate Bond, Rule 144A, Securitized Products Data Sets). "
    "robots.txt: www.finra.org/robots.txt does not disallow /finra-data/; services-dynarep.ddwa.finra.org has "
    "none (404). Limits kept: User-Agent \"Marden research cody@marden.ai\", at most 1 request/second per host "
    "plus jitter, once a day after 18:30 ET, only the bond page's own requests. A challenge, 401/403/429, login "
    "wall or refused API answer stops the run and is recorded here (last_error); nothing is retried around it."
)

SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "portal", "https://www.finra.org/finra-data/fixed-income",
                        24 * 60, TERMS_NOTE)


@dataclass
class Stats:
    counts: TraceCounts = field(default_factory=TraceCounts)
    extra: dict[str, Any] = field(default_factory=dict)
    documents_seen: int = 0
    documents_new: int = 0
    not_on_finra: list[str] = field(default_factory=list)
    with_history: set[str] = field(default_factory=set)
    partial_day_skipped: int = 0

    def doc(self, d: Any) -> None:
        self.documents_seen += 1
        self.documents_new += int(d.was_new)

    def as_meta(self) -> dict[str, Any]:
        return {"documents_seen": self.documents_seen, "documents_new": self.documents_new,
                **self.counts.as_meta(), "cusips_with_history": len(self.with_history),
                "cusips_not_on_finra": len(self.not_on_finra), "not_on_finra": self.not_on_finra[:40],
                "partial_day_skipped": self.partial_day_skipped, **self.extra}


def _stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.000 ")


def _window(now: datetime, backfill: bool) -> tuple[str, str]:
    end = now.astimezone(UTC).replace(second=0, microsecond=0)
    return _stamp(end - timedelta(days=BACKFILL_DAYS if backfill else UPDATE_DAYS)), _stamp(end)


def _after_close(now: datetime) -> bool:
    local = now.astimezone(ET)
    return (local.hour, local.minute) >= TRACE_CLOSE_ET


def ran_ok_this_evening(now: datetime) -> bool:
    local = now.astimezone(ET)
    since = local.replace(hour=TRACE_CLOSE_ET[0], minute=TRACE_CLOSE_ET[1], second=0, microsecond=0)
    if local < since:
        return False
    with db.transaction() as conn:
        row = conn.execute("SELECT 1 FROM source_run WHERE source_id = %s AND ok AND started_at >= %s LIMIT 1",
                           (SOURCE_ID, since)).fetchone()
    return row is not None


def _priced_cusips(conn: Any) -> set[str]:
    """CUSIPs this collector has already read a history for (archive rows from `seed legacy_trace` do not
    count: they start in 2026, so the first collector run still asks for the page's five years)."""
    rows = conn.execute(
        """SELECT DISTINCT p.source_url FROM price_obs p JOIN document d ON d.id = p.document_id
           WHERE p.source_name = %s AND d.source_id = %s""", (SOURCE_NAME, SOURCE_ID)).fetchall()
    return {r["source_url"].split("cusip=", 1)[1][:9] for r in rows if r["source_url"] and "cusip=" in r["source_url"]}


def _collect_cusip(flow: BondPageClient, cusip: str, iids: list[str], tpl: dict[str, Any], stats: Stats,
                   universe: Any, priced: set[str], now: datetime) -> None:
    bond_doc = flow.post_json(data_url(*tpl["bond"][:2]), bond_body(cusip, tpl["bond"][2]), cusip=cusip,
                              doc_type="trace_bond", title=f"FINRA bond data {cusip}")
    stats.doc(bond_doc)
    records = [r for r in parse_envelope(bond_doc.read()) if str(r.get("cusip") or "").upper() == cusip]
    sec = parse_security(records[0]) if records else None
    if sec is None:
        stats.not_on_finra.append(cusip)
        if cusip not in priced:
            return  # the page stops here too: no bond, no price chart
    if "price_yield" not in tpl:
        tpl_doc = flow.get_json(PRICE_YIELD_TEMPLATE, doc_type="trace_template", title="FINRA price/yield template")
        stats.doc(tpl_doc)
        tpl["price_yield"] = parse_template(tpl_doc.read())
    start, end = _window(now, backfill=cusip not in priced)
    hist_doc = flow.post_json(data_url(*tpl["price_yield"][:2]), price_yield_body(cusip, start, end), cusip=cusip,
                              doc_type="trace_eod", title=f"FINRA end-of-day price/yield {cusip}")
    stats.doc(hist_doc)
    rows = parse_eod(parse_envelope(hist_doc.read()), cusip)
    today = now.astimezone(ET).date()
    closed = _after_close(now)
    with db.transaction() as conn:
        writer = TraceWriter(conn, producer=PRODUCER, counts=stats.counts)
        for iid in iids:
            for row in rows:
                if row.trade_date > today or (row.trade_date == today and not closed):
                    stats.partial_day_skipped += 1
                    continue
                writer.trade(iid, cusip, row.trade_date, row.price, yield_pct=row.yield_pct, document_id=hist_doc.id)
            if sec and sec.last_trade_at and sec.last_sale_price is not None and sec.last_trade_date <= today:
                writer.trade(iid, cusip, sec.last_trade_date, sec.last_sale_price, yield_pct=sec.last_sale_yield,
                             obs_at=sec.last_trade_at, document_id=bond_doc.id)
            if sec and universe.facts_from(iid, cusip):
                writer.security_facts(iid, sec, valid_from=today, document_id=bond_doc.id)
    if rows:
        stats.with_history.add(cusip)


def run(*, cusips: list[str] | None = None, force: bool = False, now: datetime | None = None,
        flow: BondPageClient | None = None) -> dict[str, Any]:
    """One collection run (see module docstring). Returns the run stats; a refusal returns ok=False."""
    now = now or datetime.now(UTC)
    upsert_spec(SOURCE)
    if not force and not _after_close(now):
        return {"ok": True, "skipped": "before the 18:30 ET TRACE close (once a day after the close; --force)"}
    if not force and ran_ok_this_evening(now):
        return {"ok": True, "skipped": "already collected after today's TRACE close"}
    stats = Stats()

    def body() -> None:
        with db.transaction() as conn:
            universe = watched_universe(conn)
            priced = _priced_cusips(conn)
        targets = [c.strip().upper() for c in cusips] if cusips else universe.cusips()
        stats.extra.update(cusips_watched=len(universe.cusips()), isins_watched=len(universe.isins),
                           cusips_requested=len(targets))
        client = flow or BondPageClient(SOURCE_ID)
        try:
            if not targets:
                return
            client.page(targets[0])
            tpl_doc = client.get_json(BOND_TEMPLATE, doc_type="trace_template", title="FINRA bond template")
            stats.doc(tpl_doc)
            tpl: dict[str, Any] = {"bond": parse_template(tpl_doc.read())}
            for i, cusip in enumerate(targets, 1):
                try:
                    _collect_cusip(client, cusip, universe.instruments(cusip), tpl, stats, universe, priced, now)
                except ParseError as exc:  # one odd answer: that bond is skipped (its raw answer is stored)
                    stats.extra.setdefault("parse_errors", {})[cusip] = str(exc)[:300]
                stats.extra["cusips_done"] = i
        finally:
            stats.extra["requests"] = client.requests
            if flow is None:
                client.close()

    try:
        with heartbeat.run(SOURCE_ID) as hb:
            hb.meta = stats.extra
            try:
                body()
            finally:
                hb.meta = stats.as_meta()
            hb.seen(stats.counts.prices_seen + stats.counts.facts_seen)
            hb.new(stats.counts.prices_new + stats.counts.facts_new)
    except (SourceBlocked, FetchError, ParseError) as exc:
        log.warning("%s: %s: %s", SOURCE_ID, type(exc).__name__, exc)
        out = stats.as_meta()
        out.update(ok=False, refused=isinstance(exc, AccessRefused), error=f"{type(exc).__name__}: {exc}")
        return out
    out = stats.as_meta()
    out["ok"] = True
    return out


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="weekdays_1845_et")
@app.task(name="collect.finra_trace", queue="collect", queueing_lock="collect.finra_trace", lock="collect.finra_trace")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if not due_et(timestamp, *RUN_AT_ET):
        return {"skipped": "not 18:45 ET"}
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cusip", action="append", dest="cusips", help="only this CUSIP (repeatable; default: all watched)")
    p.add_argument("--force", action="store_true",
                   help="run now even before the 18:30 ET close or after an ok run this evening")


@cli.action("collect", SOURCE_ID, help="FINRA TRACE end-of-day prices from finra.org bond pages", configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    out = run(cusips=args.cusips, force=args.force)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1
