"""Image markup data (imagery_context): each Sentinel-2 image's box, day and comparison scene, and per
linked campus the building outlines, plan, counts, schedule text and Marden's imagery read. Real seeded
rows copied from the local `marden` database (facility_rows, site_rows); expectations are read from the
copied facts and buildings with SQL, not from the builders.
"""

from __future__ import annotations

from facility_rows import copy_facilities
from real_rows import copied_rows
from site_rows import copy_sites

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import facilities_campus, sites_detail, sites_tabs  # noqa: F401 (registers builders)
from marden.readmodels.views.imagery_context import image_bbox, scene_day

ABILENE_CAMPUS, ABILENE_SITE = "stargate-abilene", "abilene"
PC = "https://planetarycomputer.microsoft.com/api/data/v1/item/bbox/-99.78899,32.49832,-99.77301,32.51180.png?x=1"


def build(view: str, key: str) -> dict:
    b = get_builder(view)
    assert b is not None
    with building(business_today()), db.transaction() as conn:
        return b.build_one(conn, key)


def test_bbox_and_scene_day_parse_what_the_collector_writes():
    assert image_bbox(PC) == [-99.78899, 32.49832, -99.77301, 32.5118]
    assert image_bbox("https://example.com/a.png") is None
    assert image_bbox(PC.replace("-99.78899", "-99.70000")) is None  # west east of east: not a box
    assert scene_day("S2B_MSIL2A_20261008T172209_R012_T14SMB_20261008T222906") == "2026-10-08"
    assert scene_day("not a scene") is None


def _imagery_rows(subject_type: str, subject_id: str) -> dict[str, dict]:
    rows = copied_rows(
        """SELECT * FROM fact WHERE subject_type = %s AND subject_id = %s AND field = 'imagery'
             AND status = 'published' ORDER BY valid_from DESC, id DESC""",
        (subject_type, subject_id),
    )
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["value"]["month"], r)
    return out


def _scores(subject_type: str, subject_id: str) -> dict[str, dict]:
    rows = copied_rows(
        """SELECT * FROM fact WHERE subject_type = %s AND subject_id = %s AND field = 'change_score'
             AND status = 'published' ORDER BY valid_from DESC, id DESC""",
        (subject_type, subject_id),
    )
    return {r["quote_loc"].split(" vs ")[0]: r for r in rows}


def _check_images(images: list[dict], subject_type: str, subject_id: str) -> None:
    rows, scores = _imagery_rows(subject_type, subject_id), _scores(subject_type, subject_id)
    assert images and [i["month"] for i in images] == sorted(rows)[-len(images):]
    for img in images:
        row = rows[img["month"]]
        assert img["url"] == row["value"]["url"] and img["date"] == row["valid_from"].isoformat()
        in_url = [float(p) for p in row["value"]["url"].split("/bbox/")[1].split(".png")[0].split(",")]
        assert img["bbox"] == in_url
        w, s, e, n = img["bbox"]
        assert w < e and s < n
        score = scores.get(row["value"]["scene_id"])
        if score is None:
            assert "changeScore" not in img
            continue
        base = score["quote_loc"].split(" vs ")[1].split(";")[0]
        assert img["changeScore"] == score["value"] and img["changeMethod"] == score["method_version"]
        assert img["comparedWith"] == f"{base[11:15]}-{base[15:17]}-{base[17:19]}"
        assert img["comparedWith"] < img["date"] and img["changeThreshold"] == 0.05


def _check_campus(entry: dict, campus_id: str) -> None:
    polygons = copied_rows(
        "SELECT id, label FROM building WHERE campus_id = %s AND GeometryType(footprint) = 'POLYGON'", (campus_id,)
    )
    assert {b["id"] for b in entry["buildings"]} == {r["id"] for r in polygons}
    for b in entry["buildings"]:
        start = copied_rows(
            """SELECT value #>> '{}' AS v FROM fact WHERE subject_type = 'building' AND subject_id = %s
                 AND field = 'construction_start' AND status = 'published'""",
            (b["id"],),
        )
        assert b["constructionStart"]["value"] == (start[0]["v"][:10] if start else None)
        assert b["footprint"]["type"] == "Polygon"
    plan = copied_rows(
        """SELECT value FROM fact WHERE subject_type = 'campus' AND subject_id = %s AND field = 'capacity_plan'
             AND status = 'published' ORDER BY valid_from DESC, id DESC LIMIT 1""",
        (campus_id,),
    )
    steps = plan[0]["value"] if plan else []
    want = sorted(({"date": s["date"][:10], "buildingsOperational": s["buildings_operational"]} for s in steps),
                  key=lambda s: s["date"]) if plan else None
    assert entry["plan"]["value"] == want
    note = copied_rows(
        """SELECT * FROM fact WHERE subject_type = 'campus' AND subject_id = %s AND field = 'imagery_note'
             AND status = 'published' ORDER BY valid_from DESC, id DESC LIMIT 1""",
        (campus_id,),
    )
    if note:
        assert entry["read"]["value"]["text"] == note[0]["value"]["read"]
        assert entry["read"]["value"]["flag"] == note[0]["value"]["flag"]
        assert (entry["read"]["tag"], entry["read"]["sourceName"]) == (note[0]["tag"], note[0]["source_name"])
    else:
        assert entry["read"]["value"] is None


def test_campus_detail_carries_image_boxes_and_the_markup_data():
    campuses = copy_facilities()
    assert ABILENE_CAMPUS in campuses
    detail = build("dc_detail", ABILENE_CAMPUS)
    _check_images(detail["imagery"], "campus", ABILENE_CAMPUS)
    [entry] = detail["imageryCampuses"]
    assert entry["id"] == ABILENE_CAMPUS
    _check_campus(entry, ABILENE_CAMPUS)


def test_site_schedule_and_evidence_carry_the_linked_campuses():
    slugs = copy_sites()
    assert ABILENE_SITE in slugs
    linked = [r["campus_id"] for r in copied_rows(
        "SELECT campus_id FROM site_link WHERE site_slug = %s AND status = 'approved' ORDER BY campus_id",
        (ABILENE_SITE,),
    )]
    schedule, evidence = build("site_schedule", ABILENE_SITE), build("site_tab:evidence", ABILENE_SITE)
    _check_images(schedule["imagery"], "site", ABILENE_SITE)
    _check_images(evidence["imagery"], "site", ABILENE_SITE)
    assert len(schedule["imagery"]) <= 3 and schedule["imagery"] == evidence["imagery"][-len(schedule["imagery"]):]
    for doc in (schedule, evidence):
        assert [c["id"] for c in doc["imageryCampuses"]] == linked
        for entry in doc["imageryCampuses"]:
            _check_campus(entry, entry["id"])


def test_building_stage_readings_and_roofed_by_follow_the_stored_observations():
    campuses = copy_facilities()
    assert ABILENE_CAMPUS in campuses
    rows = copied_rows(
        """SELECT f.subject_id, b.campus_id FROM fact f JOIN building b ON b.id = f.subject_id
            WHERE f.subject_type = 'building' AND f.field = 'imagery_stage' AND f.status = 'published'
            GROUP BY 1, 2 ORDER BY 1 LIMIT 1""",
    )
    if not rows:
        return  # the stage model has not run on the source database
    bid, cid = rows[0]["subject_id"], rows[0]["campus_id"]
    stored = copied_rows(
        """SELECT DISTINCT ON (valid_from) valid_from, value, confidence, tag, method_version FROM fact
            WHERE subject_type = 'building' AND subject_id = %s AND field = 'imagery_stage' AND status = 'published'
            ORDER BY valid_from, id DESC""",
        (bid,),
    )
    [entry] = build("dc_detail", cid)["imageryCampuses"]
    got = next(b for b in entry["buildings"] if b["id"] == bid)["imageryStage"]
    assert [r["date"] for r in got["readings"]] == [r["valid_from"].isoformat() for r in stored]
    assert [r["stage"] for r in got["readings"]] == [r["value"]["stage"] for r in stored]
    assert {r["stage"] for r in got["readings"]} <= {"bare_ground", "under_construction", "roofed"}  # never operating
    latest = got["latest"]
    assert latest["value"]["stage"] == stored[-1]["value"]["stage"]
    assert (latest["tag"], latest["methodVersion"]) == ("M", stored[-1]["method_version"])
    if got["roofedBy"]["value"] is not None:
        start = next(r for r in got["readings"] if r["date"] == got["roofedBy"]["value"])
        assert start["stage"] == "roofed" and start["confidence"] >= 0.6
        later = [r for r in got["readings"] if r["date"] >= start["date"]]
        assert sum(r["stage"] == "roofed" for r in later) >= 2 / 3 * len(later)
    tab = {b["id"]: b for b in build("dc_tab:buildings", cid)}
    assert tab[bid]["imageryStage"] == got


def test_naip_is_the_newest_stored_aerial_photo():
    campuses = copy_facilities()
    assert ABILENE_CAMPUS in campuses
    rows = copied_rows(
        """SELECT * FROM fact WHERE subject_type = 'campus' AND subject_id = %s AND field = 'imagery_naip'
             AND status = 'published' ORDER BY valid_from DESC, id DESC LIMIT 1""",
        (ABILENE_CAMPUS,),
    )
    detail = build("dc_detail", ABILENE_CAMPUS)
    if not rows:
        assert "imageryNaip" not in detail
        return
    got = detail["imageryNaip"]
    assert got["value"]["date"] == rows[0]["value"]["date"] and got["tag"] == rows[0]["tag"]
    assert [t["url"] for t in got["value"]["tiles"]] == [t["url"] for t in rows[0]["value"]["tiles"]]
    assert got["sourceName"] == rows[0]["source_name"]
