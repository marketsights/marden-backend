"""HIFLD grid boundaries (via HIFLD Next) -> `grid_region` boundaries, then the spatial join (`grid_join`).

Layers (DOE/ORNL and DHS publications on the former HIFLD Open portal): Control Areas (balancing authority
areas), Independent System Operators, NERC Regions and Electric Retail Service Territories; see hifld_layers.

Where the files come from: DHS decommissioned HIFLD Open (FGDC: "The HIFLD Open part of the program has been
decommissioned", portal offline since 2025-08-26), the old services return "Invalid URL", and the EIA Energy
Atlas "Balancing Authorities" layer now answers "Token Required". The final public release of each layer is
preserved, unchanged and versioned, by HIFLD Next (Public Environmental Data Partners,
https://hifld.publicenvirodata.org), which records the original producer and HIFLD Open issue/modified dates.
Marden reads the GeoPackage of the latest version of each layer: dataset catalog -> layer catalog ->
'latest-version' collection.json -> geopackage asset. All metadata and files are stored raw first.

Facts written from these boundaries (by grid_join) are tag F, source_name 'HIFLD', source_url = the
GeoPackage URL, valid_from = the layer's HIFLD Open 'modified' date. TERMS: see TERMS_NOTE. Cadence: monthly
check for a new version (unchanged files answer 304 and are not re-parsed). `python -m marden collect hifld_grid`.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from typing import Any

from marden import cli
from marden.collectors import gpkg, grid_registry
from marden.collectors.grid_store import counts
from marden.collectors.hifld_layers import LayerContext, layer_loaded, load_layer
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats
from marden.core import db, http
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source

log = logging.getLogger(__name__)

SOURCE_ID = "hifld_next"
API = "https://hifld.publicenvirodata.org"
STORAGE_HOST = "storage.googleapis.com"
CRON_UTC = "29 11 2 * *"  # 2nd of each month, 11:29 UTC (after eia930_ref and eia861_ba)
# Load order: interconnections before ISOs, balancing authorities before utilities (their parents).
LAYERS = ("nerc-regions", "independent-systems-operators", "control-areas", "electric-retail-service-territories")
TERMS_NOTE = (
    "Checked 2026-10-21. Original HIFLD Open metadata (e.g. Control Areas, ISO 19139 'Limitations of use'): \"None "
    "(Public Use). Users are advised to read the data set's metadata thoroughly to understand appropriate use and "
    "data limitations.\" Producer: Oak Ridge National Laboratory / DOE (Control Areas, Retail Service Territories, "
    "ISOs), DHS HSIP (NERC Regions). HIFLD Open is gone: FGDC (https://www.fgdc.gov/organization/working-groups-"
    "subcommittees/hifld/index_html): \"The HIFLD Open part of the program has been decommissioned.\" Host used: "
    "HIFLD Next (https://hifld.publicenvirodata.org/about): \"HIFLD Next sources its archive from the Data Rescue "
    "Project, which captured a full snapshot of 400-plus HIFLD Open data layers and their associated metadata before "
    "the portal was taken offline\"; robots.txt: \"User-agent: * Allow: /\" and \"Content-Signal: ai-train=yes, "
    "search=yes, ai-input=yes\"; no terms-of-use page (404); STAC license field \"other\". llms.txt asks clients to "
    "use the JSON API and catalog links rather than scrape HTML, which is what this collector does. Polite use: "
    "about 12 requests per run (3 small JSON + 1 GeoPackage per layer), 1 req/s, conditional GETs."
)
SOURCE = declare_source(SOURCE_ID, "HIFLD grid layers (HIFLD Next archive of HIFLD Open)", "feed", API,
                        30 * 24 * 60, TERMS_NOTE)
http.set_rate_limit("hifld.publicenvirodata.org", 1.0)


@dataclass(frozen=True)
class LayerVersion:
    layer: str
    version: str
    collection_url: str
    gpkg_url: str
    modified: str
    issued: str | None
    agency: str | None
    office: str | None
    feature_count: int | None


def _json(doc: DocumentRow) -> dict[str, Any]:
    try:
        payload = json.loads(doc.read())
    except ValueError as exc:
        raise ParseError(f"{doc.url} is not JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ParseError(f"{doc.url}: expected a JSON object")
    return payload


def link(payload: dict[str, Any], rel: str, url: str) -> str:
    for item in payload.get("links") or []:
        if item.get("rel") == rel and item.get("href"):
            return str(item["href"])
    raise ParseError(f"{url}: no '{rel}' link")


def parse_collection(payload: dict[str, Any], layer: str, url: str) -> LayerVersion:
    """A HIFLD Next STAC collection.json -> the layer version and its GeoPackage asset."""
    assets = payload.get("assets") or {}
    gpkg_href = next((a.get("href") for k, a in sorted(assets.items()) if k.startswith("geopackage")), None)
    if not gpkg_href:
        raise ParseError(f"{url}: no geopackage asset (assets: {sorted(assets)})")
    dates = payload.get("hifld:source_dates") or {}
    modified = dates.get("modified") or dates.get("issued")
    if not modified:
        raise ParseError(f"{url}: no hifld:source_dates.modified")
    version = str(payload.get("version") or payload.get("id", "").rsplit("/", 1)[-1])
    return LayerVersion(layer, version, url, str(gpkg_href), str(modified)[:10],
                        str(dates["issued"])[:10] if dates.get("issued") else None, payload.get("hifld:agency"),
                        payload.get("hifld:office"), payload.get("hifld:feature_count"))


def discover(source_id: str, layer: str, fetcher: Fetcher | None, stats: RunStats) -> LayerVersion:
    url = f"{API}/api/collections/hifld/datasets/{layer}"
    doc = fetch_document(source_id, url, doc_type="stac_catalog", title=f"HIFLD Next dataset {layer}",
                         external_id=layer, fetcher=fetcher)
    stats.document(doc)
    child_url = link(_json(doc), "child", url)
    child = fetch_document(source_id, child_url, doc_type="stac_catalog", title=f"HIFLD Next layer {layer}",
                           external_id=layer, fetcher=fetcher)
    stats.document(child)
    collection_url = link(_json(child), "latest-version", child_url)
    collection = fetch_document(source_id, collection_url, doc_type="stac_collection",
                                title=f"HIFLD Next {layer} collection", external_id=layer, fetcher=fetcher)
    stats.document(collection)
    return parse_collection(_json(collection), layer, collection_url)


def load_gpkg(doc: DocumentRow, version: LayerVersion, *, force: bool = False) -> dict[str, Any]:
    with db.transaction() as conn:
        if not force and layer_loaded(conn, version.layer, doc.id):
            return {"skipped": "already loaded from this document", "document_id": doc.id}
    try:
        tables, features = gpkg.read(doc.read())
        srid = gpkg.epsg_for(tables, tables[0].name)
    except gpkg.GpkgError as exc:
        raise ParseError(f"{version.layer} GeoPackage {doc.url}: {exc}") from None
    if version.feature_count is not None and len(features) != version.feature_count:
        raise ParseError(f"{version.layer}: {len(features)} features read, catalog says {version.feature_count}")
    ctx = LayerContext(version.layer, version.version, doc.id, doc.url, version.modified, version.issued,
                       version.agency, version.office, srid)
    with db.transaction() as conn:
        grid_registry.ensure_registry(conn)
        out = load_layer(conn, ctx, features)
    return {**out, "document_id": doc.id, "version": version.version, "as_of": version.modified}


def run(
    *, layers: tuple[str, ...] = LAYERS, source: SourceSpec = SOURCE, fetcher: Fetcher | None = None,
    force: bool = False, join: bool = True,
) -> dict[str, Any]:
    changed: list[str] = []

    def body(stats: RunStats) -> None:
        per_layer: dict[str, Any] = {}
        stats.extra["layers"] = per_layer
        for layer in layers:
            version = discover(source.id, layer, fetcher, stats)
            doc = fetch_document(source.id, version.gpkg_url, doc_type="geopackage", title=f"HIFLD {layer}",
                                 doc_date=version.modified, external_id=f"{layer}@{version.version}",
                                 meta={"collection_url": version.collection_url, "agency": version.agency,
                                       "office": version.office}, fetcher=fetcher)
            stats.document(doc)
            result = load_gpkg(doc, version, force=force)
            per_layer[layer] = result
            if "skipped" not in result:
                changed.append(layer)
                stats.facts_seen += result["features"]
                stats.facts_new += result["inserted"] + result["changed"]
        with db.transaction() as conn:
            stats.extra["grid_region"] = counts(conn)

    out = collect(source, body)
    if join and out.get("ok"):
        from marden.collectors import grid_join

        out["join"] = grid_join.run()
    out["layers_reloaded"] = changed
    return out


@app.periodic(cron=CRON_UTC, periodic_id="monthly")
@app.task(name="collect.hifld_grid", queue="collect", queueing_lock="collect.hifld_grid", lock="collect.grid")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--layer", action="append", choices=LAYERS, help="only this layer (repeatable)")
    p.add_argument("--force", action="store_true", help="re-parse files that were already loaded")
    p.add_argument("--no-join", action="store_true", help="skip the spatial join afterwards")


@cli.action("collect", "hifld_grid", help="HIFLD ISO, NERC, control-area and utility boundaries", configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    layers = tuple(args.layer) if args.layer else LAYERS
    return print_stats(run(layers=layers, force=args.force, join=not args.no_join))
