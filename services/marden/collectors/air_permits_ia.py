"""Iowa adapter of `air_permits`: Iowa DNR air construction permits for our Iowa plants.

Where the records are (checked 2026-10-21): the Iowa DNR Air Quality construction permit search (AQRR,
https://idnraqrr.iowadnr.gov/ConPermitSearch; no robots.txt) lists every issued construction permit by plant:
a plant-name autocomplete (JSON), the plant's page (county, city, plant number; one row per permit: permit number,
project number, emission point, equipment description, date issued) and a download link per permit that serves
the project's "Collection of Air Permits" PDF from programs.iowadnr.gov (Iowa EASY Air; no robots.txt). Iowa DNR
notices point the public there: "The public may review the following air quality construction permits available
online at https://idnraqrr.iowadnr.gov/ConPermitSearch". Applications not yet permitted are not listed.

Run: autocomplete prefixes from our Iowa plants' names -> plant pages of names that carry the plant's project
phrase (stored raw when their permit rows changed: the pages carry a per-request e-mail obfuscation key) -> one
PDF per project, stored raw and read: company, contacts' e-mail domains (the owner: midamerican.com), the
process description, the emission-unit table and the combined tons/year limits. Rules: User-Agent "Marden
research cody@marden.ai", one request per 2 s, no retries, stop on 401/403/429 or a challenge page.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import unquote, urlencode

from marden.collectors import air_permits_text as text
from marden.collectors.air_permits_match import (
    GENERATION,
    Action,
    Facility,
    Match,
    Permit,
    PlantProfile,
    project_phrase,
    same_name,
)
from marden.collectors.air_permits_tx import get
from marden.collectors.iso_queue_match import contains, tokens
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, http
from marden.core.http import Fetcher
from marden.core.sources import declare_source

AGENCY = "Iowa DNR"
HOST = "idnraqrr.iowadnr.gov"
BASE = f"https://{HOST}/ConPermitSearch"
TERMS = (
    "Checked 2026-10-21. Iowa DNR Air Quality construction permit search (AQRR), https://idnraqrr.iowadnr.gov/"
    "ConPermitSearch: no robots.txt (404); permit PDFs are served from programs.iowadnr.gov (Iowa EASY Air; no "
    "robots.txt). Iowa DNR public notices: \"The public may review the following air quality construction permits "
    "available online at https://idnraqrr.iowadnr.gov/ConPermitSearch\". State of Iowa site policy "
    "(https://www.iowa.gov/pages/policies) applies. Marden: User-Agent \"Marden research cody@marden.ai\", one "
    "request per 2 s, weekly; stops on any refusal or challenge."
)
SOURCE = declare_source("iowa_dnr_aqrr", "Iowa DNR air construction permits (AQRR)", "portal", BASE, 7 * 24 * 60, TERMS)
http.set_rate_limit(HOST, 0.5)
http.set_rate_limit("programs.iowadnr.gov", 0.5)
ROW = re.compile(r'<a class="btn btn-secondary" href="(/ConPermitSearch/DownloadFile\?[^"]+)">Download</a>', re.I)
SELECT = re.compile(r'data-selectclick="(\d+)"')
EMAIL = re.compile(r"[\w.+-]+@([\w-]+)\.(?:com|org|net|coop|gov|us)\b", re.I)


@dataclass(frozen=True)
class PermitRow:
    permit: str
    project: str
    emission_point: str
    equipment: str
    issued: date | None
    url: str


def plant_page(page: bytes) -> tuple[dict[str, str], list[PermitRow]]:
    body = page.decode("utf-8", errors="replace")
    flat = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", body, flags=re.S))).split())
    head = {}
    for label in ("County Name", "City Name", "Plant Name", "Plant Number"):
        if m := re.search(rf"{label}:\s*(.+?)\s+(?:County Name|City Name|Plant Name|Plant Number|Note):", flat):
            head[label] = m.group(1).strip()
    rows = []
    for href in ROW.findall(body):
        href = html.unescape(href)
        m = re.search(r"dataString=([^&]+)", href)
        data = json.loads(unquote(m.group(1))) if m else {}
        if not data.get("permitNumber"):
            continue
        issued = text.to_date(f"{data['issueDate'][5:7]}/{data['issueDate'][8:10]}/{data['issueDate'][:4]}") \
            if data.get("issueDate") else None
        rows.append(PermitRow(data["permitNumber"], data.get("projectNumber") or "", data.get("emissionPointID") or "",
                              data.get("emissionPointDescription") or "", issued, f"https://{HOST}{href}"))
    return head, rows


def collection(body: str) -> dict[str, Any]:
    """What a 'Collection of Air Permits' states: company, contact domains, process, emission units, limits."""
    head = text.labelled(body, ["Plant Number", "Company", "Project Number", "Project Description", "Date",
                                "Equipment Location"])
    process = re.search(r"Project/Process Description\s+(.+?)\s+Equipment Location", body, re.S)
    table = re.search(r"Table 1 - List of Emission Units.*?Testing\s+(.+?)(?:PERMIT CONDITIONS|Table 2)", body, re.S)
    units = []
    if table:
        for m in re.finditer(r"(EP-\d+)\s+(EU-\d+)\s+(.+?)\s+([\d,.]+\s*(?:MMBtu/hr|MW|kW|hp|bhp)[^\n]*)", table.group(1), re.S):
            desc = " ".join(m.group(3).split())
            units.append({"quote": " ".join(m.group(0).split()), "emission_point": m.group(1), "emission_unit": m.group(2),
                          "description": desc, "capacity": " ".join(m.group(4).split()),
                          "oem": text.oem_of(desc), "count": 1 if GENERATION.search(desc) else None})
    limits = []
    t2 = re.search(r"Table 2 - Combined Emission Limits(.+?)(?:\n\s*B\.|Table 3)", body, re.S)
    if t2:
        for m in re.finditer(r"([A-Z][A-Za-z ]+\(([A-Za-z0-9.]+)\))\s+([\d,]+(?:\.\d+)?)", t2.group(1)):
            limits.append({"pollutant": m.group(2), "value": float(m.group(3).replace(",", "")), "unit": "tons/yr",
                           "basis": "combined, 12-month rolling", "quote": " ".join(m.group(0).split())})
    return {"company": head.get("Company"), "project": head.get("Project Number"),
            "project_description": head.get("Project Description"), "date": text.to_date(head.get("Date")),
            "process": " ".join(process.group(1).split()) if process else None,
            "domains": sorted({d.lower() for d in EMAIL.findall(body)}), "units": units, "limits": limits}


def _store_page(url: str, resp_content: bytes, rows: list[PermitRow], stats: RunStats, title: str) -> int:
    prior = documents.latest_document(SOURCE.id, url)
    if prior is not None and plant_page(prior.read())[1] == rows:
        stats.document(prior)
        return prior.id
    doc = documents.record_document(SOURCE.id, url, resp_content, content_type="text/html", doc_type="aqrr_plant_html",
                                    title=title, meta={"permits": len(rows)})
    stats.document(doc)
    return doc.id


def facility(fetcher: Fetcher, plant_id: str, stats: RunStats, info: dict[str, Any]) -> Facility | None:
    url = f"{BASE}/ConstructionPermit?id={plant_id}"
    resp = get(fetcher, SOURCE.id, url, info)
    head, rows = plant_page(resp.content)
    if not head.get("Plant Number"):
        raise ParseError(f"Iowa DNR plant page {url}: no plant header")
    doc_id = _store_page(url, resp.content, rows, stats, f"Iowa DNR construction permits: {head.get('Plant Name')}")
    by_project: dict[str, list[PermitRow]] = defaultdict(list)
    for r in rows:
        by_project[r.project].append(r)
    names = [head.get("Plant Name") or ""]
    generation = None
    projects = []
    for project, prow in sorted(by_project.items(), key=lambda kv: (min((r.issued or date.max) for r in kv[1]), kv[0])):
        first = prow[0]
        pdf = documents.latest_document(SOURCE.id, first.url)
        if pdf is None:
            got = get(fetcher, SOURCE.id, first.url, info)
            if not got.content.startswith(b"%PDF"):
                info.setdefault("not_pdf", []).append(first.url)
                continue
            pdf = documents.record_document(SOURCE.id, first.url, got.content, content_type="application/pdf",
                                            doc_type="aqrr_permit_pdf", title=f"Iowa DNR project {project} permits",
                                            doc_date=first.issued, external_id=first.permit, extract_state="queued",
                                            meta={"final_url": got.final_url, "plant_number": head.get("Plant Number")})
        stats.document(pdf)
        c = collection(text.pdf_text(pdf.read()))
        names += [c["company"] or ""] + [d for d in c["domains"] if d not in {"iowadnr", "dnr", "gmail", "outlook"}]
        if c["process"] and GENERATION.search(c["process"]):
            generation = generation or f"Project/Process Description: {c['process']}"
        projects.append((project, prow, pdf, c))
    if not projects:
        return None
    return Facility(SOURCE.id, AGENCY, "IA", head["Plant Number"], head.get("Plant Name") or head["Plant Number"],
                    head.get("County Name"), sorted({n for n in names if n}), generation,
                    [plant_permit(head, projects, url, doc_id)], url, doc_id,
                    quote=f"County Name: {head.get('County Name')} City Name: {head.get('City Name')} Plant Name: "
                          f"{head.get('Plant Name')} Plant Number: {head.get('Plant Number')}")


def plant_permit(head: dict[str, str], projects: list[tuple[str, list[PermitRow], Any, dict[str, Any]]], url: str,
                 doc_id: int) -> Permit:
    """A plant's construction permits as one permit card: the first project's permits, later projects (new or
    changed emission units) as amendments; units and limits of every project, each with its project."""
    actions, units, limits = [], [], []
    for project, prow, pdf, c in projects:
        src = {"url": prow[0].url, "document_id": pdf.id, "project": project}
        units += [dict(u, **src) for u in c["units"]]
        limits += [dict(e, **src) for e in c["limits"]]
        issued = min((r.issued for r in prow if r.issued), default=None)
        if issued:
            when = f"{c['date'].month}/{c['date'].day}/{c['date'].year}" if c["date"] else "n/a"
            actions.append(Action(issued, "final_action", f"Construction permits {', '.join(r.permit for r in prow)} "
                                  f"issued ({c['project_description'] or 'project ' + project})", prow[0].url, project,
                                  quote=f"Project Number: {project} Project Description: {c['project_description']} "
                                        f"Date: {when}", document_id=pdf.id))
    first_project, first_rows, _, first_c = projects[0]
    ids = [r.permit for _, prow, _, _ in projects for r in prow]
    return Permit(
        AGENCY, ", ".join(r.permit for r in first_rows), ids, type="Construction permit", status="issued",
        agency_status="issued", applicant=first_c["company"], issued=actions[0].day if actions else None,
        units=units, emission_limits=limits, actions=actions, url=url,
        quote=f"{head.get('Plant Name')} {head.get('Plant Number')}: " + "; ".join(
            f"{r.permit} project {r.project} {r.emission_point} {r.equipment} issued {r.issued}"
            for _, prow, _, _ in projects for r in prow),
        quote_loc="Iowa DNR construction permit search, plant page", document_id=doc_id,
    )


def candidates(fetcher: Fetcher, plants: list[PlantProfile], stats: RunStats, info: dict[str, Any]) -> dict[str, str]:
    """AQRR plant id -> plant name, for AQRR plant names that are our plant's name or carry its project phrase.
    The autocomplete matches the start of a name, so it is asked for the first word of each plant name."""
    found: dict[str, str] = {}
    asked: dict[str, list[str]] = {}
    for p in plants:
        words = tokens(p.name)
        if not words:
            continue
        if words[0] not in asked:
            url = f"{BASE}/ConstructionPermitPlantNameAutoComplete?{urlencode({'prefix': words[0]})}"
            asked[words[0]] = json.loads(get(fetcher, SOURCE.id, url, info).content)
        want = project_phrase(p.name)
        for name in asked[words[0]]:
            if not (same_name(name, p.name) or (want and contains(tokens(name), want))):
                continue
            view = f"{BASE}/ByConstructionPermitPlantNameView?{urlencode({'prefix': name})}"
            for pid in SELECT.findall(get(fetcher, SOURCE.id, view, info).content.decode("utf-8", "replace")):
                found.setdefault(pid, name)
    info["autocomplete"] = asked
    return found


def run_ia(plants: list[PlantProfile], fetcher: Fetcher, stats: RunStats, info: dict[str, Any], match: Any) -> list[Match]:
    ids = candidates(fetcher, plants, stats, info)
    info["aqrr_plants"] = ids
    facilities = [f for pid in sorted(ids) if (f := facility(fetcher, pid, stats, info))]
    return match(facilities)
