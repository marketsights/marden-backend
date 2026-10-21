"""The four HIFLD grid layers -> `grid_region` rows (used by `hifld_grid`).

| Layer (HIFLD Next slug)              | Rows                                    | Key                                  |
| ------------------------------------ | --------------------------------------- | ------------------------------------ |
| nerc-regions                         | 'nerc:<entity>-<subregion>' + the three US interconnection boundaries (union) | NAME, SUBNAME |
| independent-systems-operators        | 'iso:<short>' boundaries (the seven US ISO/RTOs) | ID = EIA id (grid_registry.US_ISOS) |
| control-areas                        | 'ba:<EIA code>' boundaries              | source_ID = EIA-861 BA ID, else name |
| electric-retail-service-territories  | 'utility:<EIA utility number>'          | source_ID (ID) = EIA utility number  |

Interconnections are the union of NERC subregions: WECC subregions form the Western Interconnection, the Texas
RE (ERCOT) subregion the ERCOT Interconnection and every other subregion the Eastern Interconnection (the NERC
regional entities are defined on those interconnections). The union covers the US portion only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from marden.collectors import grid_registry
from marden.collectors.gpkg import Feature
from marden.collectors.grid_store import Region, UpsertStats, name_key, rows_of_kind, slug, upsert
from marden.core.db import Conn
from marden.core.jsonutil import jsonb

MISSING = {"", "NOT AVAILABLE", "-999999", "-999999.0", "None"}


@dataclass(frozen=True)
class LayerContext:
    """Where a layer's features came from; copied into every row's meta.hifld."""

    layer: str
    version: str
    document_id: int
    source_url: str  # the GeoPackage URL
    as_of: str  # the HIFLD Open 'modified' date of the dataset
    issued: str | None
    agency: str | None
    office: str | None
    srid: int

    def meta(self, **extra: Any) -> dict[str, Any]:
        base = {"layer": self.layer, "version": self.version, "document_id": self.document_id,
                "source_url": self.source_url, "as_of": self.as_of, "issued": self.issued, "agency": self.agency,
                "office": self.office}
        return {**base, **{k: v for k, v in extra.items() if v is not None}}


def text(props: dict[str, Any], *names: str) -> str | None:
    """The first non-missing attribute among `names` (HIFLD writes 'NOT AVAILABLE' and -999999 for blanks)."""
    for name in names:
        value = props.get(name)
        if value is None:
            continue
        s = str(value).strip()
        if s and s not in MISSING:
            return s
    return None


def feature_id(props: dict[str, Any]) -> str | None:
    """HIFLD's ID attribute (renamed source_ID in the GeoPackages that also carry an `fid`)."""
    value = text(props, "source_ID", "ID")
    return str(int(float(value))) if value and re.fullmatch(r"\d+(\.0+)?", value) else value


def _country(props: dict[str, Any]) -> str | None:
    return {"USA": "US", "CAN": "CA", "MEX": "MX"}.get(text(props, "COUNTRY") or "")


# ---------------------------------------------------------------- NERC regions -> interconnections
def nerc_entity(name: str) -> str:
    m = re.search(r"\(([^)]+)\)\s*$", name)
    return m.group(1).strip() if m else name


def nerc_interconnection(entity: str) -> str:
    if entity == "WECC":
        return grid_registry.WESTERN
    if entity == "TRE":
        return grid_registry.ERCOT
    return grid_registry.EASTERN


def nerc_region(props: dict[str, Any], ctx: LayerContext) -> Region:
    name = text(props, "NAME") or "NERC region"
    entity = nerc_entity(name)
    sub = text(props, "SUBNAME") or entity
    return Region(f"nerc:{slug(entity)}-{slug(sub)}", "reliability_region", f"{name} – {sub}", "US",
                  nerc_interconnection(entity), {"hifld": ctx.meta(
                      feature_id=feature_id(props), name=name, subregion=sub, entity=entity,
                      sourcedate=text(props, "SOURCEDATE"), val_date=text(props, "VAL_DATE"))})


_UNION = """
UPDATE grid_region i SET geom = u.geom, meta = i.meta || jsonb_build_object('boundary', %(meta)s::jsonb || jsonb_build_object('subregions', u.ids)),
       updated_at = now()
FROM (SELECT parent_id, array_agg(id ORDER BY id) AS ids,
             ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_UnaryUnion(ST_Collect(geom))), 3)) AS geom
      FROM grid_region
      WHERE kind = 'reliability_region' AND meta -> 'hifld' ->> 'layer' = 'nerc-regions'
        AND meta -> 'hifld' ->> 'document_id' = %(doc)s AND geom IS NOT NULL
      GROUP BY parent_id) u
WHERE i.id = u.parent_id AND i.id = ANY(%(ids)s)
RETURNING i.id
"""


def union_interconnections(conn: Conn, ctx: LayerContext) -> list[str]:
    meta = ctx.meta(method="union of HIFLD NERC Regions subregions: WECC -> Western, Texas RE -> ERCOT, all "
                           "others -> Eastern; US portion only")
    rows = conn.execute(_UNION, {"meta": jsonb(meta), "doc": str(ctx.document_id),
                                 "ids": list(grid_registry.US_INTERCONNECTIONS)}).fetchall()
    return sorted(r["id"] for r in rows)


# ---------------------------------------------------------------- ISO/RTOs
ISO_BY_EIA_ID = {eia_id: short for short, (_, eia_id, _, _, _) in grid_registry.US_ISOS.items()}


def iso_region(props: dict[str, Any], ctx: LayerContext) -> Region:
    fid = feature_id(props) or ""
    name = text(props, "NAME") or fid
    short = ISO_BY_EIA_ID.get(fid)
    rid = f"iso:{short}" if short else f"iso:eia{fid}"
    parent = grid_registry.US_ISOS[short][3] if short else None
    return Region(rid, "iso_rto", name, "US", parent, {"hifld": ctx.meta(
        feature_id=fid, name=name, peak_load_mw=text(props, "PEAK_LOAD"), year=text(props, "YEAR"),
        sourcedate=text(props, "SOURCEDATE"), val_date=text(props, "VAL_DATE"))})


# ---------------------------------------------------------------- control areas (balancing authorities)
def _strip_code_suffix(name: str) -> str:
    """'ARLINGTON VALLEY, LLC - AVBA' -> 'ARLINGTON VALLEY, LLC'; 'NORTHWESTERN ENERGY (NWMT)' -> 'NORTHWESTERN ENERGY'."""
    return re.sub(r"(\s+-\s+[A-Z0-9]{2,6}|\s*\([A-Z0-9]{2,6}\))\s*$", "", name)


def _keys(name: str | None) -> list[str]:
    if not name:
        return []
    out = []
    for n in (name, _strip_code_suffix(name)):
        k = name_key(n)
        if k:
            out += [k, " ".join(sorted(k.split()))]
    return out


@dataclass
class BaIndex:
    """EIA-861 BA ids and every known spelling of each balancing authority -> its row id."""

    by_eia_id: dict[str, str]
    by_name: dict[str, str]

    @classmethod
    def load(cls, conn: Conn) -> BaIndex:
        by_id: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for r in rows_of_kind(conn, "balancing_authority"):
            meta = r["meta"] or {}
            for ba_id in (meta.get("eia861") or {}).get("ba_ids", []):
                by_id.setdefault(str(ba_id), r["id"])
            names = [r["name"], (meta.get("eia930") or {}).get("name"), *(meta.get("eia861") or {}).get("names", [])]
            for n in names:
                for k in _keys(n):
                    by_name.setdefault(k, r["id"])
        return cls(by_id, by_name)

    def match(self, eia_id: str | None, name: str | None) -> tuple[str | None, str]:
        """(row id, how it matched) or (None, 'unmatched')."""
        if eia_id and eia_id in self.by_eia_id:
            return self.by_eia_id[eia_id], "eia861_ba_id"
        for k in _keys(name):
            if k in self.by_name:
                return self.by_name[k], "name"
        return None, "unmatched"


def control_area_region(props: dict[str, Any], ctx: LayerContext, index: BaIndex) -> tuple[Region, str]:
    fid = feature_id(props)
    name = text(props, "NAME") or f"EIA {fid}"
    rid, how = index.match(fid, name)
    region = Region(rid or f"ba:eia{fid}", "balancing_authority", name, _country(props), None, {"hifld": ctx.meta(
        feature_id=fid, name=name, match=how, state=text(props, "STATE"), peak_load_mw=text(props, "PEAK_LOAD"),
        year=text(props, "YEAR"), sourcedate=text(props, "SOURCEDATE"), val_date=text(props, "VAL_DATE"))})
    return region, how


# ---------------------------------------------------------------- retail service territories (utilities)
def control_area_codes(props: dict[str, Any]) -> list[str]:
    raw = text(props, "CNTRL_AREA")
    return [c.strip() for c in raw.split(",") if c.strip()] if raw else []


def utility_region(props: dict[str, Any], ctx: LayerContext, ba_ids: set[str]) -> Region:
    fid = feature_id(props) or ""
    name = text(props, "NAME") or f"EIA utility {fid}"
    codes = control_area_codes(props)
    parent = f"ba:{codes[0]}" if len(codes) == 1 and f"ba:{codes[0]}" in ba_ids else None
    return Region(f"utility:{fid}", "utility", name, _country(props) or "US", parent, {"hifld": ctx.meta(
        feature_id=fid, name=name, type=text(props, "TYPE"), state=text(props, "STATE"),
        holding_co=text(props, "HOLDING_CO"), control_areas=codes or None, planning_area=text(props, "PLAN_AREA"),
        regulated=text(props, "REGULATED"), year=text(props, "YEAR"), sourcedate=text(props, "SOURCEDATE"),
        val_date=text(props, "VAL_DATE"))})


# ---------------------------------------------------------------- loading
def load_layer(conn: Conn, ctx: LayerContext, features: list[Feature]) -> dict[str, Any]:
    """Upsert every feature of one layer. Returns counts (and the BA match report for control areas)."""
    stats = UpsertStats()
    report: dict[str, Any] = {}
    build: Callable[[dict[str, Any]], Region]
    if ctx.layer == "control-areas":
        index = BaIndex.load(conn)
        matches: dict[str, int] = {}
        unmatched: list[str] = []

        def build(props: dict[str, Any]) -> Region:
            region, how = control_area_region(props, ctx, index)
            matches[how] = matches.get(how, 0) + 1
            if how == "unmatched":
                unmatched.append(f"{region.meta['hifld'].get('feature_id')} {region.name}")
            return region

        report = {"match": matches, "unmatched": unmatched}
        prefer_existing = True
    elif ctx.layer == "electric-retail-service-territories":
        ba_ids = {r["id"] for r in rows_of_kind(conn, "balancing_authority")}

        def build(props: dict[str, Any]) -> Region:
            return utility_region(props, ctx, ba_ids)

        prefer_existing = False
    elif ctx.layer == "independent-systems-operators":
        def build(props: dict[str, Any]) -> Region:
            return iso_region(props, ctx)

        prefer_existing = True  # the registry names the seven ISO/RTOs
    elif ctx.layer == "nerc-regions":
        def build(props: dict[str, Any]) -> Region:
            return nerc_region(props, ctx)

        prefer_existing = False
    else:
        raise ValueError(f"unknown HIFLD layer {ctx.layer!r}")
    no_geometry = 0
    for f in features:
        if f.wkb is None:
            no_geometry += 1
            continue
        stats.add(upsert(conn, build(f.properties), wkb=f.wkb, srid=ctx.srid, prefer_existing=prefer_existing))
    out: dict[str, Any] = {"features": len(features), "no_geometry": no_geometry, **stats.as_dict(), **report}
    if ctx.layer == "nerc-regions":
        out["interconnections"] = union_interconnections(conn, ctx)
    return out


def layer_loaded(conn: Conn, layer: str, document_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM grid_region WHERE meta -> 'hifld' ->> 'layer' = %s AND meta -> 'hifld' ->> 'document_id' = %s
           LIMIT 1""",
        (layer, str(document_id)),
    ).fetchone()
    return row is not None
