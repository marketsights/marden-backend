"""Subjects and fact writes for the Sentinel-2 collector.

Subjects: every V1 site with a point (site.geom) and every campus with a point linked to a V1 site
(site_link not rejected). Subjects at the same point share one box and one set of scenes.

Facts (published; valid_from = the scene's acquisition date):
- `imagery` (tag R): {month, url, caption, scene_id}, one per subject per month: the month's clearest
  scene. While a month is open a clearer later pass adds a new fact for that month (later
  valid_from); per month the newest fact is the current pick.
- `change_score` (tag M, change-score@0.1, unit 'reflectance'): the month's pick against the pick
  ~3 months earlier; quote_loc names both scenes and the clear-pixel overlap.
- `last_visible_change` (tag M, change-score@0.1): ISO date of the newest pick whose change score is
  at least CHANGE_THRESHOLD.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.collectors.rates_common import RunStats
from marden.collectors.sentinel2_raster import METHOD_VERSION, ChangeResult
from marden.collectors.sentinel2_stac import Box, caption, imagery_url, item_url
from marden.core.db import Conn
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter

PRODUCER = "collector:sentinel2"
SOURCE_NAME = "Copernicus Sentinel-2 L2A via Microsoft Planetary Computer"
IMAGERY = "imagery"
CHANGE_SCORE = "change_score"
LAST_CHANGE = "last_visible_change"
SCORE_UNIT = "reflectance"
# change-score@0.1: a mean absolute B04/B08/B11 reflectance difference of 0.05 or more over the box
# against the scene ~3 months earlier counts as visible change (see sentinel2_raster for the score).
CHANGE_THRESHOLD = 0.05


@dataclass(frozen=True)
class Subject:
    subject_type: str
    subject_id: str
    lon: float
    lat: float

    @property
    def key(self) -> str:
        return f"{self.subject_type}:{self.subject_id}"

    @property
    def point(self) -> tuple[float, float]:
        return (round(self.lon, 5), round(self.lat, 5))


@dataclass
class Pick:
    """A month's chosen scene: its stored item document and the parsed item."""

    scene_id: str
    day: date
    month: str
    doc: DocumentRow
    item: dict[str, Any]
    clear: float | None = None  # None when reused from an earlier run's fact
    reused: bool = False


@dataclass
class Prior:
    """What earlier runs wrote for a point's subjects."""

    imagery: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)  # key -> month -> row
    scores: dict[str, dict[date, float]] = field(default_factory=dict)  # key -> pick day -> score

    def final_pick(self, month: str, subjects: Iterable[Subject]) -> dict[str, Any] | None:
        """The month's pick when every subject already has the same one."""
        rows = [self.imagery.get(s.key, {}).get(month) for s in subjects]
        if not rows or any(r is None for r in rows):
            return None
        ids = {r["value"]["scene_id"] for r in rows if r}
        return rows[0] if len(ids) == 1 and rows[0] and rows[0]["document_id"] else None

    def score(self, day: date, subjects: Iterable[Subject]) -> float | None:
        """The stored score for a pick day when every subject has the same one."""
        values = {self.scores.get(s.key, {}).get(day) for s in subjects}
        return next(iter(values)) if len(values) == 1 and None not in values else None


_SUBJECTS = """
SELECT 'site' AS subject_type, s.slug AS subject_id, ST_X(s.geom) AS lon, ST_Y(s.geom) AS lat
  FROM site s
 WHERE s.in_v1 AND s.geom IS NOT NULL AND (%(sites)s::text[] IS NULL OR s.slug = ANY(%(sites)s))
UNION
SELECT 'campus', c.id, ST_X(c.geom), ST_Y(c.geom)
  FROM campus c
  JOIN site_link l ON l.campus_id = c.id AND l.status <> 'rejected'
  JOIN site s ON s.slug = l.site_slug AND s.in_v1
 WHERE c.geom IS NOT NULL AND (%(sites)s::text[] IS NULL OR s.slug = ANY(%(sites)s))
ORDER BY 1 DESC, 2
"""


def load_subjects(conn: Conn, sites: list[str] | None = None) -> list[Subject]:
    rows = conn.execute(_SUBJECTS, {"sites": sites}).fetchall()
    return [Subject(r["subject_type"], r["subject_id"], float(r["lon"]), float(r["lat"])) for r in rows]


def by_point(subjects: list[Subject]) -> dict[tuple[float, float], list[Subject]]:
    out: dict[tuple[float, float], list[Subject]] = {}
    for s in subjects:
        out.setdefault(s.point, []).append(s)
    return out


def load_prior(conn: Conn, subjects: list[Subject]) -> Prior:
    prior = Prior()
    keys = {(s.subject_type, s.subject_id): s.key for s in subjects}
    rows = conn.execute(
        """SELECT id, subject_type, subject_id, field, value, valid_from, document_id FROM fact
            WHERE producer = %s AND status = 'published' AND field = ANY(%s)
              AND (subject_type || ':' || subject_id) = ANY(%s)
              AND (field <> %s OR method_version = %s)
            ORDER BY valid_from, id""",
        (PRODUCER, [IMAGERY, CHANGE_SCORE], list(keys.values()), CHANGE_SCORE, METHOD_VERSION),
    ).fetchall()
    for r in rows:  # oldest first, so the newest row per month / day wins
        key = keys[(r["subject_type"], r["subject_id"])]
        if r["field"] == IMAGERY and isinstance(r["value"], dict) and r["value"].get("month"):
            prior.imagery.setdefault(key, {})[r["value"]["month"]] = dict(r)
        elif r["field"] == CHANGE_SCORE and isinstance(r["value"], int | float):
            prior.scores.setdefault(key, {})[r["valid_from"]] = float(r["value"])
    return prior


class Store:
    """Published facts for one point's subjects, counted in the run stats."""

    def __init__(self, conn: Conn, stats: RunStats, subjects: list[Subject], box: Box):
        self.facts = FactWriter(conn)
        self.stats = stats
        self.subjects = subjects
        self.box = box

    def _add(self, field_name: str, value: Any, pick: Pick, **extra: Any) -> None:
        for s in self.subjects:
            res = self.facts.add(
                s.subject_type, s.subject_id, field_name, value, source_name=SOURCE_NAME,
                source_url=item_url(pick.scene_id), valid_from=pick.day, producer=PRODUCER, status="published",
                document_id=pick.doc.id, **extra,
            )
            self.stats.facts_seen += 1
            if res.created:
                self.stats.facts_new += 1
                self.stats.new_by_series[field_name] = self.stats.new_by_series.get(field_name, 0) + 1

    def imagery(self, pick: Pick) -> None:
        if pick.clear is None:
            raise ValueError("imagery needs the pick's clear fraction")
        value = {"month": pick.month, "url": imagery_url(pick.scene_id, self.box),
                 "caption": caption(pick.day, pick.clear), "scene_id": pick.scene_id}
        self._add(IMAGERY, value, pick, tag="R")

    def change_score(self, pick: Pick, base: Pick, result: ChangeResult) -> None:
        loc = (f"{pick.scene_id} vs {base.scene_id}; {result.valid_pixels} px clear in both "
               f"({result.valid_fraction:.0%} of box {self.box.text()}); B04,B08,B11")
        self._add(CHANGE_SCORE, result.score, pick, tag="M", method_version=METHOD_VERSION, unit=SCORE_UNIT,
                  quote_loc=loc)

    def last_change(self, pick: Pick, score: float) -> None:
        loc = f"{pick.scene_id}: change_score {score} >= {CHANGE_THRESHOLD}"
        self._add(LAST_CHANGE, pick.day.isoformat(), pick, tag="M", method_version=METHOD_VERSION, quote_loc=loc)
