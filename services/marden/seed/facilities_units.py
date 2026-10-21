"""Generating units and air permits for the plants, read from the archive's own text.

The archive has no unit table (dictionary 3.4): OEM, model, count and rating appear only in prose.
A ``power_unit`` row is written only when the text states a unit count together with an OEM, a
model or a rating ("250 INNIO Jenbacher J620 (3.22 MW)", "813 Enchanted Rock 450 kW gensets",
"3 x 754 MW"); the matched passage is kept as the fact quote. An OEM/model named without a count
becomes a plant ``equipment_mentions`` fact only. Sources, most specific first: gpu/site_power
(per case, with source URLs), the covered dcmap row's power detail, the project's own text; a
plant takes its units from the first source that yields any, so one fleet is never counted twice.
Text that fits several plants of a case (and no MW total singles one out) is skipped.

Air permits: dockets inventory rows of kind ``state_air_permit``; numbers, type and applicant are
parsed from the proceeding name, the status from the proceeding file. NSR/PSD permits attach to
the case's on-site plant when exactly one fits; every permit is also a site fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from marden.core.db import Conn
from marden.seed.common import slugify
from marden.seed.facilities_archive import DCMAP, INVENTORY, POWER, SITE_POWER, Archive
from marden.seed.facilities_plants import PLANT_TYPES, PowerLinks, plant_id
from marden.seed.facilities_sink import FactSink, Src, clean, first_url, num

ONSITE_TYPES = frozenset({"data center campus power", "gas plant", "fuel cell"})
_OEMS = ("GE Vernova|Siemens Energy|Mitsubishi Power|Solar Turbines|INNIO Jenbacher|Enchanted Rock|Bloom Energy|"
         "Baker Hughes|Rolls-Royce|Caterpillar|W[äa]rtsil[äa]|Jenbacher|Siemens|Mitsubishi|Cummins|Kohler|"
         "Generac|Bloom|Solar(?= Titan)|Cat(?= [A-Z]?\\d)|GE(?= \\d|\\s[A-Z]{1,3}\\d)|MTU")
_MODEL = (r"(?:Titan\s?\d{2,3}|Taurus\s?\d{2}|Mars\s?\d{3}|F-class(?:\s+CTs?)?|SOFC|"
          r"\d{1,2}[A-Z]{1,3}(?:/\d{1,2}[A-Z]{1,3})?|[A-Z]{1,6}-?\d{1,4}[A-Za-z0-9]*(?:/[A-Z]{0,3}\d{1,4}[A-Za-z]*)?)")
_RATING = r"(?:\s*\((?P<r1>[\d.]+)\s*(?P<u1>MW|kW)\)|\s+(?P<r2>[\d.]+)\s*(?P<u2>MW|kW)\b)"
UNIT_PATTERNS = (
    re.compile(rf"(?P<upto>up to\s+)?(?P<count>\d[\d,]*)\s+(?P<oem>{_OEMS})(?:\s+(?P<model>{_MODEL}))?{_RATING}?"),
    re.compile(rf"(?P<upto>up to\s+)?(?P<count>\d[\d,]*)\s+(?P<model>{_MODEL})\s*\((?P<r1>[\d.]+)\s*(?P<u1>MW|kW)\)"),
    re.compile(r"(?P<count>\d{1,3})\s*[x×]\s*(?P<r1>\d[\d,.]*)\s*(?P<u1>MW|kW)\b"),
    re.compile(rf"(?P<oem>{_OEMS})\s*\((?P<count>\d[\d,]*)\s+(?P<model>{_MODEL})\)"),
)
MENTION = re.compile(rf"(?P<oem>{_OEMS})\s+(?P<model>{_MODEL})")
_NUMBERS = re.compile(r"\b(?:GHGPSDTX|PSDTX)\d+\b|\bNSR \d{4,6}\b|\b\d{6}\b")


@dataclass(frozen=True)
class Unit:
    oem: str | None
    model: str | None
    count: int
    rating_mw: float | None
    upto: bool
    fuel: str | None
    quote: str

    @property
    def total_mw(self) -> float | None:
        return self.count * self.rating_mw if self.rating_mw is not None else None


def parse_units(text: str) -> tuple[list[Unit], list[dict[str, str]]]:
    """Units (count stated) and bare OEM/model mentions in one text, in reading order."""
    units: list[Unit] = []
    taken: list[tuple[int, int]] = []
    for pattern in UNIT_PATTERNS:
        for m in pattern.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            g = m.groupdict()
            count = int(g["count"].replace(",", ""))
            rating, unit = g.get("r1") or g.get("r2"), g.get("u1") or g.get("u2")
            mw = num(rating.replace(",", "")) if rating else None
            if mw is not None and unit == "kW":
                mw = mw / 1000
            if not (1 <= count <= 5000) or not (g.get("oem") or g.get("model") or mw):
                continue
            tail = text[m.start(): m.end() + 40].lower()
            fuel = "diesel" if "diesel" in tail else "gas" if re.search(r"\bgas\b", tail) else None
            taken.append((m.start(), m.end()))
            units.append(Unit(g.get("oem"), g.get("model"), count, mw, bool(g.get("upto")), fuel,
                              text[max(0, m.start() - 20): m.end() + 40].strip()))
    mentions = [{"oem": m["oem"], "model": m["model"], "quote": text[max(0, m.start() - 20): m.end() + 30].strip()}
                for m in MENTION.finditer(text) if not any(a <= m.start() < b for a, b in taken)]
    return units, mentions


def _pick(candidates: list[str], arc: Archive, units: list[Unit]) -> str | None:
    if len(candidates) == 1:
        return candidates[0]
    total = sum(u.total_mw or 0 for u in units)
    if total:
        fits = [p for p in candidates if (mw := num(arc.projects[p].get("mw"))) and abs(mw - total) <= 0.03 * mw]
        if len(fits) == 1:
            return fits[0]
    return None


def write_units(conn: Conn, sink: FactSink, arc: Archive, links: PowerLinks) -> dict[str, Any]:
    texts: list[tuple[Src, str | None, str, list[str], str]] = []  # (src, url, text, candidates, label)
    case_plants: dict[str, list[str]] = {}
    for pid, case in arc.pw2c.items():
        if arc.projects.get(pid, {}).get("ty") in ONSITE_TYPES:
            case_plants.setdefault(case, []).append(pid)
    for case, proj in sorted((arc.data[SITE_POWER].get("projects") or {}).items()):
        sp = proj.get("site_power") or {}
        if isinstance(sp.get("onsite_generation"), str):
            texts.append((arc.sources[SITE_POWER], first_url(sp.get("src")), sp["onsite_generation"],
                          sorted(case_plants.get(case, [])), f"site_power {case}"))
    for row in arc.dcmap_rows:
        if row.get("cov") and row.get("case") and row.get("pwr_detail"):
            cands = sorted({p["id"] for p in row.get("pw") or []
                            if arc.projects.get(p["id"], {}).get("ty") in ONSITE_TYPES} & set(links.campuses))
            texts.append((arc.sources[DCMAP], None, row["pwr_detail"], cands, f"dcmap {row['key']}"))
    for pid, p in sorted(arc.projects.items()):
        if p["ty"] in PLANT_TYPES:
            text = " | ".join(str(p.get(k)) for k in ("n", "dc", "why", "rw") if p.get(k))
            texts.append((arc.sources[POWER], first_url(p.get("src")), text, [pid], f"power {pid}"))
    done: set[str] = set()
    stats: dict[str, Any] = {"units": 0, "mentions": 0, "skipped": []}
    for src, url, text, cands, label in texts:
        units, mentions = parse_units(text)
        if not units and not mentions:
            continue
        pid = _pick(cands, arc, units)
        if pid is None:
            stats["skipped"].append(f"{label}: units/equipment text fits {len(cands)} plants {cands}")
            continue
        ppid = plant_id(pid)
        if mentions:
            sink.add("plant", ppid, "equipment_mentions", mentions, src=src, url=url)
            stats["mentions"] += len(mentions)
        if not units or pid in done:
            continue
        done.add(pid)
        for u in units:
            _unit(conn, sink, src, url, ppid, u)
            stats["units"] += 1
    return stats


def _unit(conn: Conn, sink: FactSink, src: Src, url: str | None, ppid: str, u: Unit) -> None:
    uid = f"{ppid}-{slugify(' '.join(x for x in (u.oem, u.model) if x) or 'unit')}-{u.count}x" + (
        slugify(f"{u.rating_mw:g}mw") if u.rating_mw else "")
    conn.execute(
        """INSERT INTO power_unit (id, plant_id, oem, model, count) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET plant_id = EXCLUDED.plant_id, oem = EXCLUDED.oem,
             model = EXCLUDED.model, count = EXCLUDED.count""",
        (uid, ppid, u.oem, u.model, u.count),
    )
    for field_name, value, unit in (("oem", u.oem, None), ("model", u.model, None), ("count", u.count, "count"),
                                    ("rating_mw_each", u.rating_mw, "MW"), ("fuel", u.fuel, None),
                                    ("count_is_maximum", True if u.upto else None, None)):
        sink.add("unit", uid, field_name, value, src=src, url=url, quote=u.quote, unit=unit)


def _permit(row: dict[str, Any], status: Any) -> dict[str, Any]:
    text = row.get("proceeding") or ""
    nums = _NUMBERS.findall(text)
    applicant = re.search(r"\)\s*[\d /A-Z]*\(([^,)]+)", text)
    kind = "PBR" if "PBR" in text else "PSD" if "PSD" in text else "NSR" if "NSR" in text else None
    return {k: v for k, v in {
        "agency": row["proceeding_key"].split("_")[0], "number": nums[0] if nums else None,
        "ids": nums or None, "type": kind, "applicant": clean(applicant.group(1)) if applicant else None,
        "status": status, "proceeding": clean(text), "proceeding_key": row["proceeding_key"],
        "url": row.get("list_url"),
    }.items() if v}


def write_air_permits(sink: FactSink, arc: Archive, existing_sites: set[str]) -> dict[str, Any]:
    stats: dict[str, Any] = {"plant_permits": 0, "site_permits": 0, "skipped": []}
    by_site: dict[str, list[dict[str, Any]]] = {}
    src = arc.sources[INVENTORY]
    for row in arc.inventory:
        if row.get("kind") != "state_air_permit":
            continue
        meta = arc.docket_file(row["proceeding_key"]).get("meta") or {}
        permit = _permit(row, meta.get("project_status") or meta.get("status"))
        if not permit.get("number"):
            stats["skipped"].append(f"{row['proceeding_key']}: no permit number in the record")
            continue
        slug = arc.site_slugs.get(row["case_id"])
        if slug in existing_sites:
            by_site.setdefault(slug, []).append(permit)
        cands = sorted(p for p, c in arc.pw2c.items() if c == row["case_id"]
                       and arc.projects.get(p, {}).get("ty") in ONSITE_TYPES)
        named = [p for p in cands if any(w.lower() in (arc.projects[p].get("n") or "").lower()
                                         for w in ("microgrid",) if w.lower() in permit["proceeding"].lower())]
        pick = named[0] if len(named) == 1 else cands[0] if len(cands) == 1 else None
        if permit.get("type") == "PBR":
            stats["skipped"].append(f"{row['proceeding_key']}: PBR (data-center permit), kept on the site only")
        elif pick is None:
            stats["skipped"].append(f"{row['proceeding_key']}: fits {len(cands)} on-site plants of {row['case_id']}")
        else:
            sink.add("plant", plant_id(pick), "air_permit", permit, src=src, url=permit.get("url"))
            stats["plant_permits"] += 1
    for slug, permits in sorted(by_site.items()):
        sink.add("site", slug, "air_permits", permits, src=src, url=permits[0].get("url"))
        stats["site_permits"] += len(permits)
    return stats
