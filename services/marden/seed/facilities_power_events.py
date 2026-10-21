"""power.json events: project evidence (``ev``), docket latest actions (``dk``), the filing feed
(``feed``) and each docket's last filings (``dockets[].lf``).

Subjects: the plant for plant-type projects, else the case-linked campus and financed site of the
project (``PowerLinks``). Feed items and docket filings reach a project through its id (``p``) or
a docket number one of its ``dk`` refs carries; docket filings also reach the sites of the
inventory cases watching the same proceeding (``AGENCY_number``). Anything that reaches no
subject is skipped and counted. Queue withdrawals, cancellations and drops are status changes
(material); other items use the title rule in ``facilities_sink.material_text``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from marden.seed.common import tag_for_url
from marden.seed.facilities_archive import POWER, Archive, docket_key
from marden.seed.facilities_plants import PowerLinks
from marden.seed.facilities_sink import EventSink, clean, material_text

QUEUE_KINDS = {"pjm_queue_recent_withdrawal": "queue withdrawal", "ercot_gis_cancellation": "queue cancellation",
               "spp_queue_dropped": "queue drop", "spp_queue_status_change": None}


def _kind(url: str | None) -> str:
    return "docket" if tag_for_url(url) == "F" else "news"


def _by_docket(arc: Archive) -> dict[str, set[str]]:
    """Docket number -> the project ids whose `dk` refs carry it."""
    out: dict[str, set[str]] = defaultdict(set)
    for pid, p in arc.projects.items():
        for d in p.get("dk") or []:
            if d.get("no"):
                out[d["no"].strip()].add(pid)
    return out


def _subjects(arc: Archive, links: PowerLinks, pids: set[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for pid in sorted(pids):
        for s in links.subjects(pid, arc):
            if s not in out:
                out.append(s)
    return out


def _case_sites(arc: Archive, existing_sites: set[str], agency: str | None,
                number: str | None) -> list[tuple[str, str]]:
    if not (agency and number):
        return []
    rows = arc.inventory_by_key.get(docket_key(agency, number.strip()), [])
    return [("site", s) for r in rows if (s := arc.site_slugs.get(r["case_id"])) in existing_sites]


def new_stats() -> dict[str, Any]:
    return {"ev": 0, "dk": 0, "feed": 0, "lf": 0, "skipped": defaultdict(int)}


def write_project_events(events: EventSink, arc: Archive, links: PowerLinks, pids: list[str],
                         stats: dict[str, Any]) -> None:
    """Each project's evidence (`ev`) and dated docket actions (`dk`) on its subjects."""
    src = arc.sources[POWER]
    for pid in pids:
        p = arc.projects[pid]
        subs = links.subjects(pid, arc)
        if not subs:
            n = len(p.get("ev") or []) + sum(1 for d in p.get("dk") or [] if d.get("ld"))
            stats["skipped"]["project not linked to a plant, campus or site"] += n
            continue
        for e in p.get("ev") or []:
            title = e.get("q") or e.get("w")  # headline, else who / what (e.g. 'Coweta Planning Commission: Rezoning')
            for st, sid in subs:
                events.add(st, sid, e.get("d"), _kind(e.get("u")), title, src=src, url=e.get("u"),
                           material_rule=material_text(title), source="power.ev")
            stats["ev"] += 1
        for d in p.get("dk") or []:
            if not d.get("ld"):
                continue  # no dated action: kept in the plant's `dockets` fact only
            title = f"{d.get('a')} {d.get('no') or ''}: {clean(d.get('la')) or clean(d.get('t'))}".replace(" :", ":")
            for st, sid in subs:
                events.add(st, sid, d["ld"], "docket", title, src=src, url=d.get("u"),
                           material_rule=material_text(d.get("la")), source="power.dk")
            stats["dk"] += 1


def write_feed_events(events: EventSink, arc: Archive, links: PowerLinks, existing_sites: set[str],
                      feed: list[dict[str, Any]], stats: dict[str, Any]) -> None:
    """power.json feed items on the project (`p`), the projects carrying the docket, or the sites
    of the inventory cases watching it."""
    src, by_docket = arc.sources[POWER], _by_docket(arc)
    for f in feed:
        pids = {f["p"]} if f.get("p") else set(by_docket.get((f.get("no") or "").strip(), set()))
        subs = _subjects(arc, links, pids) or _case_sites(arc, existing_sites, f.get("a"), f.get("no"))
        if not subs:
            stats["skipped"][f"feed {f.get('k')}: no project, docket or case link"] += 1
            continue
        k = f.get("k") or ""
        kind = "queue" if k in QUEUE_KINDS else "docket"
        rule = QUEUE_KINDS.get(k) or material_text(f.get("t"))
        for st, sid in subs:
            events.add(st, sid, f.get("d"), kind, f.get("t"), src=src, url=f.get("u"), material_rule=rule,
                       source="power.feed")
        stats["feed"] += 1


def write_docket_filings(events: EventSink, arc: Archive, links: PowerLinks, existing_sites: set[str],
                         stats: dict[str, Any]) -> None:
    """Each power.json docket's last filings (`lf`) on the subjects linked to that docket."""
    src, by_docket = arc.sources[POWER], _by_docket(arc)
    for d in arc.data[POWER].get("dockets") or []:
        no = (d.get("no") or "").strip()
        subs = list(dict.fromkeys(_subjects(arc, links, set(by_docket.get(no, set())))
                                  + _case_sites(arc, existing_sites, d.get("a"), no)))
        if not subs:
            stats["skipped"]["dockets.lf: docket not linked to a plant, campus or site"] += len(d.get("lf") or [])
            continue
        for item in d.get("lf") or []:
            for st, sid in subs:
                events.add(st, sid, item.get("d"), "docket", item.get("t"), src=src, url=item.get("u"),
                           material_rule=material_text(item.get("t")), source="power.dockets.lf")
            stats["lf"] += 1
