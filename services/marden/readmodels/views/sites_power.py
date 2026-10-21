"""Site detail Power and Buildings tabs: `site_tab:power` (SitePowerTab of the app's site-tab types)
and `site_tab:buildings` (types.ts SiteBuildings plus `linkApproved`), specs 04 and 16.

Power: power needed (site gross / IT MW), gas supply in service (the pipeline plant's expected COD
as stated), grid connection (the site's large-load request), one track per plant serving the site
(status, planned in-service date as stated, its recorded changes), the dockets and pages the case
research watches (cadence of the Marden collector for that kind, when one exists), and the in-service
history from dated schedule changes and ready-for-service statements. 'Permitted on-site GW' is a
Marden count (M) left n/d until a permit-count model runs. The plant card embeds the `plant_row`
document (On-site power view) of the site's main plant when it has been built.

Buildings: the linked campuses' buildings with footprints (types.ts Building) and the main plant.
"""

from __future__ import annotations

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.sites_common import (
    Doc,
    day,
    fact_evidence,
    gw_value,
    nd,
    next_day,
    site_row,
    site_slugs,
    src_or_nd,
    text_of,
    with_value,
)
from marden.readmodels.views.sites_links import (
    building_doc,
    campus_buildings,
    linked_campuses,
    read_doc,
    site_plants,
)

PLANT_FACTS = ("status", "expected_cod", "original_cod", "latest_change", "technology", "technology_type",
               "capacity_mw", "stage")
PIPELINE_WORDS = ("pipeline", "lateral", "gas supply")
# Marden collectors per watched-source kind (source.id); kinds without one are not collected yet.
COLLECTOR_FOR_KIND = {"ferc_docket": "ferc", "sec_filings": "sec_index"}


def _cadence(minutes: int | None) -> str:
    if not minutes:
        return "on demand"
    if minutes < 60:
        return f"every {minutes} min"
    if minutes < 1440:
        return "hourly" if minutes == 60 else f"every {minutes // 60} h"
    if minutes < 10080:
        return "daily" if minutes == 1440 else f"every {minutes // 1440} days"
    return "weekly" if minutes == 10080 else "monthly"


def watched(conn: Conn, slug: str) -> list[Doc]:
    row = known_facts(conn, "site", slug, ["watch_sources"]).get("watch_sources")
    items = row["value"] if row and isinstance(row["value"], list) else []
    sources = {r["id"]: r for r in conn.execute("SELECT id, cadence_minutes, blocked FROM source")}
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source = sources.get(COLLECTOR_FOR_KIND.get(str(item.get("kind")), ""))
        if source is None:
            cadence = "not collected yet"
        else:
            cadence = "blocked" if source["blocked"] else _cadence(source["cadence_minutes"])
        what = " · ".join(str(item[k]) for k in ("id", "query", "why") if item.get(k))
        doc: Doc = {"agency": str(item.get("agency") or item.get("kind") or "n/d"), "what": what or "n/d",
                    "cadence": cadence}
        if item.get("url"):
            doc["url"] = str(item["url"])
        out.append(doc)
    return out


def _is_pipeline(plant: Row, facts: dict[str, Row]) -> bool:
    text = " ".join([plant["name"] or "", str((facts.get("technology_type") or {}).get("value") or "")]).lower()
    return any(w in text for w in PIPELINE_WORDS)


def plant_track(conn: Conn, plant: Row, facts: dict[str, Row]) -> Doc:
    changes = []
    if row := facts.get("original_cod"):
        changes.append({"date": row["valid_from"].isoformat(), "label": f"Originally planned {row['value']}",
                        "evidence": fact_evidence(row, f"Originally planned in service {row['value']}")})
    if row := facts.get("latest_change"):
        changes.append({"date": row["valid_from"].isoformat(), "label": text_of(row["value"], 160) or "Changed",
                        "evidence": fact_evidence(row)})
    first_event = conn.execute(
        """SELECT min(event_date) AS d FROM event WHERE subject_type = 'plant' AND subject_id = %s
             AND status = 'published' AND learned_at < %s::date AND event_date <= %s""",
        (plant["id"], next_day(), day()),
    ).fetchone()["d"]
    first = min([r["valid_from"] for r in facts.values()] + ([first_event] if first_event else []), default=None)
    status = facts.get("status")
    track: Doc = {
        "source": plant["name"] or plant["id"],
        "status": str(status["value"]) if status else "n/d",
        "from": first.isoformat() if first else "",
        "plannedInService": src_or_nd(facts.get("expected_cod"), "F"),
        "changes": sorted(changes, key=lambda c: c["date"]),
    }
    notes = [str(v["value"]) for k in ("technology",) if (v := facts.get(k))]
    if (cap := facts.get("capacity_mw")) and isinstance(cap["value"], int | float):
        notes.append(f"{cap['value']:,.0f} MW")
    if notes:
        track["note"] = " · ".join(notes)
    return track


def main_plant(plants: list[tuple[Row, dict[str, Row]]]) -> Row | None:
    """The site's main on-site plant: the largest generating plant (pipelines are fuel, not power)."""
    generating = [(p, f) for p, f in plants if not _is_pipeline(p, f)]
    if not generating:
        return None
    def size(pair: tuple[Row, dict[str, Row]]) -> float:
        value = (pair[1].get("capacity_mw") or {}).get("value")
        return value if isinstance(value, int | float) else -1.0
    return max(generating, key=size)[0]


def history(conn: Conn, slug: str) -> list[Doc]:
    rows = known_fact_rows(conn, "site", slug, ["schedule_change", "rfs_statement"])
    out = []
    for row in rows["schedule_change"] + rows["rfs_statement"]:
        v = row["value"] if isinstance(row["value"], dict) else {}
        when = v.get("date") if isinstance(v.get("date"), str) else row["valid_from"].isoformat()
        change = v.get("what") or v.get("text") or text_of(v)
        if not change:
            continue
        out.append({"recorded": when, "source": row.get("source_name") or "", "change": str(change),
                    "evidence": fact_evidence(row, str(change), when)})
    out.sort(key=lambda h: h["recorded"], reverse=True)
    return out


def grid_connection(conn: Conn, slug: str) -> Doc:
    rows = known_fact_rows(conn, "site", slug, ["large_load_request"])["large_load_request"]
    utility = known_facts(conn, "site", slug, ["utility", "power_utility"])
    if rows:
        row = rows[0]
        v = row["value"] if isinstance(row["value"], dict) else {}
        parts = [str(v[k]) for k in ("utility", "name") if v.get(k)]
        if isinstance(v.get("mw"), int | float):
            parts.append(f"{v['mw']:,.0f} MW")
        if v.get("stage"):
            parts.append(f"({v['stage']})")
        kpi: Doc = {"value": with_value(row, " ".join(parts) or text_of(v))}
    else:
        kpi = {"value": nd("F")}
    if note := utility.get("utility") or utility.get("power_utility"):
        kpi["note"] = f"Utility: {note['value']}"
    return kpi


def power_tab(conn: Conn, slug: str) -> Doc | None:
    if site_row(conn, slug) is None:
        return None
    facts = known_facts(conn, "site", slug, ["gross_mw", "critical_it_mw"])
    campuses, _ = linked_campuses(conn, slug)
    plants = [(p, known_facts(conn, "plant", p["id"], PLANT_FACTS)) for p in site_plants(conn, slug, campuses)]
    pipelines = [f for p, f in plants if _is_pipeline(p, f) and f.get("expected_cod")]
    power_needed: Doc = {"value": gw_value(facts)}
    if (it := facts.get("critical_it_mw")) and isinstance(it["value"], int | float):
        power_needed["note"] = f"Critical IT {it['value']:,.0f} MW"
    gas: Doc = {"value": src_or_nd(pipelines[0]["expected_cod"] if pipelines else None, "F")}
    main = main_plant(plants)
    return {
        "powerNeeded": power_needed,
        "permittedOnsiteGw": {"value": nd("M")},
        "gasInService": gas,
        "gridConnection": grid_connection(conn, slug),
        "sources": [plant_track(conn, p, f) for p, f in plants],
        "watched": watched(conn, slug),
        "plant": read_doc(conn, "plant_row", main["id"]) if main else None,
        "history": history(conn, slug),
    }


@builder("site_tab:power", keys=site_slugs)
def build_power(conn: Conn, slug: str) -> Doc | None:
    return power_tab(conn, slug)


def buildings_tab(conn: Conn, slug: str) -> Doc | None:
    if site_row(conn, slug) is None:
        return None
    campuses, approved = linked_campuses(conn, slug)
    buildings = [d for b in campus_buildings(conn, campuses) if (d := building_doc(conn, b)) is not None]
    doc: Doc = {"campuses": [{"id": c["id"], "name": c["name"]} for c in campuses], "buildings": buildings}
    plants = [(p, known_facts(conn, "plant", p["id"], PLANT_FACTS)) for p in site_plants(conn, slug, campuses)]
    main = main_plant(plants)
    if main and (plant := read_doc(conn, "plant_row", main["id"])) is not None:
        doc["plant"] = plant
    if campuses:
        doc["linkApproved"] = approved
    return doc


@builder("site_tab:buildings", keys=site_slugs)
def build_buildings(conn: Conn, slug: str) -> Doc | None:
    return buildings_tab(conn, slug)
