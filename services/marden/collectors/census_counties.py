"""US county boundaries (Census Bureau cartographic boundary files) -> `us_county`, then the spatial join.

Why: grid_join places campuses, sites and plants by their point. Some have a county and state but no point
yet (a financed site known only as "Wink, Winkler County, TX"; an on-site plant from a permit). For those,
grid_join intersects the county polygon with the grid boundaries and publishes a region only when the
whole county lies in it (see grid_join "County basis").

Source: the national GeoPackage of the latest cartographic boundary vintage, 1:5,000,000
(https://www2.census.gov/geo/tiger/GENZ<year>/gpkg/cb_<year>_us_all_5m.zip; one file with counties, states
and other layers; only the county layer is read). Discovery: the /geo/tiger/ listing -> newest GENZ<year>
directory whose gpkg/ listing has the file. Raw first: both listings and the zip are stored before parsing;
an unchanged zip answers 304 (conditional GET) and is not re-parsed.

Cadence: Census publishes one vintage a year (spring); checked monthly. `python -m marden collect
census_counties`.
"""

from __future__ import annotations

import argparse
import io
import re
import unicodedata
import zipfile
from typing import Any

from marden import cli
from marden.collectors import gpkg
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats
from marden.core import db, http
from marden.core.db import Conn
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, fetch_document
from marden.core.jsonutil import jsonb
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source

SOURCE_ID = "census_counties"
HOST = "www2.census.gov"
INDEX_URL = f"https://{HOST}/geo/tiger/"
CRON_UTC = "41 11 2 * *"  # 2nd of each month, 11:41 UTC (after hifld_grid; same grid lock)
SCALE = "5m"
MIN_COUNTIES = 3000  # 3,235 in the 2025 vintage (50 states, DC, Puerto Rico); fewer = a truncated file
TERMS_NOTE = (
    "Checked 2026-10-21. US Census Bureau, a federal agency: its products are US government works "
    "(17 U.S.C. 105, no copyright). The file's own ISO 19115 metadata (cb_2024_us_county_20m.shp.iso.xml, "
    "resourceConstraints): \"Access constraints: None\"; use limitation: \"The cartographic boundary files "
    "contain geographic data only and do not include display mapping software or statistical data.\" "
    "robots.txt (https://www2.census.gov/robots.txt) disallows / only for named crawlers (RavenCrawler, "
    "MegaIndex, BLP_bbot, TweetmemeBot) and asks Googlebot, bingbot, Applebot and Slurp for Crawl-delay: 30; "
    "nothing for other agents. Polite use: 2 small listings and 1 conditional GET of one ~30 MB file a month, "
    "1 req/s."
)
SOURCE = declare_source(SOURCE_ID, "US Census Bureau cartographic boundary files (counties)", "feed",
                        INDEX_URL, 30 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 1.0)

_YEAR_DIR = re.compile(r'href="GENZ(\d{4})/"')


def zip_name(year: int) -> str:
    return f"cb_{year}_us_all_{SCALE}.zip"


def vintages(index_html: bytes) -> list[int]:
    """GENZ<year> directories on the /geo/tiger/ listing, newest first."""
    years = sorted({int(y) for y in _YEAR_DIR.findall(index_html.decode("utf-8", "replace"))}, reverse=True)
    if not years:
        raise ParseError(f"no GENZ<year>/ directories on {INDEX_URL}")
    return years


def has_file(listing_html: bytes, year: int) -> bool:
    return f'href="{zip_name(year)}"' in listing_html.decode("utf-8", "replace")


def county_table(year: int) -> str:
    return f"cb_{year}_us_county_{SCALE}"


def read_counties(zip_bytes: bytes, year: int) -> tuple[list[gpkg.Feature], int]:
    """The county layer's features and its EPSG code, from the zipped national GeoPackage."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if n.endswith(".gpkg")]
            if len(names) != 1:
                raise ParseError(f"{zip_name(year)}: expected one .gpkg, found {names}")
            data = zf.read(names[0])
    except zipfile.BadZipFile as exc:
        raise ParseError(f"{zip_name(year)} is not a zip file: {exc}") from None
    try:
        con = gpkg.open_bytes(data)
        try:
            tables = gpkg.feature_tables(con)
            table = next((t for t in tables if t.name == county_table(year)), None)
            if table is None:
                raise ParseError(f"no {county_table(year)} layer; layers: {[t.name for t in tables]}")
            features = list(gpkg.iter_features(con, table))
        finally:
            con.close()
        return features, gpkg.epsg_for(tables, table.name)
    except gpkg.GpkgError as exc:
        raise ParseError(f"{zip_name(year)}: {exc}") from None


_UPSERT = """
INSERT INTO us_county AS c (geoid, statefp, countyfp, stusps, name, namelsad, geom, meta)
VALUES (%(geoid)s, %(statefp)s, %(countyfp)s, %(stusps)s, %(name)s, %(namelsad)s,
        ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Force2D(ST_Transform(
          ST_SetSRID(ST_GeomFromWKB(%(wkb)s), %(srid)s), 4326))), 3)), %(meta)s)
ON CONFLICT (geoid) DO UPDATE SET
  statefp = EXCLUDED.statefp, countyfp = EXCLUDED.countyfp, stusps = EXCLUDED.stusps, name = EXCLUDED.name,
  namelsad = EXCLUDED.namelsad, geom = EXCLUDED.geom, meta = EXCLUDED.meta, updated_at = now()
WHERE (c.stusps, c.name, c.namelsad, c.meta) IS DISTINCT FROM (EXCLUDED.stusps, EXCLUDED.name, EXCLUDED.namelsad,
                                                                 EXCLUDED.meta)
   OR NOT ST_Equals(c.geom, EXCLUDED.geom)
RETURNING (xmax = 0) AS inserted
"""


def load(conn: Conn, features: list[gpkg.Feature], srid: int, doc: DocumentRow, year: int, *,
         min_counties: int = MIN_COUNTIES) -> dict[str, Any]:
    """Upsert every county of one vintage; counties the vintage no longer has are removed."""
    if len(features) < min_counties:
        raise ParseError(f"{zip_name(year)}: only {len(features)} counties (expected over {min_counties})")
    meta = {"source_url": doc.url, "document_id": doc.id, "vintage": year, "scale": f"1:{SCALE.upper()}",
            "layer": county_table(year)}
    counts = {"inserted": 0, "changed": 0, "unchanged": 0, "no_geometry": 0}
    geoids = []
    for f in features:
        p = f.properties
        if f.wkb is None:
            counts["no_geometry"] += 1
            continue
        row = conn.execute(_UPSERT, {
            "geoid": p["geoid"], "statefp": p["statefp"], "countyfp": p["countyfp"], "stusps": p["stusps"],
            "name": p["name"], "namelsad": p["namelsad"], "wkb": f.wkb, "srid": srid, "meta": jsonb(meta),
        }).fetchone()
        geoids.append(p["geoid"])
        counts["unchanged" if row is None else "inserted" if row["inserted"] else "changed"] += 1
    gone = conn.execute("DELETE FROM us_county WHERE NOT (geoid = ANY(%s)) RETURNING geoid", (geoids,)).fetchall()
    return {"counties": len(features), **counts, "removed": sorted(r["geoid"] for r in gone)}


def loaded_from(conn: Conn, document_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM us_county WHERE meta ->> 'document_id' = %s LIMIT 1",
                       (str(document_id),)).fetchone()
    return row is not None


def run(*, source: SourceSpec = SOURCE, fetcher: Fetcher | None = None, force: bool = False,
        join: bool = True, min_counties: int = MIN_COUNTIES) -> dict[str, Any]:
    reloaded: list[bool] = []

    def body(stats: RunStats) -> None:
        index = fetch_document(source.id, INDEX_URL, doc_type="index_html", title="Census TIGER directory",
                               fetcher=fetcher)
        stats.document(index)
        for year in vintages(index.read())[:3]:
            url = f"{INDEX_URL}GENZ{year}/gpkg/"
            listing = fetch_document(source.id, url, doc_type="index_html", title=f"GENZ{year} gpkg",
                                     external_id=str(year), fetcher=fetcher)
            stats.document(listing)
            if has_file(listing.read(), year):
                break
        else:
            raise ParseError(f"no {SCALE} national GeoPackage in the three newest GENZ directories")
        doc = fetch_document(source.id, url + zip_name(year), doc_type="geopackage_zip",
                             title=f"Census cartographic boundaries {year} 1:{SCALE.upper()}",
                             external_id=f"{year}-{SCALE}", fetcher=fetcher)
        stats.document(doc)
        with db.transaction() as conn:
            if not force and loaded_from(conn, doc.id):
                stats.extra["counties"] = {"skipped": "already loaded from this document", "document_id": doc.id}
                return
        features, srid = read_counties(doc.read(), year)
        with db.transaction() as conn:
            out = load(conn, features, srid, doc, year, min_counties=min_counties)
        stats.extra["counties"] = {**out, "vintage": year, "document_id": doc.id}
        stats.facts_seen += out["counties"]
        stats.facts_new += out["inserted"] + out["changed"]
        reloaded.append(True)

    out = collect(source, body)
    if join and out.get("ok") and reloaded:
        from marden.collectors import grid_join

        joined = grid_join.run()
        joined.pop("assignments", None)
        out["join"] = joined
    return out


def county_key(text: str | None) -> str:
    """'Doña Ana County' -> 'dona ana'; 'Richland Parish' -> 'richland'; '' for blanks."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = [w for w in re.findall(r"[a-z0-9]+", s) if w not in {"county", "parish", "borough"}]
    return " ".join(words)


@app.periodic(cron=CRON_UTC, periodic_id="monthly")
@app.task(name="collect.census_counties", queue="collect", queueing_lock="collect.census_counties",
          lock="collect.grid")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--force", action="store_true", help="re-parse a file that was already loaded")
    p.add_argument("--no-join", action="store_true", help="skip the spatial join afterwards")


@cli.action("collect", "census_counties", help="US county boundaries (Census cartographic boundary files)",
            configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(force=args.force, join=not args.no_join))
