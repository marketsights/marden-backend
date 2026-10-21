"""Who gets an alert: every member of every firm watching the affected name, directly or through the chain.

A firm watches sites and instruments (watchlist_item). A change on a site reaches watchers of the site and
of the instruments that finance it; on an instrument, watchers of the instrument and of its site; on an
entity, the sites it is a chain party of and the instruments it issues (with their sites and instruments);
on a campus, building or plant, the sites linked to it by an approved site_link (a plant through the
campuses it serves); on an obligation, its site and instruments. Members qualify when the firm's 'marden'
product is live (org_product active, in its dates; product active) and they hold a role on it
(member_product_role), the same test the API's grants use. Their alert settings are the stored ones over
the spec defaults (00-global: every type on, instant red, daily digest, weekly note, 2-pt threshold).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from marden.core.db import Conn

PRODUCT = "marden"
ALERT_TYPES = ("cushion", "milestone", "power", "opposition", "tenant", "price_or_mark", "construction_stall")
LIVE_SQL = """op.status = 'active' AND p.active AND op.starts_on <= (now() AT TIME ZONE 'UTC')::date
              AND (op.ends_on IS NULL OR op.ends_on >= (now() AT TIME ZONE 'UTC')::date)"""

TARGETS_SQL = """
WITH sites AS (
  SELECT %(id)s AS slug WHERE %(type)s = 'site'
  UNION SELECT site_slug FROM instrument WHERE %(type)s = 'instrument' AND id = %(id)s
  UNION SELECT site_slug FROM chain_party WHERE %(type)s = 'entity' AND entity_id = %(id)s
  UNION SELECT site_slug FROM instrument WHERE %(type)s = 'entity' AND issuer_entity_id = %(id)s
  UNION SELECT site_slug FROM site_link WHERE %(type)s = 'campus' AND campus_id = %(id)s AND status = 'approved'
  UNION SELECT sl.site_slug FROM building b JOIN site_link sl ON sl.campus_id = b.campus_id AND sl.status = 'approved'
        WHERE %(type)s = 'building' AND b.id = %(id)s
  UNION SELECT sl.site_slug FROM plant_serves ps JOIN site_link sl ON sl.campus_id = ps.campus_id
        AND sl.status = 'approved' WHERE %(type)s = 'plant' AND ps.plant_id = %(id)s
  UNION SELECT site_slug FROM obligation WHERE %(type)s = 'obligation' AND id = %(id)s
),
instruments AS (
  SELECT %(id)s AS id WHERE %(type)s = 'instrument'
  UNION SELECT id FROM instrument WHERE site_slug IN (SELECT slug FROM sites WHERE slug IS NOT NULL)
  UNION SELECT id FROM instrument WHERE %(type)s = 'entity' AND issuer_entity_id = %(id)s
  UNION SELECT unnest(instrument_ids) FROM obligation WHERE %(type)s = 'obligation' AND id = %(id)s
),
targets AS (
  SELECT 'site'::text AS kind, slug AS ref_id FROM sites WHERE slug IS NOT NULL
  UNION SELECT 'instrument', id FROM instruments WHERE id IS NOT NULL
),
watching AS (
  SELECT w.org_id, array_agg(DISTINCT coalesce(s.name, i.name, w.ref_id) ORDER BY coalesce(s.name, i.name, w.ref_id))
         AS via
  FROM watchlist_item w JOIN targets t ON t.kind = w.kind AND t.ref_id = w.ref_id
  LEFT JOIN site s ON w.kind = 'site' AND s.slug = w.ref_id
  LEFT JOIN instrument i ON w.kind = 'instrument' AND i.id = w.ref_id
  GROUP BY w.org_id
)
SELECT wa.org_id, wa.via, m.user_id, u.email, u.name, st.settings
FROM watching wa
JOIN membership m ON m.org_id = wa.org_id
JOIN app_user u ON u.id = m.user_id
JOIN member_product_role r ON r.org_id = m.org_id AND r.user_id = m.user_id AND r.product_id = %(product)s
JOIN org_product op ON op.org_id = r.org_id AND op.product_id = r.product_id
JOIN product p ON p.id = op.product_id
LEFT JOIN alert_settings st ON st.org_id = m.org_id AND st.user_id = m.user_id
WHERE """ + LIVE_SQL + """
ORDER BY wa.org_id, m.user_id
"""


def default_settings() -> dict[str, Any]:
    return {"types": dict.fromkeys(ALERT_TYPES, True), "instantRed": True, "dailyDigest": True,
            "weeklyNote": True, "priceThresholdPts": 2}


def merged_settings(stored: dict[str, Any] | None) -> dict[str, Any]:
    """Stored settings over the defaults, as the API's getAlertSettings merges them."""
    out = default_settings()
    if stored:
        out.update({k: v for k, v in stored.items() if k != "types"})
        out["types"] = {**out["types"], **(stored.get("types") or {})}
    return out


@dataclass(frozen=True)
class Recipient:
    org_id: str
    user_id: str
    email: str
    name: str | None
    via: tuple[str, ...]
    settings: dict[str, Any] = field(hash=False, compare=False)

    def wants(self, alert_types: list[str], threshold_pts: int | None) -> bool:
        """The user takes at least one of the finding's alert types (and, for a price move, it is at their
        threshold)."""
        types = self.settings.get("types") or {}
        if not any(types.get(kind, True) for kind in alert_types):
            return False
        return threshold_pts is None or int(self.settings.get("priceThresholdPts", 2)) == threshold_pts


def recipients(conn: Conn, subject_type: str, subject_id: str) -> list[Recipient]:
    rows = conn.execute(TARGETS_SQL, {"type": subject_type, "id": subject_id, "product": PRODUCT}).fetchall()
    return [Recipient(r["org_id"], r["user_id"], r["email"], r["name"], tuple(r["via"] or ()),
                      merged_settings(r["settings"])) for r in rows]


def live_member(conn: Conn, org_id: str, user_id: str) -> dict[str, Any] | None:
    """The member's email and merged settings while their access is live (None otherwise)."""
    row = conn.execute(
        """SELECT u.email, u.name, st.settings FROM membership m
           JOIN app_user u ON u.id = m.user_id
           JOIN member_product_role r ON r.org_id = m.org_id AND r.user_id = m.user_id AND r.product_id = %s
           JOIN org_product op ON op.org_id = r.org_id AND op.product_id = r.product_id
           JOIN product p ON p.id = op.product_id
           LEFT JOIN alert_settings st ON st.org_id = m.org_id AND st.user_id = m.user_id
           WHERE m.org_id = %s AND m.user_id = %s AND """ + LIVE_SQL,
        (PRODUCT, org_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return {"email": row["email"], "name": row["name"], "settings": merged_settings(row["settings"])}


def live_members(conn: Conn) -> list[dict[str, Any]]:
    """Every member with live access, with merged settings (digests)."""
    rows = conn.execute(
        """SELECT m.org_id, m.user_id, u.email, u.name, st.settings FROM membership m
           JOIN app_user u ON u.id = m.user_id
           JOIN member_product_role r ON r.org_id = m.org_id AND r.user_id = m.user_id AND r.product_id = %s
           JOIN org_product op ON op.org_id = r.org_id AND op.product_id = r.product_id
           JOIN product p ON p.id = op.product_id
           LEFT JOIN alert_settings st ON st.org_id = m.org_id AND st.user_id = m.user_id
           WHERE """ + LIVE_SQL + " ORDER BY m.org_id, m.user_id",
        (PRODUCT,),
    ).fetchall()
    return [{**r, "settings": merged_settings(r["settings"])} for r in rows]


# ------------------------------------------------------------------ names and screen links
NAME_SQL = {
    "site": "SELECT name FROM site WHERE slug = %s",
    "instrument": "SELECT name FROM instrument WHERE id = %s",
    "entity": "SELECT name FROM entity WHERE id = %s",
    "campus": "SELECT name FROM campus WHERE id = %s",
    "building": "SELECT coalesce(label, id) AS name FROM building WHERE id = %s",
    "plant": "SELECT name FROM power_plant WHERE id = %s",
}


def subject_name(conn: Conn, subject_type: str, subject_id: str) -> str:
    sql = NAME_SQL.get(subject_type)
    row = conn.execute(sql, (subject_id,)).fetchone() if sql else None
    return str(row["name"]) if row and row.get("name") else subject_id


def ref_url(conn: Conn, subject_type: str, subject_id: str, tab: str | None = None) -> str:
    """The screen an alert links to (00-global routes)."""
    if subject_type == "site":
        return f"/sites/{subject_id}" + (f"?tab={tab}" if tab else "")
    if subject_type == "instrument":
        return f"/instruments/{subject_id}"
    if subject_type == "entity":
        return f"/payers/{subject_id}"
    if subject_type == "campus":
        return f"/data-centers/{subject_id}"
    if subject_type == "building":
        row = conn.execute("SELECT campus_id FROM building WHERE id = %s", (subject_id,)).fetchone()
        return f"/data-centers/{row['campus_id']}?tab=buildings" if row else "/data-centers"
    if subject_type == "plant":
        return f"/power/{subject_id}"
    return "/watchlist"
