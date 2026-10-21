"""PJM load forecast large-load adjustments -> facts on our PJM sites and campuses (tag F, source PJM).

PJM's long-term load forecast adds the large loads (mostly data centers) that each transmission zone's
utilities submit as "load adjustments". PJM publishes them by zone and area, not by project, in
"Total Load Adjustments Breakdown" (Table B-9b, "Total Adjustments to Summer Peak Load (MW) for Each PJM
Zone and RTO"), linked on https://www.pjm.com/planning/resource-adequacy-planning/load-forecast-dev-process.
PJM's per-customer Large Load Registry is still pending FERC approval (ER26-3515) and is not public.

The page and the workbook are stored raw. Each of our sites and campuses whose ISO is PJM and whose
utility names a PJM zone (e.g. "Dominion Energy Virginia" -> DOM, "AEP Ohio" -> AEP / AEPOHIO) gets
`pjm_zone_large_load_adjustments`: the zone's MW by forecast year, its areas, and which of our facts
named the zone ("presumed" when that fact says so). The value describes the zone, not the campus.

Cadence: the forecast is published each January (mid-year update in summer); checked monthly.
`python -m marden collect pjm_load_forecast`.
"""

from __future__ import annotations

import argparse
import io
import re
from datetime import UTC, date
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import openpyxl

from marden import cli
from marden.collectors.pjm_queue import HOST, TERMS_NOTE
from marden.collectors.rates_common import ET, ParseError, RunStats, collect, print_stats
from marden.core import db
from marden.core.documents import DocumentRow
from marden.core.facts import FactWriter
from marden.core.http import Fetcher, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "pjm_load_forecast"
PRODUCER = "collector:pjm_load_forecast"
PAGE_URL = f"https://{HOST}/planning/resource-adequacy-planning/load-forecast-dev-process"
FIELD = "pjm_zone_large_load_adjustments"
CRON_UTC = "29 12 3 * *"  # the 3rd of each month, 12:29 UTC
SOURCE = declare_source(
    SOURCE_ID, "PJM", "portal", f"https://{HOST}", 31 * 24 * 60,
    TERMS_NOTE.replace("Both exports are the public download buttons on the queue pages",
                       "The load forecast workbooks are public downloads on the load forecast page"),
)
_LINK = re.compile(r'href="([^"]*total-load-adjustments-breakdown[^"]*\.xlsx)"', re.I)
# Utility words in our facts -> (PJM zone, area within the zone or None for the whole zone).
ZONES = (
    (r"\bnovec\b|northern virginia electric", ("DOM", "NVEC")),
    (r"rappahannock", ("DOM", "REC")),
    (r"\bodec\b|old dominion electric", ("DOM", "ODEC")),
    (r"dominion", ("DOM", None)),
    (r"aep ohio|ohio power", ("AEP", "AEPOHIO")),
    (r"indiana michigan|\bi&m\b", ("AEP", "IM")),
    (r"\baep\b|appalachian power|kentucky power", ("AEP", None)),
    (r"comed|commonwealth edison", ("COMED", None)),
    (r"\bppl\b", ("PL", None)),
    (r"pse&g|\bpseg\b|public service electric", ("PS", None)),
    (r"\bpeco\b", ("PECO", None)),
    (r"\bbge\b|baltimore gas", ("BGE", None)),
    (r"smeco|southern maryland electric", ("PEPCO", "SMECO")),
    (r"\bpepco\b|potomac electric", ("PEPCO", None)),
    (r"duquesne", ("DLCO", None)),
    (r"aes ohio|dayton power", ("DAYTON", None)),
    (r"duke energy (ohio|kentucky)", ("DEOK", None)),
    (r"ohio edison|toledo edison|cleveland electric|firstenergy ohio", ("ATSI", None)),
    (r"met-ed|metropolitan edison", ("METED", None)),
    (r"penelec|pennsylvania electric", ("PENLC", None)),
    (r"jcp&l|jersey central", ("JCPL", None)),
    (r"mon power|potomac edison|west penn power", ("APS", None)),
    (r"atlantic city electric", ("AE", None)),
    (r"delmarva", ("DPL", None)),
    (r"east kentucky|\bekpc\b", ("EKPC", None)),
)


def find_workbook(page: bytes) -> str:
    m = _LINK.search(page.decode("utf-8", errors="replace"))
    if not m:
        raise ParseError(f"no 'Total Load Adjustments Breakdown' workbook link on {PAGE_URL}")
    return urljoin(PAGE_URL, m[1])


def parse_table(data: bytes) -> dict[str, Any]:
    """{'table': 'Table B-9b', 'title': ..., 'zones': {zone: {'by_year': {...}, 'areas': {area: {...}}}}}."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises zipfile/KeyError/InvalidFileException for non-workbooks
        raise ParseError(f"PJM load adjustments file is not a workbook: {type(exc).__name__}: {exc}") from None
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    if len(rows) < 6 or not str(rows[0][0] or "").startswith("Table B-9"):
        raise ParseError(f"unexpected PJM load adjustments layout: {rows[:2]!r}"[:300])
    head = next((i for i, r in enumerate(rows[:8]) if len(r) > 3 and r[1] == "ZONENAME" and r[2] == "AREANAME"), None)
    if head is None:
        raise ParseError("PJM load adjustments: no ZONENAME / AREANAME header row")
    years = [int(y) for y in rows[head][3:] if isinstance(y, int | float)]
    zones: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for r in rows[head + 1:]:
        first = str(r[0]).strip() if r[0] is not None else ""
        values = [v for v in r[3:3 + len(years)]]
        if not any(isinstance(v, int | float) for v in values):
            continue
        series = {str(y): v for y, v in zip(years, values, strict=True) if isinstance(v, int | float)}
        if first:
            current = first
            zones[first] = {"zone_name": r[1], "by_year": series, "areas": {}}
        elif current and r[2]:
            zones[current]["areas"][str(r[2]).strip()] = series
    if "DOM" not in zones or "PJM RTO" not in zones:
        raise ParseError(f"PJM load adjustments: zones {sorted(zones)} lack DOM or PJM RTO")
    return {"table": str(rows[0][0]).strip(), "title": str(rows[1][0] or "").strip(), "unit": "MW",
            "zones": zones}


def zones_for(utility: str | None) -> list[tuple[str, str | None]]:
    if not utility:
        return []
    low = utility.lower()
    found: list[tuple[str, str | None]] = []
    for pattern, zone in ZONES:
        if re.search(pattern, low) and zone[0] not in {z for z, _ in found}:
            found.append(zone)
    return found


def subjects(conn: Any) -> list[tuple[str, str, str, str]]:
    """(subject_type, id, iso text, utility text) for our sites and campuses."""
    rows = conn.execute(
        """SELECT u.subject_type, u.subject_id, i.value #>> '{}' AS iso, u.value #>> '{}' AS utility
             FROM fact_current u
             JOIN fact_current i ON i.subject_type = u.subject_type AND i.subject_id = u.subject_id
            WHERE ((u.subject_type = 'site' AND u.field = 'power_utility' AND i.field = 'power_iso')
                OR (u.subject_type = 'campus' AND u.field = 'utility' AND i.field = 'iso'))
              AND jsonb_typeof(u.value) = 'string' AND jsonb_typeof(i.value) = 'string'
            ORDER BY 1, 2"""
    ).fetchall()
    return [(r["subject_type"], r["subject_id"], r["iso"], r["utility"]) for r in rows]


def load(doc: DocumentRow, stats: RunStats) -> None:
    table = parse_table(doc.read())
    modified = (doc.meta.get("http") or {}).get("last_modified")
    day = parsedate_to_datetime(modified).astimezone(UTC).date() if modified else doc.fetched_at.astimezone(ET).date()
    day = min(day, date.today())
    written: dict[str, list[str]] = {}
    with db.transaction() as conn:
        facts = FactWriter(conn)
        for stype, sid, iso, utility in subjects(conn):
            if "pjm" not in iso.lower():
                continue
            zones = []
            for zone, area in zones_for(utility):
                z = table["zones"].get(zone)
                if z is None:
                    continue
                zones.append({
                    "zone": zone, "area": area, "by_year": z["by_year"], "areas": z["areas"],
                    "area_by_year": z["areas"].get(area) if area else None,
                    "zone_from": {"field": "power_utility" if stype == "site" else "utility", "text": utility,
                                  "presumed": "presum" in utility.lower()},
                })
            if not zones:
                continue
            value = {"table": table["table"], "title": table["title"], "unit": "MW (summer peak)", "zones": zones}
            res = facts.add(stype, sid, FIELD, value, tag="F", source_name="PJM", source_url=doc.url, valid_from=day,
                            producer=PRODUCER, status="published", document_id=doc.id)
            stats.facts_seen += 1
            stats.facts_new += int(res.created)
            written[f"{stype}:{sid}"] = [z["zone"] + (f"/{z['area']}" if z["area"] else "") for z in zones]
    stats.extra["pjm_zones"] = sorted(table["zones"])
    stats.extra["subjects"] = written


def run(*, fetcher: Fetcher | None = None) -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        page = fetch_document(SOURCE_ID, PAGE_URL, doc_type="index_html", title="PJM load forecast page",
                              fetcher=fetcher)
        stats.document(page)
        url = find_workbook(page.read())
        doc = fetch_document(SOURCE_ID, url, doc_type="load_forecast_xlsx", title="PJM Table B-9b",
                             fetcher=fetcher)
        stats.document(doc)
        load(doc, stats)

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="monthly_3rd")
@app.task(name="collect.pjm_load_forecast", queue="collect", queueing_lock="collect.pjm_load_forecast",
          lock="collect.pjm_load_forecast")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("collect", "pjm_load_forecast", help="PJM load forecast large-load adjustments by zone (Table B-9b)")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
