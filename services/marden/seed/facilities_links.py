"""Site-link proposals: financed site -> campus, for a person to approve (spec 17).

Sources: the dcmap / site.json ``case`` of a covered campus (19 pairs), plus the two links the
dictionary derives from names only (APLD_PF1B4 -> apld_polaris_forge_1, GLXY_H1 -> galaxy_helios),
which carry an M evidence fact. Every link is inserted as 'proposed' with an open review item; a
rerun never touches a link a person has already approved or rejected.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn
from marden.core.jsonutil import jsonb
from marden.seed.facilities_archive import DCMAP, Archive
from marden.seed.facilities_sink import PRODUCER, FactSink

NAME_LINKS = {"APLD_PF1B4": "apld_polaris_forge_1", "GLXY_H1": "galaxy_helios"}  # dictionary 3.5


def propose_links(conn: Conn, sink: FactSink, arc: Archive, existing_sites: set[str]) -> dict[str, Any]:
    pairs: list[tuple[str, str, str, bool]] = []  # (case, dcmap key, basis, by name)
    for row in arc.dcmap_rows:
        if row.get("cov") and row.get("case"):
            pairs.append((row["case"], row["key"], "dcmap.case", False))
    keys = {r["key"] for r in arc.dcmap_rows}
    for case, key in NAME_LINKS.items():
        if key in keys:
            pairs.append((case, key, "name match (dictionary 3.5)", True))
    stats: dict[str, Any] = {"proposed": 0, "reviews": 0, "skipped": []}
    src = arc.sources[DCMAP]
    for case, key, basis, by_name in pairs:
        slug = arc.site_slugs.get(case)
        cid = arc.campus_of_key[key]
        if slug not in existing_sites:
            stats["skipped"].append(f"{case} -> {key}: site '{slug}' has no row (run `seed sites` first)")
            continue
        evidence = {"site_slug": slug, "case": case, "campus_id": cid, "basis": basis}
        fid = sink.add("campus", cid, "site_link_evidence", evidence, src=src, rule=by_name)
        row = conn.execute(
            """INSERT INTO site_link (site_slug, campus_id, evidence_fact_id, status)
               VALUES (%s, %s, %s, 'proposed') ON CONFLICT (site_slug, campus_id) DO NOTHING
               RETURNING site_slug""",
            (slug, cid, fid),
        ).fetchone()
        stats["proposed"] += int(row is not None)
        review = conn.execute(
            """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
               VALUES ('link', 'site_link', %s, %s, %s, %s)
               ON CONFLICT (kind, ref_type, ref_id) DO NOTHING RETURNING id""",
            (f"{slug}|{cid}", f"Link financed site {slug} to campus {cid}",
             "site links are material: a person approves each one once (spec 17)",
             jsonb({**evidence, "evidence_fact_id": fid, "producer": PRODUCER})),
        ).fetchone()
        stats["reviews"] += int(review is not None)
    return stats
