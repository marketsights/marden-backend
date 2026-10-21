"""Seed loader ``legacy_frankfurt``: the archive's Börse Frankfurt fixings -> price_obs kind 'exchange'.

    cd services && uv run python -m marden seed legacy_frankfurt

Two inputs, both stored raw as seed documents:

1. ``watch/**/bondq.json``: snapshots of the legacy "bondq" watcher ({asof, src, q: [{id, isin, last,
   prev, chg, ts, hi52, lo52}]}). Each quote becomes a price_obs on every instrument whose ISIN, CUSIP or
   ``identifiers`` carry the quote's ISIN (market_watch.watched_isins, the universe the collector reads),
   never on an instrument only the archive's own ``id`` names: such quotes are reported (``unmatched``,
   with that id) until the instruments loader adds the ISIN to the identifiers. price = ``last`` (percent of
   par), obs_at = ``ts`` (the fixing time), obs_date = its Frankfurt date, tag R, source_name
   'Börse Frankfurt', source_url the bond's live.deutsche-boerse.com page: the same keys the
   ``boerse_frankfurt`` collector uses, so a fixing seen in several snapshots is stored once.
2. ``aidebt.json`` ``px``/``pxd`` values that are copies of those fixings (same instrument and date,
   within 0.0051 of ``last``; docs/methods/price-sources.md section 3). The fixing itself is written
   (not the rounded copy), so these rows coincide with the snapshot rows; the report counts them.

The snapshots carry no bid/ask, so no ``exchange_quote`` fact is written. Unlicensed source: see
docs/methods/licensing.md. Idempotent: rerunning writes no new rows.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from marden import cli
from marden.collectors import boerse_frankfurt as FWB
from marden.collectors.market_watch import isin_valid, watched_isins
from marden.core import db, heartbeat
from marden.core.db import Conn
from marden.core.records import PriceWriter
from marden.core.sources import get_spec, upsert_spec
from marden.seed.common import SEED_SOURCE_ID, load_json, seed_dir, seed_document

LOADER = "legacy_frankfurt"
SNAPSHOT_GLOB = "watch/**/bondq.json"
AIDEBT = "aidebt.json"
COPY_TOLERANCE = Decimal("0.0051")  # legacy prices are the source value rounded to 2 decimals


@dataclass(frozen=True)
class Fixing:
    quote_id: str  # the legacy instrument id on the quote
    isin: str
    price: Decimal
    at: datetime
    relpath: str

    @property
    def local_day(self) -> str:
        return self.at.astimezone(FWB.BERLIN).date().isoformat()


def snapshot_files() -> list[str]:
    root = seed_dir()
    return sorted(str(p.relative_to(root)) for p in root.glob(SNAPSHOT_GLOB) if p.is_file())


def parse_snapshot(payload: Any, relpath: str) -> tuple[list[Fixing], list[str]]:
    """(fixings, problems) from one bondq.json snapshot."""
    if not isinstance(payload, dict) or not isinstance(payload.get("q"), list):
        return [], [f"{relpath}: not a bondq snapshot"]
    out: list[Fixing] = []
    problems: list[str] = []
    for q in payload["q"]:
        isin = str(q.get("isin") or "").upper()
        try:
            price = Decimal(str(q["last"]))
            at = datetime.fromisoformat(str(q["ts"]))
        except (KeyError, InvalidOperation, ValueError, TypeError):
            problems.append(f"{relpath}: {q.get('id')} has no usable last/ts")
            continue
        if at.tzinfo is None or not isin_valid(isin) or not Decimal(0) < price < Decimal(1000):
            problems.append(f"{relpath}: {q.get('id')} {isin} {price} {q.get('ts')} rejected")
            continue
        out.append(Fixing(str(q.get("id") or ""), isin, price, at, relpath))
    return out, problems


def _targets(conn: Conn, fixings: list[Fixing]) -> dict[tuple[str, str], list[str]]:
    """(quote id, isin) -> the instruments whose identifiers carry the ISIN (as the collector maps it)."""
    by_isin = watched_isins(conn)
    return {(f.quote_id, f.isin): by_isin.get(f.isin, []) for f in fixings}


def aidebt_copies(rows: dict[str, dict[str, Any]], fixings: list[Fixing]) -> dict[str, Fixing]:
    """aidebt id -> the fixing its px copies (same instrument, same date, within COPY_TOLERANCE)."""
    by_id: dict[str, list[Fixing]] = defaultdict(list)
    for f in fixings:
        by_id[f.quote_id].append(f)
    out: dict[str, Fixing] = {}
    for iid, row in rows.items():
        px, pxd = row.get("px"), row.get("pxd")
        if px is None or not pxd:
            continue
        for f in sorted(by_id.get(iid, []), key=lambda x: (x.at, x.price)):
            if str(f.at.date()) == str(pxd)[:10] and abs(f.price - Decimal(str(px))) <= COPY_TOLERANCE:
                out[iid] = f
                break
    return out


def _aidebt_rows() -> dict[str, dict[str, Any]]:
    payload = load_json(AIDEBT)
    cols = payload["cols"].split(",") if isinstance(payload["cols"], str) else payload["cols"]
    return {r[0]: dict(zip(cols, r, strict=True)) for r in payload["rows"]}


def _write(prices: PriceWriter, iid: str, f: Fixing, doc_id: int) -> bool:
    return prices.add(
        iid, f.local_day, "exchange", f.price, tag="R", source_name=FWB.SOURCE_NAME, source_url=FWB.page_url(f.isin),
        obs_at=f.at, document_id=doc_id,
    ).created


def load() -> dict[str, Any]:
    upsert_spec(get_spec(SEED_SOURCE_ID))
    upsert_spec(FWB.SOURCE)  # the Börse Frankfurt source row carries the terms note for these values
    counts: Counter[str] = Counter()
    problems: list[str] = []
    unmatched: set[str] = set()
    with heartbeat.run(SEED_SOURCE_ID) as hb:
        with db.transaction() as conn:
            docs: dict[str, int] = {}
            fixings: list[Fixing] = []
            for relpath in snapshot_files():
                docs[relpath] = seed_document(relpath, conn=conn).id
                found, bad = parse_snapshot(load_json(relpath), relpath)
                fixings += found
                problems += bad
                counts["snapshots"] += 1
                counts["snapshot_quotes"] += len(found)
            targets = _targets(conn, fixings)
            prices = PriceWriter(conn)
            done: set[tuple[str, str, datetime, Decimal]] = set()  # one write per instrument and fixing
            for f in fixings:
                iids = targets[(f.quote_id, f.isin)]
                if f.quote_id not in iids:  # the archive's id is not an instrument carrying this ISIN
                    unmatched.add(f"{f.quote_id} {f.isin}")
                for iid in iids:
                    if (iid, f.isin, f.at, f.price) in done:
                        continue
                    done.add((iid, f.isin, f.at, f.price))
                    created = _write(prices, iid, f, docs[f.relpath])
                    counts["price_obs.created" if created else "price_obs.existing"] += 1
            counts["distinct_fixings"] = len({(f.isin, f.at, f.price) for f in fixings})
            counts["instruments"] = len({i for f in fixings for i in targets[(f.quote_id, f.isin)]})

            docs[AIDEBT] = seed_document(AIDEBT, conn=conn).id
            copies = aidebt_copies(_aidebt_rows(), fixings)
            counts["aidebt_px_frankfurt_copies"] = len(copies)
            for iid, f in sorted(copies.items()):
                if iid not in targets.get((f.quote_id, f.isin), []):
                    unmatched.add(f"aidebt {iid}")
                    continue
                key = "aidebt_copy.created" if _write(prices, iid, f, docs[AIDEBT]) else "aidebt_copy.same_as_snapshot"
                counts[key] += 1
        hb.seen(counts["price_obs.created"] + counts["price_obs.existing"])
        hb.new(counts["price_obs.created"] + counts["aidebt_copy.created"])
        hb.meta = {"loader": LOADER, "counts": dict(counts)}
    return {"loader": LOADER, "counts": dict(counts), "unmatched": sorted(unmatched), "problems": problems}


@cli.action("seed", LOADER, help="Load the archive's Börse Frankfurt fixings (bondq snapshots, aidebt copies)")
def run_cli(args: argparse.Namespace) -> int:
    print(json.dumps(load(), indent=2, default=str))
    return 0
