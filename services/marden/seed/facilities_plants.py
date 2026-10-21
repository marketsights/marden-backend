"""Power plants (power.json projects of plant types), their facts, operators and plant_serves.

Large-load interconnection requests, transmission and solar+storage projects are not plants: they
become ``large_load_request`` / ``grid_project`` facts on the campus (covered dcmap ``pw``) and the
financed site (``aiwcredit.pw2c``) they are linked to. Fuzzy ``pw`` links on Epoch-only dcmap rows
are never used.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from marden.core.db import Conn
from marden.seed.common import ensure_entity, slugify, tag_for_url
from marden.seed.facilities_archive import AIWCREDIT, DCMAP, POWER, Archive
from marden.seed.facilities_sink import FactSink, clean, confidence, first_url, is_nd, num, tags_weakest

PLANT_TYPES = frozenset({"gas plant", "fuel cell", "SMR", "co-location", "data center campus power", "nuclear restart"})
LARGE_LOAD = "large-load interconnection"
PLANT_STATUS = {"under construction": "Under construction", "permitting": "Permit pending",
                "queued": "Permit pending", "proposed": "Permit pending", "approved": "Permitted",
                "operating": "Operating", "withdrawn": "Withdrawn", "denied": "Denied"}  # stalled: none
ROLES = {"C": "debt_vehicle", "B": "developer_builder", "T": "tenant_payer", "U": "utility",
         "S": "power_supplier", "K": "unknown"}
_QUEUE = re.compile(r"\b(\d{2}IN[RF]\d{4}|C0\d-\d{3,4}|GEN-\d{4}-\d{3}|AE\d-\d{3}|J\d{4})\b")
_TECH = (
    (re.compile(r"combined[- ]cycle|\bCCGTs?\b|\bNGCC\b", re.I), "Combined cycle"),
    (re.compile(r"fuel[- ]cells?|\bBloom\b|\bSOFC\b", re.I), "Fuel cell"),
    (re.compile(r"reciprocating|\bengines?\b|gensets?|Jenbacher|W[aä]rtsil[aä]", re.I), "Reciprocating engine"),
    (re.compile(r"simple[- ]cycle|aeroderivative|\bturbines?\b|peaker|\bCTs?\b", re.I), "Simple-cycle turbine"),
)


def plant_id(project_id: str) -> str:
    return slugify(project_id)


def bad_mw(project: dict[str, Any]) -> str | None:
    """Why a power.json MW value is not a plant capacity (dictionary 3.4), or None."""
    mw = num(project.get("mw"))
    if mw is None:
        return None
    if mw == 1.0:
        return "mw 1.0 is a GW figure written as MW"
    if float(mw).is_integer() and 2000 <= mw <= 2100 and str(int(mw)) in (project.get("n") or ""):
        return f"mw {int(mw)} is the year in the project name"
    return None


def technology(project: dict[str, Any]) -> str:
    """types.ts PlantTech by rule from the project type and its own text."""
    if project["ty"] == "fuel cell":
        return "Fuel cell"
    text = " ".join(str(project.get(k) or "") for k in ("n", "rw", "why", "dc"))
    if project["ty"] in ("gas plant", "data center campus power", "co-location"):
        for pattern, tech in _TECH:
            if pattern.search(text):
                return tech
    return "Other"


@dataclass
class PowerLinks:
    """Case-linked relations between power projects, campuses and financed sites."""

    campuses: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))  # project -> campus ids
    sites: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))  # project -> site slugs
    skipped: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, arc: Archive, existing_sites: set[str]) -> PowerLinks:
        links = cls()
        for row in arc.dcmap_rows:
            if not (row.get("cov") and row.get("case")):
                continue  # only case-linked covered rows (never the fuzzy Epoch-only pw lists)
            cid = arc.campus_of_key[row["key"]]
            for p in row.get("pw") or []:
                pid = p.get("id")
                if pid not in arc.projects:
                    links.skipped.append(f"{pid}: in dcmap.pw of {row['key']} but not in power.json")
                    continue
                other = arc.pw2c.get(pid)
                if other and other != row["case"] and arc.projects[pid]["ty"] in PLANT_TYPES:
                    links.skipped.append(f"{pid}: pw2c case {other} differs from campus {row['key']} case")
                    continue
                if cid not in links.campuses[pid]:
                    links.campuses[pid].append(cid)
        for pid, case in arc.pw2c.items():
            slug = arc.site_slugs.get(case)
            if pid in arc.projects and slug in existing_sites and slug not in links.sites[pid]:
                links.sites[pid].append(slug)
        return links

    def subjects(self, pid: str, arc: Archive) -> list[tuple[str, str]]:
        """Event / fact subjects for a project: the plant itself, else its campuses and sites."""
        p = arc.projects.get(pid)
        if p is None:
            return []
        if p["ty"] in PLANT_TYPES:
            return [("plant", plant_id(pid))]
        return [("campus", c) for c in self.campuses.get(pid, [])] + [("site", s) for s in self.sites.get(pid, [])]


def _operator_entity(conn: Conn, arc: Archive, project: dict[str, Any]) -> str | None:
    dev = clean(project.get("dev"))
    if not dev:
        return None
    name = re.sub(r"\s*\([^)]*\)", "", dev).strip() or dev
    gf = arc.data[POWER].get("gf") or {}
    gx = arc.data[POWER].get("gx") or {}
    funcs = {gx[k][0]: v for k, v in gf.items() if k in gx}
    kind = "utility" if funcs.get(name) == "u" or name == clean(project.get("ut")) else "power_provider"
    return ensure_entity(conn, name, kind, aliases=[dev] if dev != name else ())


def write_plants(conn: Conn, sink: FactSink, arc: Archive, links: PowerLinks) -> dict[str, Any]:
    plants, other = 0, defaultdict(int)
    unlinked: list[str] = []
    for pid, p in arc.projects.items():
        if p["ty"] in PLANT_TYPES:
            _plant(conn, sink, arc, pid, p)
            plants += 1
            continue
        subjects = links.subjects(pid, arc)
        if not subjects:
            unlinked.append(pid)
            continue
        other[p["ty"]] += 1
        field_name = "large_load_request" if p["ty"] == LARGE_LOAD else "grid_project"
        value = {k: v for k, v in {
            "project_id": pid, "name": clean(p.get("n")), "type": p["ty"], "developer": clean(p.get("dev")),
            "utility": clean(p.get("ut")), "iso": p.get("iso"), "state": p.get("st"),
            "mw": None if bad_mw(p) else num(p.get("mw")), "stage": p.get("stage"),
            "expected_cod": clean(p.get("cod")), "queue_ids": _QUEUE.findall(p.get("n") or "") or None,
            "latest_change": clean(p.get("why")), "serves_text": clean(p.get("dc")),
        }.items() if not is_nd(v)}
        for st, sid in subjects:
            sink.add(st, sid, field_name, value, src=arc.sources[POWER], url=first_url(p.get("src")),
                     confidence=confidence(p.get("conf")))
    return {"plants": plants, "non_plant_linked": dict(other), "non_plant_unlinked": unlinked}


def _plant(conn: Conn, sink: FactSink, arc: Archive, pid: str, p: dict[str, Any]) -> None:
    ppid = plant_id(pid)
    conn.execute(
        """INSERT INTO power_plant (id, name, state) VALUES (%s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, state = EXCLUDED.state""",
        (ppid, clean(p.get("n")) or pid, p.get("st")),
    )
    src, url, conf = arc.sources[POWER], first_url(p.get("src")), confidence(p.get("conf"))

    def add(field_name: str, value: Any, **kw: Any) -> int | None:
        kw.setdefault("url", url)
        return sink.add("plant", ppid, field_name, value, src=src, confidence=conf, **kw)

    add("legacy_id", pid, url=None)
    add("technology_type", p.get("ty"))
    add("technology", technology(p), rule=True)
    add("operator", clean(p.get("dev")))
    add("operator_entity_id", _operator_entity(conn, arc, p), rule=True)
    add("utility", clean(p.get("ut")))
    add("iso", p.get("iso"))
    if bad_mw(p):
        sink.counts[("skipped", f"plant.capacity_mw {pid}: {bad_mw(p)}")] += 1
    else:
        derived = "derived" in (p.get("rw") or "").lower()
        add("capacity_mw", num(p.get("mw")), unit="MW", judged=derived)
    add("stage", p.get("stage"))
    add("status", PLANT_STATUS.get(p.get("stage") or ""), rule=True)
    add("serves_text", clean(p.get("dc")))
    add("debt_text", clean(p.get("debt")))
    add("expected_cod", clean(p.get("cod")))
    add("original_cod", clean(p.get("cod0")))
    add("latest_change", clean(p.get("why")))
    add("risk_level", p.get("risk"), judged=True)
    add("risk_note", clean(p.get("rw")), judged=True)
    add("queue_ids", _QUEUE.findall(p.get("n") or "") or None)
    parties = [{"kind": x[0], "key": x[1], "role": ROLES.get(x[2], x[2]), "role_code": x[2],
                "name": ((arc.data[POWER].get("gx") or {}).get(x[1]) or [None])[0] if x[0] == "G"
                else x[1].split("|")[0], "note": x[3] if len(x) > 3 and x[3] else None}
               for x in p.get("x") or [] if len(x) >= 3]
    add("parties", parties, rule=True)
    dk = [{k: v for k, v in {"agency": d.get("a"), "number": d.get("no"), "title": clean(d.get("t")),
                              "latest_action": clean(d.get("la")), "latest_date": d.get("ld") or None,
                              "url": d.get("u")}.items() if v} for d in p.get("dk") or []]
    if dk:
        tag = tags_weakest(tag_for_url(d.get("url"), default="R") if d.get("url") else "R" for d in dk)
        add("dockets", dk, url=next((d["url"] for d in dk if d.get("url")), None), tag=tag)


def write_serves(conn: Conn, sink: FactSink, arc: Archive, links: PowerLinks) -> dict[str, Any]:
    """plant_serves from case-linked dcmap rows (checked against pw2c); pw2c site links as facts."""
    rows = 0
    for pid, campuses in sorted(links.campuses.items()):
        p = arc.projects[pid]
        if p["ty"] not in PLANT_TYPES:
            continue
        for cid in campuses:
            case = next(r["case"] for r in arc.dcmap_rows if arc.campus_of_key[r["key"]] == cid)
            fid = sink.add("plant", plant_id(pid), "serves_campus",
                           {"campus_id": cid, "case": case, "basis": "dcmap.pw (covered, case-linked)",
                            "pw2c_case": arc.pw2c.get(pid)}, src=arc.sources[DCMAP])
            conn.execute(
                """INSERT INTO plant_serves (plant_id, campus_id, evidence_fact_id) VALUES (%s, %s, %s)
                   ON CONFLICT (plant_id, campus_id) DO NOTHING""",
                (plant_id(pid), cid, fid),
            )
            rows += 1
    only_sites = []
    for pid, slugs in sorted(links.sites.items()):
        if arc.projects[pid]["ty"] not in PLANT_TYPES:
            continue
        sink.add("plant", plant_id(pid), "serves_sites", slugs, src=arc.sources[AIWCREDIT])
        if not links.campuses.get(pid):
            only_sites.append(f"{pid}: pw2c links site(s) {slugs}; no case-linked campus lists it")
    return {"plant_serves": rows, "plants_with_site_only": only_sites}
