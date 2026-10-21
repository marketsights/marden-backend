"""Docket entities, docket items (dockets/out) and case news (aiwc/state/events.json) as events.

Docket items: one ``docket`` entity per proceeding (``Docket <AGENCY> <number>``), facts on it from
the inventory, and each item of ``dockets/out/<key>.json`` as an event (kind 'docket') on the
financed site of every inventory case that watches the proceeding. Case news: the verified
superset ``aiwc/state/events.json`` (601) on the case's site. Tags follow the item URL host.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn
from marden.seed.common import SOURCE_NAMES, ensure_entity, host_of, parse_date, source_name_for_url
from marden.seed.facilities_archive import EVENTS, INVENTORY, POWER, Archive, docket_key
from marden.seed.facilities_sink import ARCHIVE_CUTOFF, SEVERITY, EventSink, FactSink, clean, is_nd, material_text

OPPOSITION = {"Court case", "Moratorium", "Air permit contested"}
# Meeting calendars list meetings ahead of time; every other docket host lists documents already filed.
MEETING_HOSTS = ("civicclerk.com", "granicus.com", "legistar.com", "boarddocs.com", "municode.com")
DATED_AFTER_COLLECTION = "filed document dated after the archive was collected (date likely mistyped in the source)"


def dated_after_collection(day: Any, url: Any) -> bool:
    """A filed document cannot carry a date after the archive was collected: such a date is a typo in the
    source (NMED's AQB 26-57 position statement is dated 2027-07-22). It is not corrected here; the item waits
    for a person. Scheduled meetings on agenda calendars are dated ahead and are not affected."""
    d = parse_date(day)
    host = host_of(url) if isinstance(url, str) else ""
    if d is None or d <= ARCHIVE_CUTOFF:
        return False
    return not any(host == h or host.endswith("." + h) for h in MEETING_HOSTS)
STATE_KIND = {"news": "news", "court": "news", "county_board": "docket", "city_council": "docket",
              "zoning": "docket", "ferc": "docket", "ferc_filing": "docket", "ferc_issuance": "docket",
              "state_env_permit": "permit"}


def docket_entity(conn: Conn, key: str, aliases: list[str]) -> str:
    return ensure_entity(conn, f"Docket {key.replace('_', ' ')}", "docket",
                         aliases=[a for a in aliases if a], meta={"proceeding_key": key})


def _known_host(url: str | None) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in SOURCE_NAMES)


def _body(proceeding: str | None) -> str | None:
    """'City of Abilene City Council (CivicClerk)' -> 'City of Abilene City Council'."""
    text = clean(proceeding)
    return text.split(" (")[0] if text else None


def write_docket(conn: Conn, facts: FactSink, arc: Archive, key: str, existing_sites: set[str],
                 stats: dict[str, Any]) -> list[str]:
    """One inventory proceeding: its docket entity and entity facts. Returns the financed sites
    its items go to (``write_docket_items``)."""
    rows = arc.inventory_by_key[key]
    lead = rows[0]
    inv_src = arc.sources[INVENTORY]
    eid = docket_entity(conn, key, [lead.get("proceeding"), key])
    stats["docket_entities"] += 1
    sites = sorted({s for r in rows if (s := arc.site_slugs.get(r["case_id"])) in existing_sites})
    url = lead.get("list_url")
    meta = arc.docket_file(key).get("meta") or {}
    status = meta.get("project_status") or meta.get("status")
    add = facts.add
    add("entity", eid, "proceeding", clean(lead.get("proceeding")), src=inv_src, url=url)
    add("entity", eid, "proceeding_kind", lead.get("kind"), src=inv_src, url=url)
    add("entity", eid, "list_url", url, src=inv_src, url=url)
    add("entity", eid, "linked_sites", sites, src=inv_src, url=url)
    if status != "true":  # NMED's post flag is not a proceeding status
        add("entity", eid, "status", status, src=arc.src(f"dockets/out/{key}.json"), url=url)
    add("entity", eid, "next_dates", [d for r in rows for d in r.get("next_dates") or []] or None,
        src=inv_src, url=url)
    add("entity", eid, "flags", [d for r in rows for d in r.get("flags_30d") or []] or None, src=inv_src, url=url)
    if not sites:
        stats["skipped"].append(f"{key}: no financed site row for cases {[r['case_id'] for r in rows]}")
    return sites


def write_docket_items(events: EventSink, arc: Archive, key: str, items: list[dict[str, Any]],
                       sites: list[str], stats: dict[str, Any]) -> None:
    """Items of dockets/out/<key>.json as events (kind 'docket') on each watching site."""
    rows = arc.inventory_by_key[key]
    adverse = {(f.get("d"), f.get("u")) for r in rows for f in r.get("flags_30d") or [] if f.get("sev") == "adverse"}
    body = _body(rows[0].get("proceeding"))
    src = arc.src(f"dockets/out/{key}.json")
    for item in items:
        u = item.get("u")
        # the agency's own name when the host is a generic portal (CivicClerk, Granicus, county sites)
        name = source_name_for_url(u) if _known_host(u) or not body else body
        rule = "adverse flag" if (item.get("d"), u) in adverse else material_text(item.get("t"))
        if dated_after_collection(item.get("d"), u):
            rule = DATED_AFTER_COLLECTION
            stats["skipped"].append(f"{key}: filed item dated {item.get('d')}, after the archive: left as a candidate")
        for slug in sites:
            events.add("site", slug, item.get("d"), "docket", item.get("t"), src=src, url=u,
                       material_rule=rule, source_name=name, source=f"dockets/out/{key}")
        stats["items"] += 1


def write_power_dockets(conn: Conn, facts: FactSink, arc: Archive, dockets: list[dict[str, Any]],
                        stats: dict[str, Any]) -> None:
    """power.json's key proceedings as docket entities (same ids as the inventory's)."""
    for d in dockets:
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


def write_state_events(events: EventSink, arc: Archive, existing_sites: set[str], rows: list[dict[str, Any]],
                       stats: dict[str, Any]) -> None:
    """aiwc/state/events.json rows on the case's site. Material: sev >= 2 with a rule label (cls)."""
    for e in rows:
        if not arc.in_scope(e.get("case")):
            continue
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
