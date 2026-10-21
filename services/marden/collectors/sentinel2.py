"""Sentinel-2 L2A construction imagery and change scores from Microsoft Planetary Computer (spec 22
"Construction progress", spec 17 "Sentinel-2 imagery: each pass, change score weekly").

For each V1 site with a point and each campus with a point linked to one (marden.collectors.
sentinel2_store), over a ~1.5 km box:
1. STAC search (GET /search, collection sentinel-2-l2a, tile cloud cover < 20 %, last 18 months).
   Search pages are listings, not stored: PC returns the same results with properties in a
   different key order on every call, so a stored page would be a new document each run. Pages and
   scenes listed are counted in run meta; every scene Marden uses is stored as its own item JSON.
2. Per month, the clearest scene in the box (SCL read in tile-cloud order; marden.collectors.
   sentinel2_stac). Its item JSON is stored raw with fetch_document; the imagery URL is the PC data
   API's true-colour rendering cropped to the box. A month whose pick every subject already has
   is not re-ranked once the month has closed (FINAL_LAG_DAYS after its end).
3. change-score@0.1 against the pick ~3 months earlier, computed in code from rasterio windowed
   reads of B04/B08/B11/SCL (marden.collectors.sentinel2_raster). COG range reads are not stored
   (the files are ~100-200 MB each; the scene and box in quote_loc reproduce them); they are
   counted in run meta. A score is computed once per pick and reused on later runs.
4. last_visible_change = the newest pick whose score is >= CHANGE_THRESHOLD.

Nothing is filled in: a month without a clear-enough scene has no imagery fact, a pair without
enough clear pixels in both scenes has no score. Cadence: every ~5 days (each pass), overnight ET.
`python -m marden collect sentinel2 [--site SLUG ...]`.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from marden import cli
from marden.collectors.rates_common import RunStats, collect, print_stats, today_et
from marden.collectors.sentinel2_raster import (
    CLEAR_ENOUGH,
    SCORE_BANDS,
    ChangeResult,
    CogReader,
    ReadError,
    SceneReader,
    change_score,
    clear_fraction,
)
from marden.collectors.sentinel2_stac import (
    MONTHS,
    Box,
    Scene,
    base_months,
    candidates_by_month,
    choose_tile,
    item_url,
    month_final,
    parse_item,
    parse_search,
    search_url,
    window_start,
)
from marden.collectors.sentinel2_store import (
    CHANGE_THRESHOLD,
    Pick,
    Prior,
    Store,
    Subject,
    by_point,
    load_prior,
    load_subjects,
)
from marden.core import db, documents, http
from marden.core.config import get_settings
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, default_fetcher, fetch_document, marden_user_agent
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source

log = logging.getLogger(__name__)

SOURCE_ID = "sentinel2_pc"
PC_HOST = "planetarycomputer.microsoft.com"
CRON_UTC = "17 7 */5 * *"  # 07:17 UTC (02:17 / 03:17 ET) on days 1, 6, 11, ... 31: about every 5 days
MAX_PAGES = 5
TERMS_NOTE = (
    "Checked 2026-10-21. Planetary Computer Supplemental Terms of Use for Previews "
    "(https://planetarycomputer.microsoft.com/terms, last updated January 2023): \"Previews are not meant for "
    "production use (though such use is not prohibited)\"; SAS tokens \"may not be sublicensed to, distributed "
    "to, or shared with third parties ... Tokens may not be monetized\" and \"rate limiting may be put into "
    "place to limit the amount of egress\" (so signed URLs are used in memory only, never stored or served; "
    "imagery URLs point at the PC data API, which needs no token). PC docs (https://planetarycomputer."
    "microsoft.com/docs/concepts/sas/): \"The data hosted by the Planetary Computer is free for anyone to use\"; "
    "\"The STAC metatdata [sic] API is available to all users and does not require an account or a token to "
    "use\". Microsoft Services Agreement (https://www.microsoft.com/en-us/servicesagreement, effective "
    "2026-09-30) 3.a.vi: \"Don\u2019t circumvent any restrictions on access to, usage, or availability of the "
    "Services (e.g., attempting to “jailbreak” an AI system or impermissible scraping)\" (the documented public "
    "API is used, rate-limited, nothing circumvented). robots.txt: \"User-agent: *\" with no Disallow. Data "
    "licence (the collection's 'license' link, https://sentinel.esa.int/documents/247904/690755/"
    "Sentinel_Data_Legal_Notice): \"users shall have a free, full and open access to Copernicus Sentinel Data\" "
    "for \"(a) reproduction; (b) distribution; (c) communication to the public; (d) adaptation, modification "
    "and combination with other data and information\"; \"Where the Copernicus Sentinel Data and Service "
    "Information have been adapted or modified, the user shall provide the following notice\": \"'Contains "
    "modified Copernicus Sentinel data [Year]' for Sentinel data\" (in every imagery caption). Not blocked. "
    "Polite use: 2 req/s to the API, 4 req/s COG range reads, declared User-Agent."
)
SOURCE = declare_source(SOURCE_ID, "Copernicus Sentinel-2 L2A via Microsoft Planetary Computer", "api",
                        "https://planetarycomputer.microsoft.com/api/stac/v1", 5 * 24 * 60, TERMS_NOTE)
http.set_rate_limit(PC_HOST, 2.0)


@dataclass
class Context:
    source: SourceSpec
    stats: RunStats
    fetcher: Fetcher
    reader: SceneReader
    today: date
    months: int
    windows: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)  # scene id -> band -> raw window

    def count(self, name: str, n: int = 1) -> None:
        self.stats.extra[name] = self.stats.extra.get(name, 0) + n


# ---------------------------------------------------------------- steps
def search(box: Box, ctx: Context) -> list[Scene]:
    url: str | None = search_url(box, window_start(ctx.today, ctx.months))
    scenes: list[Scene] = []
    for _ in range(MAX_PAGES):
        if url is None:
            break
        resp = ctx.fetcher.get(url, use_cache=False)
        ctx.count("search_pages")
        page, url = parse_search(resp.content, box)
        scenes.extend(page)
    ctx.count("scenes_listed", len(scenes))
    return scenes


def rank(candidates: list[Scene], box: Box, ctx: Context) -> tuple[Scene, float] | None:
    """The clearest candidate inside the box; stops at the first one at least CLEAR_ENOUGH clear."""
    best: tuple[Scene, float] | None = None
    for scene in candidates:
        try:
            scl = ctx.reader.read({"SCL": scene.scl_href or ""}, box)["SCL"]
        except ReadError as exc:
            ctx.count("read_errors")
            log.warning("sentinel2: SCL %s: %s", scene.id, exc)
            continue
        ctx.count("scl_reads")
        frac = clear_fraction(scl)
        if best is None or frac > best[1]:
            best = (scene, frac)
        if frac >= CLEAR_ENOUGH:
            break
    return best


def store_item(scene_id: str, day: date, ctx: Context) -> tuple[DocumentRow, dict[str, Any]]:
    doc = fetch_document(ctx.source.id, item_url(scene_id), doc_type="stac_item", title=f"Sentinel-2 L2A {scene_id}",
                         doc_date=day, external_id=scene_id, fetcher=ctx.fetcher)
    ctx.stats.document(doc)
    return doc, parse_item(doc.read())


def choose_picks(box: Box, subjects: list[Subject], prior: Prior, ctx: Context) -> dict[str, Pick]:
    scenes = search(box, ctx)
    tile = choose_tile(scenes)
    picks: dict[str, Pick] = {}
    for month, candidates in (candidates_by_month(scenes, tile) if tile else {}).items():
        row = prior.final_pick(month, subjects) if month_final(month, ctx.today) else None
        if row is not None:
            doc = documents.get_document(row["document_id"])
            if doc is not None:
                picks[month] = Pick(row["value"]["scene_id"], row["valid_from"], month, doc, parse_item(doc.read()),
                                    reused=True)
                ctx.count("picks_reused")
                continue
        chosen = rank(candidates, box, ctx)
        if chosen is None:
            continue
        scene, frac = chosen
        doc, item = store_item(scene.id, scene.day, ctx)
        picks[month] = Pick(scene.id, scene.day, month, doc, item, clear=frac)
    ctx.stats.extra.setdefault("tiles", {})[box.text()] = tile
    return picks


def window(pick: Pick, box: Box, ctx: Context) -> dict[str, np.ndarray] | None:
    cached = ctx.windows.get(pick.scene_id)
    if cached is not None:
        return cached
    hrefs = {b: pick.item["hrefs"][b] for b in ("SCL", *SCORE_BANDS)}
    try:
        raw = ctx.reader.read(hrefs, box)
    except ReadError as exc:
        ctx.count("read_errors")
        log.warning("sentinel2: bands %s: %s", pick.scene_id, exc)
        return None
    ctx.count("band_window_reads")
    ctx.windows[pick.scene_id] = raw
    return raw


def score_pair(pick: Pick, base: Pick, box: Box, ctx: Context) -> ChangeResult | None:
    later, earlier = window(pick, box, ctx), window(base, box, ctx)
    if later is None or earlier is None:
        return None
    return change_score(later, pick.item["baseline"], earlier, base.item["baseline"])


def collect_point(subjects: list[Subject], ctx: Context) -> dict[str, Any]:
    box = Box.around(subjects[0].lon, subjects[0].lat)
    with db.transaction() as conn:
        prior = load_prior(conn, subjects)
    picks = choose_picks(box, subjects, prior, ctx)
    with db.transaction() as conn:
        store = Store(conn, ctx.stats, subjects, box)
        for pick in picks.values():
            if not pick.reused:
                store.imagery(pick)

    scores: dict[str, float] = {}
    for month, pick in picks.items():
        base = next((picks[m] for m in base_months(month) if m in picks), None)
        if base is None:
            continue
        known = prior.score(pick.day, subjects)
        if known is not None:
            scores[month] = known
            ctx.count("scores_reused")
            continue
        result = score_pair(pick, base, box, ctx)
        if result is None:
            ctx.count("scores_not_computed")
            continue
        with db.transaction() as conn:
            Store(conn, ctx.stats, subjects, box).change_score(pick, base, result)
        ctx.count("scores_computed")
        scores[month] = result.score

    changed = [m for m, s in scores.items() if s >= CHANGE_THRESHOLD]
    if changed:
        last = max(changed)
        with db.transaction() as conn:
            Store(conn, ctx.stats, subjects, box).last_change(picks[last], scores[last])
    ctx.windows.clear()
    return {"subjects": [s.key for s in subjects], "box": box.text(), "months": len(picks),
            "scores": {m: scores[m] for m in sorted(scores)},
            "last_visible_change": picks[max(changed)].day.isoformat() if changed else None}


def run(
    *, sites: list[str] | None = None, source: SourceSpec = SOURCE, fetcher: Fetcher | None = None,
    reader: SceneReader | None = None, today: date | None = None, months: int = MONTHS,
) -> dict[str, Any]:
    """One collection run over every V1 point (or only `sites` and their campuses)."""

    def body(stats: RunStats) -> None:
        with db.transaction() as conn:
            subjects = load_subjects(conn, sites)
        cog = reader or CogReader(marden_user_agent(get_settings()))
        ctx = Context(source, stats, fetcher or default_fetcher(), cog, today or today_et(), months)
        if sites:
            found = {s.subject_id for s in subjects if s.subject_type == "site"}
            stats.extra["sites_without_point"] = sorted(set(sites) - found)
        points = by_point(subjects)
        stats.extra.update(points=len(points), subjects=len(subjects), by_point=[])
        for group in points.values():
            stats.extra["by_point"].append(collect_point(group, ctx))
        if isinstance(cog, CogReader):
            stats.extra.update(cog_requests=cog.reads, cog_errors=cog.errors)
        reads_ok = stats.extra.get("scl_reads") or stats.extra.get("band_window_reads")
        if stats.extra.get("read_errors") and not reads_ok:
            raise http.FetchError("https://sentinel2l2a01.blob.core.windows.net", None, "every COG read failed")

    return collect(source, body)


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="every_5_days")
@app.task(name="collect.sentinel2", queue="collect", queueing_lock="collect.sentinel2", lock="collect.sentinel2")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


def _configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--site", action="append", dest="sites", default=None,
                        help="only this V1 site slug and its linked campuses (repeatable)")


@cli.action("collect", "sentinel2", help="Sentinel-2 L2A imagery and change scores (Planetary Computer)",
            configure=_configure)
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run(sites=args.sites))
