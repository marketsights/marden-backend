"""Seed loader ``instruments``: the 707 aidebt.json instruments (+ aiwcredit.rowinfo) into Marden.

    cd services && uv run python -m marden seed instruments [--write-docs]

Writes ``instrument`` identity rows (legacy id kept, kind, issuer entity, site slug, primary CUSIP/ISIN,
in_v1), the issuer ``entity`` rows, identity ``site`` rows for case-linked slugs (seed/site_map.json, names
from aiwc/memo/titles.json), sourced facts, and only the prices docs/methods/price-sources.md allows:
press-reported dealer quotes (kind 'dealer', tag R) and fund marks recomputed from the N-PORT or BDC filing
line the instrument cites (kind 'fund_mark', tag F; dedupe contract in seed/instruments_nport.py). No
TRACE, Börse Frankfurt or Yahoo value is stored. Idempotent: rerunning writes no new rows.

Facts (subject_type ``instrument``; tags per seed/instruments_facts.py):

| field                        | unit      | meaning                                                              |
|------------------------------|-----------|----------------------------------------------------------------------|
| coupon                       | text      | coupon or spread as stated ("9.75%", "SOFR+2.50%")                   |
| coupon_type                  | text      | fixed / floating / zero, parsed from coupon                          |
| coupon_rate_pct              | pct       | fixed coupon rate parsed from coupon                                 |
| coupon_benchmark             | text      | floating-rate benchmark (SOFR, EURIBOR, ...)                         |
| coupon_spread_bp             | bp        | spread over the benchmark                                            |
| maturity                     | date      | maturity at the precision stated: YYYY-MM-DD, YYYY-MM or YYYY        |
| maturity_text                | text      | maturity that is not a date ("2027-2066", "Short-term")              |
| amount_usd                   | USD       | size (aidebt amt_m x 1e6)                                            |
| amount_basis                 | text      | 'drawn' or 'holder_par' where the size is not the issue size (M)     |
| seniority                    | text      | ranking as stated ("Secured (SPV)", "Senior unsecured")              |
| status                       | text      | outstanding / proposed / repaid                                      |
| source_urls                  | list      | every source URL the row cites                                       |
| rating                       | text      | rating text as reported (R)                                          |
| tenants                      | text      | tenant / end-user chain text (M)                                     |
| backstop                     | text      | guarantee or backstop text (M)                                       |
| chain                        | text      | obligation chain text (M)                                            |
| flags                        | list      | legacy stress flags (M)                                              |
| holder_notes                 | list      | legacy holder strings (M)                                            |
| legacy_score                 | score_0_8 | legacy aidebt stress score (M)                                       |
| legacy_category              | text      | aidebt cat: corp / abs / util / proj / bdc (M)                       |
| exposure_links               | list      | [{case, role}] cases the instrument is exposed to (rowinfo.x, M)     |
| identifiers                  | list      | [{type: cusip/isin/loanx/common_code, value, from}] (F if on N-PORT) |
| issuer_fair_value_disclosure | text      | issuer 10-Q fair-value statement; a fact, not a price (F)            |
| legacy_price_status          | text      | what became of the legacy px (blocked copy, recomputed, ...) (M)     |
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from marden import cli
from marden.core import db, heartbeat
from marden.core.config import REPO_ROOT
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.records import PriceWriter
from marden.core.sources import get_spec, upsert_spec
from marden.seed import instruments_facts as F
from marden.seed import instruments_prices as P
from marden.seed.cited import CitedDocs
from marden.seed.common import SEED_SOURCE_ID, as_of, ensure_entity, producer, source_name_for_url
from marden.seed.instruments_archive import (
    AIDEBT,
    AIWCREDIT,
    Archive,
    cites_fund_filing,
    fund_citations,
    price_records,
    read_archive,
    read_blocked_in_memory,
    research_notes,
)
from marden.seed.instruments_ids import build_identifiers
from marden.seed.instruments_parse import core_name, entity_kind, instrument_kind, is_unnamed
from marden.seed.instruments_v1 import V1_IDS

LOADER = "instruments"
PRODUCER = producer(LOADER)
SITE_MAP = Path(__file__).with_name("site_map.json")
LOAN_KINDS = frozenset({"construction_loan", "term_loan", "ddtl", "facility", "lease", "other"})
FACT_CHUNK = 25


@dataclass
class LoadReport:
    counts: Counter[str] = field(default_factory=Counter)
    skipped: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    legacy_status: dict[str, str] = field(default_factory=dict)
    price_kinds: dict[str, set[str]] = field(default_factory=dict)

    def skip(self, reason: str, iid: str) -> None:
        self.skipped[reason].append(iid)


def _site_map() -> dict[str, dict[str, Any]]:
    return {s["case"]: s for s in json.loads(SITE_MAP.read_text())["sites"]}


def _ensure_site(conn: Conn, case: str, arc: Archive, sites: dict[str, dict[str, Any]]) -> str | None:
    site = sites.get(case)
    if site is None:
        return None
    name = (arc.titles.get(case) or [None])[0] or site["slug"]
    conn.execute(
        "INSERT INTO site (slug, name, legacy_group, in_v1) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (site["slug"], name, case, bool(site["in_v1"])),
    )
    return site["slug"]


def _issuer_entity(conn: Conn, row: dict[str, Any], arc: Archive, report: LoadReport) -> str | None:
    issuer = " ".join(str(row.get("issuer") or "").split())
    if is_unnamed(issuer):
        report.skip("issuer not named in the archive: no entity, issuer_name kept", row["id"])
        return None
    group_name, ticker = (arc.gn.get(row.get("grp") or "") or ["", ""])[:2]
    if ticker:  # a listed group: the issuer is the company itself or one of its subsidiaries
        parent = ensure_entity(conn, group_name, "company", ticker=ticker)
        if core_name(issuer) == core_name(group_name):
            return ensure_entity(conn, group_name, "company", aliases=[issuer])
        return ensure_entity(conn, issuer, entity_kind(row.get("cat")), parent_id=parent)
    return ensure_entity(conn, issuer, entity_kind(row.get("cat")))


def _write_instruments(conn: Conn, arc: Archive, idents: dict[str, Any], report: LoadReport) -> dict[str, str]:
    sites = _site_map()
    kinds: dict[str, str] = {}
    for iid, row in arc.rows.items():
        info = arc.rowinfo.get(iid) or {}
        group_name = (arc.gn.get(row.get("grp") or "") or [""])[0]
        kind = instrument_kind(row, convertible_flag=bool(info.get("cv")), group_name=group_name)
        kinds[iid] = kind
        site_slug = _ensure_site(conn, info["c"], arc, sites) if info.get("c") else None
        ident = idents[iid]
        created = conn.execute(
            """INSERT INTO instrument (id, name, kind, issuer_entity_id, issuer_name, site_slug, cusip, isin, in_v1)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind,
                 issuer_entity_id = EXCLUDED.issuer_entity_id, issuer_name = EXCLUDED.issuer_name,
                 site_slug = EXCLUDED.site_slug, cusip = EXCLUDED.cusip, isin = EXCLUDED.isin, in_v1 = EXCLUDED.in_v1
               RETURNING (xmax = 0) AS created""",
            (iid, " ".join(str(row.get("inst") or iid).split()), kind, _issuer_entity(conn, row, arc, report),
             " ".join(str(row.get("issuer") or "n/d").split()), site_slug, ident.cusip, ident.isin, iid in V1_IDS),
        ).fetchone()
        report.counts["instrument.created" if created and created["created"] else "instrument.updated"] += 1
        report.counts[f"kind.{kind}"] += 1
    return kinds


def _write_prices(conn: Conn, arc: Archive, idents: dict[str, Any], kinds: dict[str, str],
                  report: LoadReport) -> tuple[set[str], set[tuple[str, str]]]:
    """Dealer quotes and recomputed fund marks. Returns (ids with a dealer quote at their legacy px,
    (id, url) of recomputed marks)."""
    prices = PriceWriter(conn)
    dealer_at_px: set[str] = set()
    for q in P.dealer_quotes(price_records(arc)):
        if q.instrument_id not in arc.rows:
            continue
        res = prices.add(q.instrument_id, q.obs_date, "dealer", q.price, tag="R", source_name=source_name_for_url(
            q.url), source_url=q.url, document_id=arc.docs[q.relpath])
        report.counts["price_obs.dealer.created" if res.created else "price_obs.dealer.existing"] += 1
        row = arc.rows[q.instrument_id]
        if row.get("pxd") == q.obs_date.isoformat() and P.close_price(row.get("px"), q.price):
            dealer_at_px.add(q.instrument_id)
    citations = fund_citations(arc)
    loanish = {i: kinds[i] in LOAN_KINDS for i in arc.rows}
    marks = P.nport_marks(citations, arc.records, idents, arc.rows, loanish)
    bdc, skipped = P.bdc_marks(citations, arc.fundpos, arc.bdc_names, arc.rows)
    for iid, url, reason in skipped:
        report.skip(f"BDC mark not loaded: {reason}", f"{iid} <- {url.rsplit('/', 1)[-1]}")
    recomputed: set[tuple[str, str]] = set()
    for m in marks + bdc:
        res = prices.add(m.instrument_id, m.obs_date, "fund_mark", m.price, tag="F", source_name=m.source_name,
                         source_url=m.source_url, document_id=arc.docs[m.relpath])
        report.counts[f"price_obs.fund_mark.{m.method}.{'created' if res.created else 'existing'}"] += 1
        recomputed.add((m.instrument_id, m.source_url))
    return dealer_at_px, recomputed


def _legacy_status(iid: str, row: dict[str, Any], arc: Archive, blocked: dict[str, str], dealer_at_px: set[str],
                   recomputed: set[tuple[str, str]]) -> str | None:
    if row.get("px") is None:
        return None
    if iid in P.ISSUER_FAIR_VALUE_IDS:
        return P.ISSUER_FAIR_VALUE
    if iid in blocked:
        return blocked[iid]
    if iid in dealer_at_px:
        return P.DEALER_LOADED
    own = set(row.get("src") or []) | {(arc.pxsrc.get(iid) or {}).get("url")}
    if any((iid, u) in recomputed for u in own):
        return P.MARK_RECOMPUTED
    if cites_fund_filing(row, arc.pxsrc.get(iid), set(arc.fundpos)):
        return P.MARK_NOT_RECOMPUTABLE
    return P.NOT_A_FUND_FILING


def _seeded_prices(conn: Conn, ids: list[str]) -> dict[str, set[str]]:
    """Kinds of price_obs per instrument stored from seed files (any seed loader), for the docs."""
    kinds: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute(
        """SELECT DISTINCT p.instrument_id, p.kind FROM price_obs p JOIN document d ON d.id = p.document_id
           WHERE p.instrument_id = ANY(%s) AND p.price IS NOT NULL AND d.source_id = %s""",
        (ids, SEED_SOURCE_ID),
    ).fetchall():
        kinds[r["instrument_id"]].add(r["kind"])
    return kinds


RESOURCED = "re-sourced: a money or term value is F only when a filed document the archive row cites states it"


def _supersede_replaced(conn: Conn, facts: FactWriter, iid: str, written: dict[str, int], report: LoadReport) -> None:
    """Earlier rows of this loader for a term this run wrote differently (another tag or source) are superseded
    by the new row, so a rerun after a sourcing fix corrects the value's tag and keeps the old row as history."""
    if not written:
        return
    stale = conn.execute(
        """SELECT id, field FROM fact WHERE subject_type = 'instrument' AND subject_id = %s AND producer = %s
             AND status = 'published' AND field = ANY(%s)""",
        (iid, PRODUCER, list(written)),
    ).fetchall()
    replaced = {r["id"]: written[r["field"]] for r in stale if r["id"] != written[r["field"]]}
    done = facts.supersede(replaced, RESOURCED, replaced_by=replaced, decided_by=PRODUCER)
    report.counts["fact.superseded"] += len(done)


FUND_FILINGS = 8  # newest fund filings listing an instrument that are checked for its terms


def fund_filings(conn: Conn, ids: list[str], idents: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """Fund filings (N-PORT, BDC 10-Q/10-K) whose holdings lines name each instrument, newest first: the
    archive's N-PORT records for its identifiers, then the stored ``holding`` lines linked to it."""
    out: dict[str, list[str]] = defaultdict(list)
    for iid in ids:
        ident = (idents or {}).get(iid)
        for rec in sorted(getattr(ident, "nport_records", []) or [], key=lambda r: (r.period, r.url), reverse=True):
            out[iid].append(rec.url)
    for r in conn.execute(
        """SELECT instrument_id, filing_url FROM holding WHERE instrument_id = ANY(%s) AND filing_url IS NOT NULL
            GROUP BY instrument_id, filing_url ORDER BY instrument_id, max(period_end) DESC, filing_url""",
        (ids,),
    ).fetchall():
        out[r["instrument_id"]].append(r["filing_url"])
    return {k: list(dict.fromkeys(v))[:FUND_FILINGS] for k, v in out.items()}


def _write_facts(conn: Conn, arc: Archive, chunk: list[str], idents: dict[str, Any], kinds: dict[str, str],
                 status: dict[str, str], report: LoadReport) -> None:
    facts = FactWriter(conn)
    cited = CitedDocs(conn)
    seeded = _seeded_prices(conn, chunk)
    funds = fund_filings(conn, chunk, idents)
    for iid in chunk:
        row = arc.rows[iid]
        ident = idents[iid]
        rows: list[F.FactRow] = F.terms_facts(iid, row, kinds[iid], AIDEBT, cited,
                                              ids=[*ident.cusips, *ident.isins], also=funds.get(iid, []))
        rating = F.rating_fact(row, AIDEBT)
        rows += [rating] if rating else []
        raw_rating = (row.get("rating") or "").strip()
        if rating is None and raw_rating.lower().startswith("no rating found"):
            report.skip("rating text says no instrument rating was found: not written", iid)
        elif rating is None and raw_rating:
            report.skip("rating read off finra.org's TRACE security master ('via FINRA'): not written", iid)
        elif rating is not None and rating.value != raw_rating:
            report.skip("rating clause read off finra.org removed, rest kept", iid)
        judged, skips = F.judgment_facts(row, AIDEBT)
        rows += judged
        for s in skips:
            report.skip(s, iid)
        for extra in (F.exposure_fact(arc.rowinfo.get(iid) or {}, AIWCREDIT, arc.aiwcredit_as_of),
                      F.identifier_fact(idents[iid], AIWCREDIT)):
            rows += [extra] if extra else []
        if iid in P.ISSUER_FAIR_VALUE_IDS and arc.pxsrc.get(iid):
            src = arc.pxsrc[iid]
            rows.append(F.FactRow("issuer_fair_value_disclosure", src["label"], "F", source_name_for_url(src["url"]),
                                  src["url"], AIDEBT, as_of(row.get("pxd"))))
        if status.get(iid):
            rows.append(F._m("legacy_price_status", status[iid], AIDEBT, F.LOADER_METHOD))
        report.price_kinds[iid] = seeded.get(iid, set())  # for the docs only, never a fact (other writers add prices)
        written: dict[str, int] = {}
        for fr in rows:
            res = facts.add(
                "instrument", iid, fr.field, fr.value, tag=fr.tag, source_name=fr.source_name, source_url=fr.source_url,
                valid_from=fr.valid_from, producer=PRODUCER, status="published", unit=fr.unit,
                document_id=fr.document_id or arc.docs.get(fr.relpath), method_version=fr.method_version,
            )
            report.counts["fact.created" if res.created else "fact.existing"] += 1
            report.counts[f"field.{fr.field}"] += 1
            if fr.field in F.CHECKED_TERMS and res.status == "published":
                written[fr.field] = res.id
            if fr.flag:
                facts.request_review(res.id, title=f"{fr.field} on {iid}: {F.UNVERIFIED}", reason=fr.flag,
                                     payload={"subject_type": "instrument", "subject_id": iid, "field": fr.field})
                report.skip(f"{F.UNVERIFIED}: no cited filing states the value and the row has no press source",
                            f"{iid}.{fr.field}")
        _supersede_replaced(conn, facts, iid, written, report)


def load(*, only: set[str] | None = None) -> LoadReport:
    """Run the loader (``only``: restrict to these instrument ids, for tests).

    Identity rows and prices are written in one transaction; facts follow in chunks of FACT_CHUNK
    instruments, each its own transaction, because every published fact re-marks the same read-model
    rows dirty and one huge transaction makes those updates slow. Every write is idempotent, so a run
    that stops part way is completed by running again.
    """
    upsert_spec(get_spec(SEED_SOURCE_ID))
    report = LoadReport()
    with heartbeat.run(SEED_SOURCE_ID) as hb:
        with db.transaction() as conn:
            arc, idents, kinds, status = _identity_and_prices(conn, only, report)
        ids = list(arc.rows)
        for start in range(0, len(ids), FACT_CHUNK):
            with db.transaction() as conn:
                _write_facts(conn, arc, ids[start:start + FACT_CHUNK], idents, kinds, status, report)
        report.counts["documents"] = len(arc.docs)
        hb.seen(len(arc.rows))
        hb.new(report.counts["instrument.created"])
        hb.meta = {"loader": LOADER, "counts": dict(report.counts)}
    return report


def _identity_and_prices(
    conn: Conn, only: set[str] | None, report: LoadReport
) -> tuple[Archive, dict[str, Any], dict[str, str], dict[str, str]]:
    arc = read_archive(conn, only=only)
    trace_rows, bondq = read_blocked_in_memory()
    blocked = P.classify_legacy_px(arc.rows, trace_rows, bondq)
    del trace_rows, bondq  # recognised in memory; nothing from them is kept
    idents, id_report = build_identifiers(arc.rows, arc.ids, research_notes(arc), arc.records, arc.bondq_list)
    for iid, isin in id_report.bondq_uncorroborated:
        report.skip("bondq_list ISIN not corroborated by an allowed identifier source", f"{iid} {isin}")
    for key, iids in id_report.cusip_collisions.items():
        report.skip("identifier shared by several instruments (kept on each)", f"{key}: {', '.join(iids)}")
    kinds = _write_instruments(conn, arc, idents, report)
    dealer_at_px, recomputed = _write_prices(conn, arc, idents, kinds, report)
    status = {iid: s for iid, row in arc.rows.items()
              if (s := _legacy_status(iid, row, arc, blocked, dealer_at_px, recomputed))}
    report.legacy_status = status
    report.counts.update(f"legacy_px.{s}" for s in status.values())
    return arc, idents, kinds, status


def _configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--write-docs", action="store_true",
                   help="also write docs/seed-data/v1-instruments.md and unpriced.md from this run")


@cli.action("seed", LOADER, help="Load aidebt.json instruments, facts, dealer quotes and recomputed fund marks",
            configure=_configure)
def run_cli(args: argparse.Namespace) -> int:
    report = load()
    for key in sorted(report.counts):
        print(f"{key}: {report.counts[key]}")
    for reason in sorted(report.skipped):
        items = report.skipped[reason]
        print(f"skipped ({len(items)}): {reason}: {'; '.join(items[:6])}{' ...' if len(items) > 6 else ''}")
    if getattr(args, "write_docs", False):
        from marden.seed.instruments_docs import write_docs

        write_docs(REPO_ROOT / "docs" / "seed-data", report)
    return 0
