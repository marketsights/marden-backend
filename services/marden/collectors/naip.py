"""USDA NAIP aerial imagery (0.6 m, public domain) from Microsoft Planetary Computer: the sharp base image
for every US site and campus (spec 22 "Construction progress"; the satellite panels draw building outlines on
it). Sentinel-2 (sentinel2.py) stays the recent-change series; NAIP is flown about every two years per state,
so it is sharp but older, and every use shows its acquisition date.

For each site and campus with a point:
1. The box: the newest Sentinel-2 image box of the subject (so the two line up for comparison), else the
   ~1.5 km box around its point (sentinel2_stac.Box.around).
2. STAC search (collection naip, the box). The newest acquisition whose items' footprints cover the box;
   one item when one covers it all, else that year's items that touch it (each rendered over its part of
   the box). Every item used is stored raw (fetch_document of its item JSON).
3. The image URL is the PC data API's rendering of the item's RGB bands cropped to the box (or to the item's
   part of it), at about 1.2 m per pixel (at most MAX_PX wide), as WebP; no token.

Fact (published; subject site or campus; field `imagery_naip`; tag R; valid_from = the acquisition day; a new
rendering supersedes the subject's earlier one):
  {date, gsd, url, bbox, tiles: [{item, url, bbox}], caption}
caption: "USDA NAIP aerial photo, 26 Jul 2022: 0.6 m. Public domain (USDA FSA)". Nothing is filled in: a
subject with no NAIP coverage (outside the US) has no fact. `python -m marden collect naip [--site SLUG ...]`.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode

from marden import cli
from marden.collectors.rates_common import RunStats, collect, print_stats
from marden.collectors.sentinel2_stac import DATA_ROOT, METERS_PER_DEG_LAT, STAC_ROOT, Box
from marden.core import db, http
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.http import Fetcher, default_fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source

log = logging.getLogger(__name__)

SOURCE_ID = "naip_pc"
SOURCE_NAME = "USDA NAIP via Microsoft Planetary Computer"
COLLECTION = "naip"
FIELD = "imagery_naip"
PRODUCER = "collector:naip"
PC_HOST = "planetarycomputer.microsoft.com"
M_PER_PX = 1.2
MAX_PX = 1400
TERMS_NOTE = (
    "Checked 2026-10-22. NAIP is USDA Farm Service Agency imagery in the public domain (USDA FSA APFO: "
    "'NAIP imagery is in the public domain'; the Planetary Computer catalog lists the naip collection's "
    "licence as 'proprietary' with the USDA public-domain statement linked). Served through the Planetary "
    "Computer STAC and data APIs, which need no token (same terms as sentinel2_pc: the documented public API, "
    "rate-limited, declared User-Agent). Attribution shown with every image: 'USDA NAIP'."
)
SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "api", f"{STAC_ROOT}/collections/{COLLECTION}", 30 * 24 * 60,
                        TERMS_NOTE)
http.set_rate_limit(PC_HOST, 2.0)

_SUBJECTS = """
SELECT 'site' AS subject_type, slug AS subject_id, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM site
 WHERE geom IS NOT NULL AND (%(sites)s::text[] IS NULL OR slug = ANY(%(sites)s::text[]))
UNION ALL
SELECT 'campus', c.id, ST_X(c.geom), ST_Y(c.geom) FROM campus c
 WHERE c.geom IS NOT NULL AND (%(sites)s::text[] IS NULL
       OR c.id IN (SELECT campus_id FROM site_link WHERE site_slug = ANY(%(sites)s::text[]) AND status <> 'rejected'))
"""


@dataclass(frozen=True)
class Item:
    id: str
    day: date
    gsd: float
    bbox: tuple[float, float, float, float]


@dataclass
class Subject:
    subject_type: str
    subject_id: str
    box: Box


# ---------------------------------------------------------------- pure parts
def parse_items(doc: dict[str, Any]) -> list[Item]:
    out = []
    for f in doc.get("features") or []:
        props = f.get("properties") or {}
        when, bbox = props.get("datetime"), f.get("bbox")
        if not (isinstance(f.get("id"), str) and isinstance(when, str) and isinstance(bbox, list) and len(bbox) == 4):
            continue
        gsd = props.get("gsd")
        out.append(Item(f["id"], date.fromisoformat(when[:10]), float(gsd) if isinstance(gsd, int | float) else 0.6,
                        (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))))
    return out


def _covers(b: tuple[float, float, float, float], box: Box) -> bool:
    return b[0] <= box.west and b[1] <= box.south and b[2] >= box.east and b[3] >= box.north


def _part(b: tuple[float, float, float, float], box: Box) -> tuple[float, float, float, float] | None:
    w, s, e, n = max(b[0], box.west), max(b[1], box.south), min(b[2], box.east), min(b[3], box.north)
    return (round(w, 5), round(s, 5), round(e, 5), round(n, 5)) if w < e and s < n else None


def choose(items: list[Item], box: Box) -> list[Item]:
    """The newest acquisition: one item covering the whole box, else that day's items touching it."""
    if not items:
        return []
    covering = sorted((i for i in items if _covers(i.bbox, box)), key=lambda i: (i.day, i.gsd < 1), reverse=True)
    newest = max(i.day for i in items)
    if covering and covering[0].day >= newest:
        return [covering[0]]
    same = [i for i in items if i.day == newest and _part(i.bbox, box)]
    if covering and len(same) <= 1:
        return [covering[0]]
    return sorted(same, key=lambda i: i.id)


def size_px(b: tuple[float, float, float, float]) -> tuple[int, int]:
    lat = (b[1] + b[3]) / 2
    w_m = (b[2] - b[0]) * METERS_PER_DEG_LAT * math.cos(math.radians(lat))
    h_m = (b[3] - b[1]) * METERS_PER_DEG_LAT
    scale = max(w_m, h_m) / M_PER_PX
    f = min(1.0, MAX_PX / scale) if scale else 1.0
    return max(1, round(w_m / M_PER_PX * f)), max(1, round(h_m / M_PER_PX * f))


def render_url(item_id: str, b: tuple[float, float, float, float]) -> str:
    w, h = size_px(b)
    params = {"collection": COLLECTION, "item": item_id, "assets": "image", "asset_bidx": "image|1,2,3", "nodata": 0}
    return f"{DATA_ROOT}/item/bbox/{','.join(f'{v:.5f}' for v in b)}/{w}x{h}.webp?{urlencode(params)}"


def search_url(box: Box) -> str:
    return f"{STAC_ROOT}/search?{urlencode({'collections': COLLECTION, 'bbox': box.text(), 'limit': 100})}"


def item_url(item_id: str) -> str:
    return f"{STAC_ROOT}/collections/{COLLECTION}/items/{quote(item_id, safe='')}"


def caption(day: date, gsd: float) -> str:
    return f"USDA NAIP aerial photo, {day.day} {day:%b %Y}: {gsd:g} m. Public domain (USDA FSA)"


def fact_value(items: list[Item], box: Box) -> dict[str, Any]:
    tiles = []
    for it in items:
        b = box.bounds() if _covers(it.bbox, box) else _part(it.bbox, box)
        if b is not None:
            tiles.append({"item": it.id, "url": render_url(it.id, b), "bbox": list(b)})
    day, gsd = max(i.day for i in items), min(i.gsd for i in items)
    return {"date": day.isoformat(), "gsd": gsd, "url": tiles[0]["url"], "bbox": list(box.bounds()),
            "tiles": tiles, "caption": caption(day, gsd)}


def subject_box(conn: Conn, subject_type: str, subject_id: str, lon: float, lat: float) -> Box:
    row = conn.execute(
        """SELECT value->>'url' AS url FROM fact WHERE subject_type = %s AND subject_id = %s AND field = 'imagery'
             AND status = 'published' ORDER BY valid_from DESC, id DESC LIMIT 1""",
        (subject_type, subject_id),
    ).fetchone()
    if row and row["url"] and "/bbox/" in row["url"]:
        try:
            w, s, e, n = (float(x) for x in row["url"].split("/bbox/")[1].split(".png")[0].split(","))
            if w < e and s < n:
                return Box(w, s, e, n)
        except ValueError:
            pass
    return Box.around(lon, lat)


# ---------------------------------------------------------------- run
def run(*, sites: list[str] | None = None, source: SourceSpec = SOURCE, fetcher: Fetcher | None = None
        ) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        f = fetcher or default_fetcher()
        with db.transaction() as conn:
            rows = conn.execute(_SUBJECTS, {"sites": sites}).fetchall()
            subjects = [Subject(r["subject_type"], r["subject_id"],
                                subject_box(conn, r["subject_type"], r["subject_id"], float(r["lon"]), float(r["lat"])))
                        for r in rows]
        stats.extra.update(subjects=len(subjects), searches=0, without_naip=0)
        for subj in subjects:
            resp = f.get(search_url(subj.box))
            stats.extra["searches"] += 1
            items = choose(parse_items(json.loads(resp.content)), subj.box)
            if not items:
                stats.extra["without_naip"] += 1
                continue
            with db.transaction() as conn:
                docs = [fetch_document(source.id, item_url(i.id), doc_type="stac_item", title=i.id,
                                       doc_date=i.day, external_id=i.id, fetcher=f, conn=conn) for i in items]
                stats.documents_new += sum(d.was_new for d in docs)
                value = fact_value(items, subj.box)
                res = FactWriter(conn).add(
                    subj.subject_type, subj.subject_id, FIELD, value, tag="R", source_name=SOURCE_NAME,
                    source_url=item_url(items[0].id), valid_from=max(i.day for i in items), producer=PRODUCER,
                    status="published", document_id=docs[0].id,
                    quote_loc=f"{', '.join(i.id for i in items)}; box {subj.box.text()}",
                )
                stats.facts_seen += 1
                if res.created:
                    stats.facts_new += 1
                    stats.new_by_series[FIELD] = stats.new_by_series.get(FIELD, 0) + 1
                old = conn.execute(
                    """SELECT id FROM fact WHERE subject_type = %s AND subject_id = %s AND field = %s
                         AND status = 'published' AND id <> %s""",
                    (subj.subject_type, subj.subject_id, FIELD, res.id),
                ).fetchall()
                if old:  # an earlier photo or the same one over an earlier box
                    FactWriter(conn).supersede([r["id"] for r in old], "newer NAIP rendering of the subject's box",
                                               replaced_by={r["id"]: res.id for r in old})

    return collect(source, body)


@app.periodic(cron="41 8 3 * *", periodic_id="monthly")
@app.task(name="collect.naip", queue="collect", queueing_lock="collect.naip", lock="collect.naip")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--site", action="append", dest="sites", default=None,
                        help="only this site slug and its linked campuses (repeatable)")


@cli.action("collect", "naip", help="USDA NAIP aerial imagery, 0.6 m (Planetary Computer)", configure=_configure)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(sites=args.sites))
