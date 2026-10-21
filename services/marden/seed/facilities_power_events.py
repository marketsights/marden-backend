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


def write_power_events(events: EventSink, arc: Archive, links: PowerLinks,
                       existing_sites: set[str]) -> dict[str, Any]:
    src = arc.sources[POWER]
    power = arc.data[POWER]
    by_docket: dict[str, set[str]] = defaultdict(set)  # docket number -> project ids carrying it
    for pid, p in arc.projects.items():
        for d in p.get("dk") or []:
            if d.get("no"):
                by_docket[d["no"].strip()].add(pid)
    stats: dict[str, Any] = {"ev": 0, "dk": 0, "feed": 0, "lf": 0, "skipped": defaultdict(int)}

    def subjects(pids: set[str]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for pid in sorted(pids):
            for s in links.subjects(pid, arc):
                if s not in out:
                    out.append(s)
        return out

    def case_sites(agency: str | None, number: str | None) -> list[tuple[str, str]]:
        if not (agency and number):
            return []
        rows = arc.inventory_by_key.get(docket_key(agency, number), [])
        return [("site", s) for r in rows if (s := arc.site_slugs.get(r["case_id"])) in existing_sites]

    for pid, p in sorted(arc.projects.items()):
        subs = links.subjects(pid, arc)
        if not subs:
            n = len(p.get("ev") or []) + sum(1 for d in p.get("dk") or [] if d.get("ld"))
            stats["skipped"]["project not linked to a plant, campus or site"] += n
            continue
        for e in p.get("ev") or []:
            for st, sid in subs:
                events.add(st, sid, e.get("d"), _kind(e.get("u")), e.get("q"), src=src, url=e.get("u"),
                           material_rule=material_text(e.get("q")), source="power.ev")
            stats["ev"] += 1
        for d in p.get("dk") or []:
            if not d.get("ld"):
                continue  # no dated action: kept in the plant's `dockets` fact only
            title = f"{d.get('a')} {d.get('no') or ''}: {clean(d.get('la')) or clean(d.get('t'))}".replace(" :", ":")
            for st, sid in subs:
                events.add(st, sid, d["ld"], "docket", title, src=src, url=d.get("u"),
                           material_rule=material_text(d.get("la")), source="power.dk")
            stats["dk"] += 1
    for f in power.get("feed") or []:
        pids = {f["p"]} if f.get("p") else set(by_docket.get((f.get("no") or "").strip(), set()))
        subs = subjects(pids) or case_sites(f.get("a"), f.get("no"))
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
    for d in power.get("dockets") or []:
        no = (d.get("no") or "").strip()
        subs = subjects(set(by_docket.get(no, set()))) + [s for s in case_sites(d.get("a"), no)]
        subs = list(dict.fromkeys(subs))
        if not subs:
            stats["skipped"]["dockets.lf: docket not linked to a plant, campus or site"] += len(d.get("lf") or [])
            continue
        for item in d.get("lf") or []:
            for st, sid in subs:
                events.add(st, sid, item.get("d"), "docket", item.get("t"), src=src, url=item.get("u"),
                           material_rule=material_text(item.get("t")), source="power.dockets.lf")
            stats["lf"] += 1
    stats["skipped"] = dict(stats["skipped"])
    return stats
