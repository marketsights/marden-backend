"""A financed site's campuses, buildings, plants and imagery (spec 16), as known on the build date.

Site links are approved once by a person. A site shows its approved links; while none is approved
yet it shows the proposed ones and says so (`linkApproved: false` on the Buildings tab). Rejected
links never show. Plants are the ones serving those campuses (`plant_serves`) plus the plants whose
`serves_sites` fact names the site.
"""

from __future__ import annotations

import json
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_facts
from marden.readmodels.registry import get_builder
from marden.readmodels.views.imagery_context import building_stage, image_items, imagery_campuses
from marden.readmodels.views.sites_common import Doc, day, day_end, src_or_nd, subjects_listing

BUILDING_FACTS = ("status", "sqft", "facility_mw", "it_mw", "building_permit", "co_date", "construction_start",
                  "change_score", "construction_complete")


def linked_campuses(conn: Conn, slug: str) -> tuple[list[Row], bool]:
    """(campus rows with the link's building_ids, approved?) for the site."""
    rows = conn.execute(
        """SELECT l.status AS link_status, l.building_ids, c.* FROM site_link l JOIN campus c ON c.id = l.campus_id
           WHERE l.site_slug = %s AND (l.status = 'proposed'
                 OR (l.status = 'approved' AND (l.approved_at IS NULL OR l.approved_at < %s)))
           ORDER BY c.id""",
        (slug, day_end()),
    ).fetchall()
    approved = [r for r in rows if r["link_status"] == "approved"]
    if approved:
        return approved, True
    return rows, False


def campus_buildings(conn: Conn, campuses: list[Row]) -> list[Row]:
    out: list[Row] = []
    for campus in campuses:
        ids = campus.get("building_ids")
        rows = conn.execute(
            """SELECT id, campus_id, label, ST_AsGeoJSON(footprint)::jsonb AS footprint, created_at FROM building
               WHERE campus_id = %s AND (%s::text[] IS NULL OR id = ANY(%s::text[])) ORDER BY id""",
            (campus["id"], ids, ids),
        ).fetchall()
        out.extend(rows)
    out.sort(key=lambda b: (b["campus_id"], _label_order(b["label"] or b["id"])))
    return out


def _label_order(label: str) -> tuple:
    digits = "".join(ch for ch in label if ch.isdigit())
    return (int(digits) if digits else 10**6, label)


def building_doc(conn: Conn, b: Row) -> Doc | None:
    """types.ts Building. A building without a footprint polygon is left out (the type needs one)."""
    footprint = b.get("footprint")
    if isinstance(footprint, str):
        footprint = json.loads(footprint)
    if not isinstance(footprint, dict) or footprint.get("type") != "Polygon":
        return None
    facts = known_facts(conn, "building", b["id"], BUILDING_FACTS)
    start = facts.get("construction_start")
    doc: Doc = {
        "id": b["id"], "label": b["label"] or b["id"], "footprint": footprint,
        "sqft": src_or_nd(facts.get("sqft"), "F"), "facilityMw": src_or_nd(facts.get("facility_mw"), "F"),
        "itMw": src_or_nd(facts.get("it_mw"), "F"), "status": src_or_nd(facts.get("status"), "F"),
        "buildingPermit": src_or_nd(facts.get("building_permit"), "F"), "coDate": src_or_nd(facts.get("co_date"), "F"),
        "firstVisible": start["value"] if start and isinstance(start["value"], str) else None,
    }
    if facts.get("change_score"):
        doc["changeScore"] = src_or_nd(facts["change_score"], "M")
    if (stage := building_stage(conn, b["id"])) is not None:
        doc["imageryStage"] = stage  # Sentinel-2 construction stage (M): never an operating status
    return doc


def building_counts(conn: Conn, buildings: list[Row]) -> dict[str, int]:
    """built / total from building status facts (Operating = built)."""
    built = 0
    for b in buildings:
        status = known_facts(conn, "building", b["id"], ["status"]).get("status")
        built += bool(status and status["value"] == "Operating")
    return {"built": built, "total": len(buildings)}


def site_plants(conn: Conn, slug: str, campuses: list[Row]) -> list[Row]:
    """Plants serving the site's campuses, and plants whose serves_sites fact names the site."""
    ids = [c["id"] for c in campuses]
    rows = conn.execute(
        """SELECT p.* FROM power_plant p
           WHERE p.id IN (SELECT plant_id FROM plant_serves WHERE campus_id = ANY(%s)) OR p.id = ANY(%s)
           ORDER BY p.id""",
        (ids, subjects_listing(conn, "plant", "serves_sites", slug)),
    ).fetchall()
    # Candidates from any published serves_sites row; keep those whose fact as known on the build
    # date (known_facts) still names the site, or that serve one of its campuses.
    out = []
    for p in rows:
        serves = known_facts(conn, "plant", p["id"], ["serves_sites"]).get("serves_sites")
        linked = conn.execute(
            "SELECT 1 FROM plant_serves WHERE plant_id = %s AND campus_id = ANY(%s)", (p["id"], ids)
        ).fetchone()
        if linked or (serves and isinstance(serves["value"], list) and slug in serves["value"]):
            out.append(p)
    return out


def read_doc(conn: Conn, view: str, key: str) -> Any:
    """Another view's document as of the build date (the plant row a site's power tab embeds). Built
    with that view's own builder when it is registered, so the embed is never older than the facts
    this rebuild reads (a stored doc may be rebuilt later in the same dirty cycle); the stored
    snapshot otherwise."""
    b = get_builder(view)
    if b is not None and b.build_one is not None:
        return b.build_one(conn, key)
    row = conn.execute(
        "SELECT doc FROM read_model WHERE view = %s AND key = %s AND as_of <= %s ORDER BY as_of DESC LIMIT 1",
        (view, key, day()),
    ).fetchone()
    return row["doc"] if row else None


def imagery(conn: Conn, slug: str) -> list[Doc]:
    """Monthly Sentinel-2 images (oldest first) with the change score of the scene, when scored, and the
    box, day and comparison scene the markup needs (imagery_context.image_items)."""
    return image_items(conn, "site", slug)


def site_imagery_campuses(conn: Conn, slug: str) -> list[Doc]:
    """The site's linked campuses (approved, else proposed) with what the image markup needs."""
    campuses, _approved = linked_campuses(conn, slug)
    return imagery_campuses(conn, [c["id"] for c in campuses])


def last_visible_change(conn: Conn, slug: str) -> Doc | None:
    row = known_facts(conn, "site", slug, ["last_visible_change"]).get("last_visible_change")
    if not row or not isinstance(row["value"], str):
        return None
    out = src_or_nd(row, "M")
    out["value"] = f"Last visible change in imagery: {row['value']}"
    return out
