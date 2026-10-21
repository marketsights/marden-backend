"""Docket entities, docket items (dockets/out) and case news (aiwc/state/events.json) as events.

Docket items: one ``docket`` entity per proceeding (``Docket <AGENCY> <number>``), facts on it from
the inventory, and each item of ``dockets/out/<key>.json`` as an event (kind 'docket') on the
financed site of every inventory case that watches the proceeding. Case news: the verified
superset ``aiwc/state/events.json`` (601) on the case's site. Tags follow the item URL host.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn
from marden.seed.common import ensure_entity, host_of, source_name_for_url
from marden.seed.facilities_archive import EVENTS, INVENTORY, POWER, Archive, docket_key
from marden.seed.facilities_sink import SEVERITY, EventSink, FactSink, clean, is_nd, material_text

GENERIC_PORTALS = ("civicclerk.com", "granicus.com", "legistar.com", "municode.com", "boarddocs.com",
                   "salinetownship.org")
OPPOSITION = {"Court case", "Moratorium", "Air permit contested"}
STATE_KIND = {"news": "news", "court": "news", "county_board": "docket", "city_council": "docket",
              "zoning": "docket", "ferc": "docket", "ferc_filing": "docket", "ferc_issuance": "docket",
              "state_env_permit": "permit"}


def docket_entity(conn: Conn, key: str, aliases: list[str]) -> str:
    return ensure_entity(conn, f"Docket {key.replace('_', ' ')}", "docket",
                         aliases=[a for a in aliases if a], meta={"proceeding_key": key})


def _body(proceeding: str | None) -> str | None:
    """'City of Abilene City Council (CivicClerk)' -> 'City of Abilene City Council'."""
    text = clean(proceeding)
    return text.split(" (")[0] if text else None


def write_dockets(conn: Conn, facts: FactSink, events: EventSink, arc: Archive,
                  existing_sites: set[str]) -> dict[str, Any]:
    stats: dict[str, Any] = {"docket_entities": 0, "items": 0, "skipped": []}
    inv_src = arc.sources[INVENTORY]
    for key, rows in sorted(arc.inventory_by_key.items()):
        lead = rows[0]
        eid = docket_entity(conn, key, [lead.get("proceeding"), key])
        stats["docket_entities"] += 1
        sites = sorted({s for r in rows if (s := arc.site_slugs.get(r["case_id"])) in existing_sites})
        url = lead.get("list_url")
        meta = arc.docket_file(key).get("meta") or {}
        status = meta.get("project_status") or meta.get("status") if isinstance(meta, dict) else None
        if status == "true":
            status = None  # NMED post flag, not a proceeding status
        add = facts.add
        add("entity", eid, "proceeding", clean(lead.get("proceeding")), src=inv_src, url=url)
        add("entity", eid, "proceeding_kind", lead.get("kind"), src=inv_src, url=url)
        add("entity", eid, "list_url", url, src=inv_src, url=url)
        add("entity", eid, "linked_sites", sites, src=inv_src, url=url)
        add("entity", eid, "status", status, src=arc.src(f"dockets/out/{key}.json"), url=url)
        add("entity", eid, "next_dates", [d for r in rows for d in r.get("next_dates") or []] or None,
            src=inv_src, url=url)
        add("entity", eid, "flags", [d for r in rows for d in r.get("flags_30d") or []] or None,
            src=inv_src, url=url)
        if not sites:
            stats["skipped"].append(f"{key}: no financed site row for cases {[r['case_id'] for r in rows]}")
            continue
        adverse = {(f.get("d"), f.get("u")) for r in rows for f in r.get("flags_30d") or []
                   if f.get("sev") == "adverse"}
        body = _body(lead.get("proceeding"))
        src = arc.src(f"dockets/out/{key}.json")
        for item in arc.docket_file(key).get("items") or []:
            u = item.get("u")
            name = body if host_of(u).endswith(GENERIC_PORTALS) and body else source_name_for_url(u)
            rule = "adverse flag" if (item.get("d"), u) in adverse else material_text(item.get("t"))
            for slug in sites:
                events.add("site", slug, item.get("d"), "docket", item.get("t"), src=src, url=u,
                           material_rule=rule, source_name=name, source=f"dockets/out/{key}")
            stats["items"] += 1
    for d in arc.data[POWER].get("dockets") or []:
        key = docket_key(d["a"], d["no"]) if d.get("no") else f"{d['a']}_{clean(d.get('t')) or ''}"[:120]
        eid = docket_entity(conn, key, [clean(d.get("t")) or "", f"{d['a']}|{d.get('no') or ''}"])
        stats["docket_entities"] += 1
        psrc, url = arc.sources[POWER], d.get("u")
        facts.add("entity", eid, "proceeding", clean(d.get("t")), src=psrc, url=url)
        facts.add("entity", eid, "agency", d.get("a"), src=psrc, url=url)
        facts.add("entity", eid, "list_url", url, src=psrc, url=url)
        facts.add("entity", eid, "status", clean(d.get("st")), src=psrc, url=url)
        facts.add("entity", eid, "next_step", clean(d.get("nx")), src=psrc, url=url)
        facts.add("entity", eid, "why_it_matters", clean(d.get("why")), src=psrc, url=url, judged=True)
    return stats


def write_state_events(events: EventSink, arc: Archive, existing_sites: set[str]) -> dict[str, Any]:
    """aiwc/state/events.json on the case's site. Material: sev >= 2 with a rule label (cls)."""
    stats: dict[str, Any] = {"events": 0, "skipped": []}
    src = arc.sources[EVENTS]
    for e in arc.data[EVENTS]:
        slug = arc.site_slugs.get(e.get("case"))
        if slug not in existing_sites:
            stats["skipped"].append(f"events.json {e.get('case')}: no financed site row")
            continue
        cls = e.get("cls")
        kind = "opposition" if cls in OPPOSITION else STATE_KIND.get(e.get("kind"), "news")
        sev = e.get("sev") if isinstance(e.get("sev"), int) else 0
        rule = f"sev {sev}: {cls}" if sev >= 2 and not is_nd(cls) else None
        events.add("site", slug, e.get("d"), kind, e.get("t"), src=arc.src(EVENTS, e.get("d")), url=e.get("u"),
                   severity=SEVERITY.get(sev, "none"), material_rule=rule, source=EVENTS)
        stats["events"] += 1
    return stats
