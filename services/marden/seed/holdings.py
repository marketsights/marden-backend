"""Seed loader ``holdings``: fund holdings and fund marks from marden_data_2026-10-20.zip.

    cd services && uv run python -m marden seed holdings [--write-docs]

Loads (1) N-PORT per-series position lines (aiwc/pxsrc/w2/**, aiwc/pxsrc/agg_*.json), (2) the 30 Jun 2026 BDC
schedules of investments (fundpos.json + watch/ref_meta.json; filing URL, filed date and NAV from the HTML page's
``data`` / ``realnav`` blocks) and (3) insurer NAIC Schedule D lines (aiwc/q3x/ins.json, 31 Dec 2025) into
``holding``, with holder entities, links to instruments (seed/holdings_link.py), fund-mark ``price_obs``
(seed/holdings_prices.py) and BDC NAV facts. Nothing from trace/*, Börse Frankfurt or Yahoo is read. Every file
is stored raw first. Idempotent: rerunning writes no new rows.

``holding`` columns written (one row per filed position line; unique on fund, accession, line_key):

| column          | unit     | meaning                                                                          |
|-----------------|----------|----------------------------------------------------------------------------------|
| fund_entity_id  | id       | N-PORT series / BDC / insurer entity                                             |
| instrument_id   | id       | linked instrument, null without a confident match                                |
| issuer_name     | text     | issuer as filed (N-PORT name, BDC borrower); null for NAIC (in title)            |
| title           | text     | N-PORT title; BDC 'borrower, lien, spread bp, matures YYYY-MM'; 'NAIC Schedule D: ...' |
| cusip, isin     | text     | identifiers on the line (check digit verified; NAIC dashes removed)              |
| period_end      | date     | N-PORT report period; BDC 2026-06-30; NAIC 2025-12-31                            |
| filing_type     | text     | 'N-PORT', '10-Q' (10-K only when the URL says so), 'NAIC Schedule D'             |
| filing_url      | url      | N-PORT primary_doc.xml; BDC 10-Q document; insurer statement source page         |
| accession       | text     | N-PORT adsh; BDC accession from the URL; NAIC 'naic-schedule-d:<period>:<insurer>' |
| filed_at        | date     | filing date (N-PORT, BDC); null for NAIC                                         |
| par             | USD      | principal (N-PORT units PA only; BDC par_k x 1000; NAIC par $m x 1e6)            |
| value_usd       | USD      | fair value as filed                                                              |
| mark_price      | pct      | 100 x value / par; null for non-USD N-PORT lines, negative values, BDC unit errors |
| pct_nav         | percent  | N-PORT pctVal (share of the fund's net assets)                                   |
| fund_net_assets | USD      | BDC net assets at 30 Jun 2026 (realnav); null for N-PORT and NAIC                |
| non_accrual     | bool     | BDC non-accrual flag; null for N-PORT, NAIC and filers with unusable flags       |
| line_key        | text     | stable per filing line: identifier, units, balance, value (BDC: borrower terms)  |

Facts (``producer = 'seed:holdings'``, published):

| subject_type | field             | unit | meaning                                                          |
|--------------|-------------------|------|------------------------------------------------------------------|
| entity       | net_assets_usd    | USD  | BDC net assets at the schedule date (realnav; F, the BDC 10-Q)   |
| entity       | nav_per_share_usd | USD  | BDC net asset value per share at the schedule date (F)           |

``price_obs`` (kind ``fund_mark``): one F row per linked, priced holder line; F cross-fund medians per
instrument, period and cohort (and per CUSIP for multi-CUSIP instruments); M unfunded-adjusted BDC DDTL marks.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from marden import cli
from marden.core import db, heartbeat
from marden.core.config import REPO_ROOT
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.records import HoldingWriter, PriceWriter
from marden.core.sources import get_spec, upsert_spec
from marden.seed import instruments_prices as IP
from marden.seed.common import SEED_SOURCE_ID, as_of, producer, source_name_for_url, tag_for_url
from marden.seed.holdings_archive import HTML, HoldingsArchive, read_holdings_archive
from marden.seed.holdings_entities import ensure_funds
from marden.seed.holdings_lines import HoldingLine
from marden.seed.holdings_link import (
    Catalog,
    LinkReport,
    bdc_anchors,
    link_bdc,
    link_naic,
    link_nport,
    load_catalog,
    nport_anchors,
)
from marden.seed.holdings_prices import PriceRow, adjusted_prices, holder_prices, median_prices
from marden.seed.instruments import LOAN_KINDS
from marden.seed.instruments_archive import fund_citations, research_notes
from marden.seed.instruments_ids import build_identifiers

LOADER = "holdings"
PRODUCER = producer(LOADER)
CHUNK = 2000


@dataclass
class LoadReport:
    counts: Counter[str] = field(default_factory=Counter)
    skipped: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    link: LinkReport = field(default_factory=LinkReport)
    linked_instruments: Counter[str] = field(default_factory=Counter)

    def skip(self, reason: str, item: str) -> None:
        self.skipped[reason].append(item)


def _cited_marks(arc: HoldingsArchive, cat: Catalog) -> tuple[dict[Any, set[str]], dict[Any, set[str]]]:
    """The filing lines the instruments loader recomputed each instrument's fund mark from (same rules, in
    memory), so both loaders agree on those lines."""
    base = arc.base
    idents, _ = build_identifiers(base.rows, base.ids, research_notes(base), base.records, base.bondq_list)
    loanish = {i: (cat.insts[i].kind if i in cat.insts else "") in LOAN_KINDS for i in base.rows}
    citations = fund_citations(base)
    nport = IP.nport_marks(citations, base.records, idents, base.rows, loanish)
    bdc, _ = IP.bdc_marks(citations, base.fundpos, base.bdc_names, base.rows)
    return nport_anchors(nport), bdc_anchors(bdc)


def _dedupe_by_entity(lines: list[HoldingLine], report: LoadReport) -> list[HoldingLine]:
    seen: set[tuple[str | None, str, str]] = set()
    out = []
    for ln in lines:
        key = (ln.fund_id, ln.accession, ln.line_key)
        if key in seen:
            report.skip("same filing line under two spellings of one fund (kept once)", f"{ln.fund_name}: {ln.title}")
            continue
        seen.add(key)
        out.append(ln)
    return out


def _existing_holdings(conn: Conn) -> dict[tuple[str, str, str], tuple[int, str | None]]:
    rows = conn.execute("SELECT id, fund_entity_id, accession, line_key, instrument_id FROM holding").fetchall()
    return {(r["fund_entity_id"], r["accession"], r["line_key"]): (r["id"], r["instrument_id"]) for r in rows}


def _write_holdings(conn: Conn, lines: list[HoldingLine], docs: dict[str, int],
                    existing: dict[tuple[str, str, str], tuple[int, str | None]], report: LoadReport) -> None:
    writer = HoldingWriter(conn)
    for ln in lines:
        assert ln.fund_id is not None
        report.counts[f"holding.{ln.source}.seen"] += 1
        found = existing.get((ln.fund_id, ln.accession, ln.line_key))
        if found is not None:
            hid, current = found
            report.counts[f"holding.{ln.source}.existing"] += 1
            if current is None and ln.instrument_id and writer.link_instrument(hid, ln.instrument_id):
                report.counts[f"holding.{ln.source}.linked_now"] += 1
            elif current is not None and current != ln.instrument_id:
                report.skip("existing holding keeps its earlier instrument link (not changed)",
                            f"holding {hid}: {current} vs {ln.instrument_id}")
            continue
        res = writer.add(
            ln.fund_id, ln.accession, ln.line_key, period_end=ln.period_end, filing_type=ln.filing_type,
            filing_url=ln.filing_url, filed_at=ln.filed_at, instrument_id=ln.instrument_id,
            issuer_name=ln.issuer_name, title=ln.title, cusip=ln.cusip, isin=ln.isin, par=ln.par,
            value_usd=ln.value, mark_price=ln.mark, pct_nav=ln.pct_nav, fund_net_assets=ln.fund_net_assets,
            non_accrual=ln.non_accrual, document_id=docs[ln.relpath],
        )
        report.counts[f"holding.{ln.source}.{'created' if res.created else 'existing'}"] += 1


def _write_prices(conn: Conn, rows: list[PriceRow], docs: dict[str, int], report: LoadReport) -> None:
    prices = PriceWriter(conn)
    for r in rows:
        res = prices.add(r.instrument_id, r.obs_date, "fund_mark", r.price, tag=r.tag, source_name=r.source_name,
                         source_url=r.source_url, method_version=r.method_version, document_id=docs[r.relpath])
        report.counts[f"price_obs.{r.kind_note}.{'created' if res.created else 'existing'}"] += 1


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _write_navs(conn: Conn, arc: HoldingsArchive, fund_ids: dict[str, str], report: LoadReport) -> None:
    facts = FactWriter(conn)
    for nav in arc.navs:
        eid = fund_ids.get(nav.fund_key)
        if eid is None:
            continue
        for fname, value in (("net_assets_usd", nav.nav_usd), ("nav_per_share_usd", nav.nav_per_share)):
            if value is None:
                continue
            res = facts.add("entity", eid, fname, _number(value), tag=tag_for_url(nav.url),
                            source_name=source_name_for_url(nav.url), source_url=nav.url, valid_from=as_of(nav.as_of),
                            producer=PRODUCER, status="published", unit="USD", document_id=arc.docs[HTML])
            report.counts[f"fact.{'created' if res.created else 'existing'}"] += 1
            report.counts[f"field.entity.{fname}"] += 1


def _chunks(items: list[Any], size: int = CHUNK) -> list[list[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def load(*, select: Callable[[HoldingLine], bool] | None = None) -> LoadReport:
    """Run the loader. ``select`` keeps only some parsed lines (tests load a slice of the real files).

    Entities and links are made in one transaction; holdings, prices and facts follow in chunks, each its own
    transaction (every write marks read models dirty). Every write is idempotent, so a run that stops part way
    is completed by running again.
    """
    upsert_spec(get_spec(SEED_SOURCE_ID))
    report = LoadReport()
    with heartbeat.run(SEED_SOURCE_ID) as hb:
        with db.transaction() as conn:
            arc = read_holdings_archive(conn)
            for reason, items in arc.skipped.items():
                report.skipped[reason].extend(items)
            lines = [ln for ln in arc.lines if select is None or select(ln)]
            cat = load_catalog(conn)
            nport_a, bdc_a = _cited_marks(arc, cat)
            link_nport(lines, cat, nport_a, report.link)
            link_bdc(lines, cat, bdc_a, report.link)
            link_naic(lines, cat, report.link)
            keys = {ln.fund_key for ln in lines} | ({n.fund_key for n in arc.navs} if select is None else set())
            fund_ids, entity_counts = ensure_funds(conn, {k: arc.funds[k] for k in keys})
            report.counts.update({f"entity.{k}": v for k, v in entity_counts.items()})
        for ln in lines:
            ln.fund_id = fund_ids[ln.fund_key]
        lines = _dedupe_by_entity(lines, report)
        with db.transaction() as conn:
            existing = _existing_holdings(conn)
        for chunk in _chunks(lines):
            with db.transaction() as conn:
                _write_holdings(conn, chunk, arc.docs, existing, report)
        adjusted, notes = adjusted_prices(lines)
        for note in notes:
            report.skip("unfunded-adjusted DDTL mark not written", note)
        rows = holder_prices(lines) + median_prices(lines) + adjusted
        for chunk in _chunks(rows):
            with db.transaction() as conn:
                _write_prices(conn, chunk, arc.docs, report)
        with db.transaction() as conn:
            _write_navs(conn, arc, fund_ids, report)
        report.linked_instruments.update(ln.instrument_id for ln in lines if ln.instrument_id)
        report.counts["documents"] = len(arc.docs)
        report.counts["instruments_linked"] = len(report.linked_instruments)
        hb.seen(len(lines))
        hb.new(sum(v for k, v in report.counts.items() if k.startswith("holding.") and k.endswith(".created")))
        hb.meta = {"loader": LOADER, "counts": dict(report.counts),
                   "link_methods": dict(report.link.methods), "unmatched": dict(report.link.unmatched)}
    return report


def _configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--write-docs", action="store_true", help="also rewrite docs/seed-data/unpriced.md from the DB")


@cli.action("seed", LOADER, help="Load N-PORT, BDC and NAIC holdings, holder entities, fund marks and BDC NAV",
            configure=_configure)
def run_cli(args: argparse.Namespace) -> int:
    report = load()
    for key in sorted(report.counts):
        print(f"{key}: {report.counts[key]}")
    for key in sorted(report.link.methods):
        print(f"linked.{key}: {report.link.methods[key]}")
    for key in sorted(report.link.unmatched):
        print(f"unlinked.{key}: {report.link.unmatched[key]}")
    for label, items in sorted(report.link.ambiguous.items()):
        print(f"ambiguous ({len(items)}): {label}: {'; '.join(items[:3])}{' ...' if len(items) > 3 else ''}")
    for reason in sorted(report.skipped):
        items = report.skipped[reason]
        print(f"skipped ({len(items)}): {reason}: {'; '.join(items[:4])}{' ...' if len(items) > 4 else ''}")
    if getattr(args, "write_docs", False):
        from marden.seed.holdings_docs import write_unpriced

        print(f"wrote {write_unpriced(REPO_ROOT / 'docs' / 'seed-data')}")
    return 0
