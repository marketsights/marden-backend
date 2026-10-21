"""Reading the archive files the instruments loader uses (raw first: each stored as a seed document).

Exception to raw-first, by rule: trace/trace_rows.json and watch/*/bondq.json are read in memory only to
recognise legacy prices that copy them (finra.org terms, Börse Frankfurt licence). They are never stored as
documents and nothing read from them is written anywhere. aiwc/pxsrc/case_px.json (TRACE last-sale rows) is
not read at all; its one dealer quote is also in aidebt.json, aiwcredit.json and out1.json.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.seed.common import as_of, load_json, seed_dir, seed_document
from marden.seed.instruments_nport import (
    FUNDPOS,
    REF_META,
    BdcLine,
    NportRecord,
    bdc_names,
    nport_files,
    parse_fundpos,
    parse_nport_file,
)

AIDEBT = "aidebt.json"
AIWCREDIT = "aiwcredit.json"
OUT1 = "aiwc/pxsrc/out1.json"
OUT2 = "aiwc/pxsrc/out2.json"
TITLES = "aiwc/memo/titles.json"
BONDQ_LIST = "watch/bondq_list.json"
STORED = (AIDEBT, AIWCREDIT, OUT1, OUT2, TITLES, BONDQ_LIST, REF_META, FUNDPOS)
TRACE_ROWS = "trace/trace_rows.json"  # in memory only
BONDQ_GLOB = "watch/**/bondq.json"  # in memory only


@dataclass
class Archive:
    rows: dict[str, dict[str, Any]]
    rowinfo: dict[str, dict[str, Any]]
    ids: dict[str, str]
    pxs: dict[str, dict[str, Any]]
    pxsrc: dict[str, dict[str, Any]]
    gn: dict[str, list[str]]
    out1: dict[str, dict[str, Any]]
    out2: dict[str, dict[str, Any]]
    titles: dict[str, list[str]]
    bondq_list: list[list[Any]]
    records: list[NportRecord]
    fundpos: dict[str, list[BdcLine]]
    bdc_names: dict[str, str]
    aiwcredit_as_of: date
    docs: dict[str, int] = field(default_factory=dict)  # relpath -> document id


def read_archive(conn: Conn, *, only: set[str] | None = None) -> Archive:
    docs: dict[str, int] = {}

    def stored(relpath: str) -> Any:
        docs[relpath] = seed_document(relpath, conn=conn).id
        return load_json(relpath)

    aidebt, aiwcredit = stored(AIDEBT), stored(AIWCREDIT)
    cols = aidebt["cols"].split(",") if isinstance(aidebt["cols"], str) else aidebt["cols"]
    rows = {r[0]: dict(zip(cols, r, strict=True)) for r in aidebt["rows"]}
    if only is not None:
        rows = {k: v for k, v in rows.items() if k in only}
    records: list[NportRecord] = []
    for relpath in nport_files(seed_dir()):
        records.extend(parse_nport_file(relpath, stored(relpath)))
    return Archive(
        rows=rows, rowinfo=aiwcredit["rowinfo"], ids=aiwcredit["ids"], pxs=aiwcredit["pxs"],
        pxsrc=aidebt.get("pxsrc", {}), gn=aidebt.get("gn", {}), out1=stored(OUT1), out2=stored(OUT2),
        titles=stored(TITLES), bondq_list=stored(BONDQ_LIST), records=records,
        fundpos=parse_fundpos(stored(FUNDPOS)), bdc_names=bdc_names(stored(REF_META)),
        aiwcredit_as_of=as_of(aiwcredit.get("asof")), docs=docs,
    )


def read_blocked_in_memory() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The TRACE pull and the Frankfurt snapshots, for recognising copies only (never stored)."""
    root = seed_dir()
    snaps = [load_json(str(p.relative_to(root))) for p in sorted(root.glob(BONDQ_GLOB)) if p.is_file()]
    return load_json(TRACE_ROWS), snaps


def price_records(arc: Archive) -> list[tuple[str, dict[str, dict[str, Any]]]]:
    """(relpath, {id: price record}) in priority order, for dealer quotes. aidebt rows carry px/pxd and
    aidebt.pxsrc carries their kind and URL."""
    aidebt = {
        i: {"kind": src.get("kind"), "px": arc.rows[i].get("px"), "date": arc.rows[i].get("pxd"), "url": src.get("url")}
        for i, src in arc.pxsrc.items() if i in arc.rows
    }
    return [(AIDEBT, aidebt), (AIWCREDIT, arc.pxs), (OUT1, arc.out1), (OUT2, arc.out2)]


def research_notes(arc: Archive) -> dict[str, list[str]]:
    """Per-instrument research text (labels and notes of its own price records), for identifiers."""
    out: dict[str, list[str]] = defaultdict(list)
    for records in (arc.pxs, arc.out1, arc.out2):
        for iid, rec in records.items():
            for r in (rec, rec.get("fund") if isinstance(rec.get("fund"), dict) else None):
                if r:
                    out[iid].extend(str(r.get(k) or "") for k in ("label", "note"))
    return out


def fund_citations(arc: Archive) -> dict[tuple[str, str], set[str]]:
    """(instrument id, filing URL) pairs the archive cites as the source of a fund or BDC mark."""
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for iid, row in arc.rows.items():
        for url in row.get("src") or []:
            if url:
                out[(iid, url)].add("aidebt.src")
    for iid, rec in arc.pxsrc.items():
        if rec.get("kind") == "fund mark" and rec.get("url"):
            out[(iid, rec["url"])].add("aidebt.pxsrc")
    for name, records in (("aiwcredit.pxs", arc.pxs), ("out1", arc.out1), ("out2", arc.out2)):
        for iid, rec in records.items():
            if iid.startswith("case:"):
                continue
            for r in (rec, rec.get("fund") if isinstance(rec.get("fund"), dict) else None):
                if r and r.get("kind") in ("fund mark", "BDC mark") and r.get("url"):
                    out[(iid, r["url"])].add(name)
    return {k: v for k, v in out.items() if k[0] in arc.rows}


_CIK = re.compile(r"/edgar/data/0*(\d+)/")


def cites_fund_filing(row: dict[str, Any], pxsrc: dict[str, Any] | None, bdc_ciks: set[str]) -> bool:
    """Does the legacy px claim to come from a fund filing (N-PORT, N-CSR or a BDC 10-Q)?"""
    urls = [u for u in row.get("src") or [] if u] + ([pxsrc["url"]] if pxsrc and pxsrc.get("url") else [])
    if pxsrc and pxsrc.get("kind") == "fund mark":
        return True
    for u in urls:
        m = _CIK.search(u)
        if u.endswith("primary_doc.xml") or "forms=NPORT" in u or "ncsr" in u.lower() or (m and m[1] in bdc_ciks):
            return True
    return False
