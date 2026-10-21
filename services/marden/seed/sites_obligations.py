"""Write the `sites` loader's obligation rows and obligation facts (field table: seed/sites.py)."""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn
from marden.seed.sites_archive import Archive
from marden.seed.sites_edges import Edge, Record, by_source
from marden.seed.sites_facts import FactSink, weakest

_LIST_FIELDS = ("conditions", "contracts")


def _upsert(conn: Conn, edge: Edge, slugs: dict[str, str], instruments: list[str]) -> None:
    sites = sorted({slugs[c] for c in edge.cases() if c in slugs})
    conn.execute(
        """INSERT INTO obligation (id, type, from_entity_id, to_entity_id, site_slug, campus_id, instrument_ids)
           VALUES (%s, %s, %s, %s, %s, NULL, %s)
           ON CONFLICT (id) DO UPDATE SET type = EXCLUDED.type, from_entity_id = EXCLUDED.from_entity_id,
             to_entity_id = EXCLUDED.to_entity_id, site_slug = EXCLUDED.site_slug,
             instrument_ids = EXCLUDED.instrument_ids""",
        (edge.id, edge.type, edge.from_id, edge.to_id, sites[0] if len(sites) == 1 else None, instruments),
    )


def _dedupe(values: list[Any]) -> list[Any]:
    out: list[Any] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _edge_sites(edge: Edge, slugs: dict[str, str]) -> list[str]:
    """Every site any of the edge's records names (the obligation row's own site set). Each source file's
    `linked_sites` fact carries the whole list: fact_current keeps one fact per field, so a per-file partial
    list (CIFR_BLACKPEARL.json and CIFR_STINGRAY.json each name one site of the same guarantee) would hide
    the other sites."""
    return sorted({slugs[c] for c in edge.cases() if c in slugs})


def _write_group(sink: FactSink, edge: Edge, records: list[Record], slugs: dict[str, str]) -> None:
    """Facts one source file contributes to one edge."""
    first, tag = records[0], weakest(r.tag for r in records)
    src = max((r.src for r in records), key=lambda s: s.as_of)
    url = first.url
    sink.add("obligation", edge.id, "edge_type", edge.type, src=src, url=url, tag=tag)
    sink.add("obligation", edge.id, "linked_sites", _edge_sites(edge, slugs), src=src, url=url, tag=tag)
    if records[0].kind != "rowinfo":
        sink.add("obligation", edge.id, "conditions", _dedupe([c for r in records for c in r.conditions]),
                 src=src, url=url, tag=tag)
        sink.add("obligation", edge.id, "contracts", [r.detail for r in records], src=src, url=url, tag=tag)
    payer_ids = sorted({i for r in records for i in r.payer_instruments})
    sink.add("obligation", edge.id, "payer_instruments", payer_ids, src=src, url=url, tag="M")
    for name in sorted({k for r in records for k in r.scalars}):
        holders = [r for r in records if name in r.scalars]
        if len(holders) != 1:
            continue  # several contracts merged into this edge: the per-contract values are in `contracts`
        rec = holders[0]
        value, unit = rec.scalars[name]
        sink.add("obligation", edge.id, name, value, src=rec.src, url=rec.url, quote=rec.quote, unit=unit,
                 tag="M" if name == "status" else (rec.tag if rec.tag == "M" else None), rule=name == "status")
    for rec in records:
        if rec.evidence:
            value = {"date": rec.src.as_of.isoformat(), "text": rec.evidence, "url": rec.url}
            sink.add("obligation", edge.id, "evidence", {k: v for k, v in value.items() if v}, src=rec.src,
                     url=rec.url, tag=rec.tag, quote=rec.quote)


def write_edges(conn: Conn, sink: FactSink, arc: Archive, edges: dict[str, Edge], slugs: dict[str, str]) -> None:
    rowinfo_src = arc.src("aiwcredit.json")
    for edge in sorted(edges.values(), key=lambda e: e.id):
        instruments = sorted({i for c in edge.cases() for i in arc.case_debt.get(c, [])})
        _upsert(conn, edge, slugs, instruments)
        for _, records in by_source(edge):
            _write_group(sink, edge, records, slugs)
        sink.add("obligation", edge.id, "linked_instruments", instruments, src=rowinfo_src, tag="M")
