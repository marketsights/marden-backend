"""Seed loader ``legacy_yahoo``: the archive's Yahoo Finance chart pulls -> series ``equity:<TICKER>``.

    cd services && uv run python -m marden seed legacy_yahoo

Reads ``aiwc/research/dl/y_*.json`` (Yahoo chart API responses saved by the legacy research, Oct 2026),
stores each file raw as a seed document, parses it with the ``yahoo_equity`` collector's parser and writes
the same series facts the collector writes (open, high, low, close, adj_close, volume; tag F, source_name
'Yahoo Finance', source_url the ticker's Yahoo history page), with producer ``seed:legacy_yahoo`` and the
seed file as the document. Unlicensed source: see docs/methods/licensing.md. Idempotent.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

from marden import cli
from marden.collectors import yahoo_equity as Y
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, heartbeat
from marden.core.sources import get_spec, upsert_spec
from marden.seed.common import SEED_SOURCE_ID, producer, seed_dir, seed_document

LOADER = "legacy_yahoo"
PRODUCER = producer(LOADER)
GLOB = "aiwc/research/dl/y_*.json"


def files() -> list[str]:
    root = seed_dir()
    return sorted(str(p.relative_to(root)) for p in root.glob(GLOB) if p.is_file())


def ticker_of(relpath: str) -> str:
    return relpath.rsplit("/", 1)[-1].removeprefix("y_").removesuffix(".json").upper()


def load() -> dict[str, Any]:
    """Load every y_*.json file. Returns counts per file and in total."""
    upsert_spec(get_spec(SEED_SOURCE_ID))
    upsert_spec(Y.SOURCE)  # the Yahoo source row carries the terms note for these values
    stats = RunStats()
    report: dict[str, Any] = {"loader": LOADER, "files": {}, "skipped": {}}
    counts: Counter[str] = Counter()
    with heartbeat.run(SEED_SOURCE_ID) as hb:
        for relpath in files():
            ticker = ticker_of(relpath)
            with db.transaction() as conn:
                doc = seed_document(relpath, doc_type="yahoo_chart_json", conn=conn)
                try:
                    chart = Y.parse_chart(doc.read(), symbol=ticker)
                except ParseError as exc:
                    report["skipped"][relpath] = f"{type(exc).__name__}: {exc}"
                    continue
                new = Y.write_chart(conn, ticker, chart, producer=PRODUCER, document_id=doc.id, stats=stats)
            report["files"][relpath] = {
                "series": Y.series_id(ticker), "bars": len(chart.bars), "facts_new": new,
                "first": chart.bars[0].day.isoformat() if chart.bars else None,
                "last": chart.bars[-1].day.isoformat() if chart.bars else None,
                "unfinished_sessions_skipped": [d.isoformat() for d in chart.unfinished],
            }
            counts["files"] += 1
            counts["bars"] += len(chart.bars)
        counts["facts_seen"], counts["facts_new"] = stats.facts_seen, stats.facts_new
        hb.seen(stats.facts_seen)
        hb.new(stats.facts_new)
        hb.meta = {"loader": LOADER, "counts": dict(counts)}
    report["counts"] = dict(counts)
    return report


@cli.action("seed", LOADER, help="Load the archive's Yahoo chart pulls (y_*.json) into series equity:<TICKER>")
def run_cli(args: argparse.Namespace) -> int:
    print(json.dumps(load(), indent=2, default=str))
    return 0
