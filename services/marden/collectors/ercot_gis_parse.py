"""Parse ERCOT's monthly GIS Report (EMIL PG7-200-ER, "GIS_Report_<Month><Year>.xlsx") into queue entries.

Layout (the workbook as ERCOT posts it; the same sheets and header names gridstatus reads):
- "Project Details - Large Gen" and "Project Details - Small Gen": one row per generation interconnection
  request (INR), header row found by its "INR" cell (a few sub-header rows follow it, then data). Named
  columns used: INR, Project Name, Interconnecting Entity, POI Location, County, CDR Reporting Zone,
  Projected COD, Fuel, Technology, Capacity (MW), GIM Study Phase, Screening Study Started, IA Signed,
  Approved for Energization, Approved for Synchronization.
- "Inactive Projects" (Inactive Date) and "Cancellation Update" (Cancel Date): INR, Project Name, County,
  Fuel, "MW **", Size Category.
Rows whose INR cell is not an INR number (sub-headers, notes) are skipped. A project listed on several
sheets keeps the last one in the order above (a cancellation is the newest record of a request).

Status: the GIS report has no status column, so `status` is the latest milestone ERCOT reports for the
request, in ERCOT's column words: 'Approved for Synchronization', 'Approved for Energization', 'IA
Signed', else the GIM Study Phase; 'Inactive' and 'Cancelled' on those sheets. `study_phase` is the GIM
Study Phase plus the milestone dates. ERCOT removes a project
from the GIS report when it reaches commercial operation, so nothing here is 'in_service'.
State: ERCOT's grid lies within Texas and the report has no state column, so every entry is TX.
queue_date: "Screening Study Started" (the report does not publish the request date).
"""

from __future__ import annotations

import io
import re
from datetime import date
from typing import Any

import openpyxl

from marden.collectors.iso_queue import QueueEntry, month_or_date, number, text, us_date
from marden.collectors.rates_common import ParseError

LARGE = "Project Details - Large Gen"
SMALL = "Project Details - Small Gen"
INACTIVE = "Inactive Projects"
CANCELLED = "Cancellation Update"
SHEETS = (LARGE, SMALL, INACTIVE, CANCELLED)
REQUIRED = (LARGE,)
# Fuel and technology codes (the report's acronym list).
FUEL = {
    "BIO": "Biomass", "COA": "Coal", "GAS": "Gas", "GEO": "Geothermal", "HYD": "Hydrogen", "NUC": "Nuclear",
    "OIL": "Fuel Oil", "OTH": "Other", "PET": "Petcoke", "SOL": "Solar", "WAT": "Water", "WIN": "Wind",
}
TECHNOLOGY = {
    "BA": "Battery Energy Storage", "CC": "Combined-Cycle", "CE": "Compressed Air Energy Storage",
    "CP": "Concentrated Solar Power", "EN": "Energy Storage", "FC": "Fuel Cell",
    "GT": "Combustion (gas) Turbine, but not part of a Combined-Cycle", "HY": "Hydroelectric Turbine",
    "IC": "Internal Combustion Engine, eg. Reciprocating", "OT": "Other", "PV": "Photovoltaic Solar",
    "ST": "Steam Turbine other than Combined-Cycle", "WT": "Wind Turbine",
}
COLUMNS = {  # field -> published header names (whitespace collapsed, lower case)
    "inr": ("inr",),
    "name": ("project name",),
    "developer": ("interconnecting entity",),
    "poi": ("poi location",),
    "county": ("county",),
    "zone": ("cdr reporting zone",),
    "cod": ("projected cod",),
    "fuel": ("fuel",),
    "technology": ("technology",),
    "mw": ("capacity (mw)", "mw **", "mw"),
    "phase": ("gim study phase",),
    "screening": ("screening study started",),
    "ia": ("ia signed",),
    "energization": ("approved for energization",),
    "sync": ("approved for synchronization",),
    "size": ("size category",),
    "inactive": ("inactive date",),
    "cancel": ("cancel date",),
}
_INR = re.compile(r"^\d{2}INR\d{3,5}[A-Z]?$")
MAX_HEADER_ROW = 60


def _norm(value: Any) -> str:
    return " ".join(str(value).split()).lower() if value is not None else ""


def _inr(value: Any) -> str | None:
    s = (text(value) or "").upper().replace(" ", "")
    return s if _INR.match(s) else None


def _header(name: str, rows: list[tuple[Any, ...]]) -> tuple[int, dict[str, int]]:
    for i, row in enumerate(rows[:MAX_HEADER_ROW]):
        names = [_norm(c) for c in row]
        if "inr" in names:
            cols = {}
            for fld, options in COLUMNS.items():
                idx = next((names.index(o) for o in options if o in names), None)
                if idx is not None:
                    cols[fld] = idx
            return i + 1, cols
    raise ParseError(f"GIS sheet {name!r} has no 'INR' header row in its first {MAX_HEADER_ROW} rows")


def _code(mapping: dict[str, str], value: Any) -> str | None:
    s = text(value)
    return mapping.get(s.upper(), s) if s else None


def parse_sheet(name: str, rows: list[tuple[Any, ...]]) -> list[QueueEntry]:
    start, cols = _header(name, rows)
    required = ("inr", "name", "county") + (("cod", "fuel") if name in (LARGE, SMALL) else ())
    missing = [f for f in required if f not in cols]
    if missing:
        raise ParseError(f"GIS sheet {name!r} lacks columns {missing}")

    def cell(row: tuple[Any, ...], fld: str) -> Any:
        i = cols.get(fld)
        return row[i] if i is not None and i < len(row) else None

    out = []
    for row in rows[start:]:
        inr = _inr(cell(row, "inr"))
        if inr is None:
            continue
        fuel, tech = _code(FUEL, cell(row, "fuel")), _code(TECHNOLOGY, cell(row, "technology"))
        size = {LARGE: "Large", SMALL: "Small"}.get(name) or text(cell(row, "size"))
        phase = text(cell(row, "phase"))
        values: dict[str, Any] = {
            "name": text(cell(row, "name")),
            "developer": text(cell(row, "developer")),
            "county": text(cell(row, "county")),
            "state": "TX",
            "poi": text(cell(row, "poi")),
            "fuel": fuel,
            "project_type": " ".join(p for p in (f"{size} Gen" if size else "Generator", f"({tech})" if tech else "")
                                     if p),
            "mw_requested": number(cell(row, "mw")),
            "queue_date": us_date(cell(row, "screening")),
            "proposed_cod": month_or_date(cell(row, "cod")),
            "zone": text(cell(row, "zone")),
        }
        if name == CANCELLED:
            values.update(status="Cancelled", status_class="withdrawn", withdrawn_date=us_date(cell(row, "cancel")))
        elif name == INACTIVE:
            since = us_date(cell(row, "inactive"))
            values.update(status="Inactive", status_class="suspended",
                          study_phase=f"Inactive since {since.isoformat()}" if since else None)
        else:
            sync, energized, ia = (us_date(cell(row, f)) for f in ("sync", "energization", "ia"))
            if sync:
                status, cls = "Approved for Synchronization", "under_construction"
            elif energized:
                status, cls = "Approved for Energization", "under_construction"
            elif ia:
                status, cls = "IA Signed", "active"
            else:
                status, cls = phase or "In study", "active"
            dates = [f"{label} {d.isoformat()}" for label, d in (
                ("IA signed", ia), ("approved for energization", energized), ("approved for synchronization", sync))
                if d]
            values.update(status=status, status_class=cls, study_phase="; ".join(([phase] if phase else []) + dates)
                          or None)
        out.append(QueueEntry(inr, values))
    return out


def parse_workbook(data: bytes) -> list[QueueEntry]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises zipfile/KeyError/InvalidFileException for non-workbooks
        raise ParseError(f"ERCOT GIS file is not an xlsx workbook: {type(exc).__name__}: {exc}") from None
    by_name = {" ".join(n.split()).lower(): n for n in wb.sheetnames}
    missing = [s for s in REQUIRED if s.lower() not in by_name]
    if missing:
        raise ParseError(f"ERCOT GIS workbook lacks sheets {missing}; has {wb.sheetnames}")
    entries: dict[str, QueueEntry] = {}
    for sheet in SHEETS:
        if sheet.lower() in by_name:
            for e in parse_sheet(sheet, list(wb[by_name[sheet.lower()]].iter_rows(values_only=True))):
                entries[e.queue_id] = e
    return list(entries.values())


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")


def report_month(friendly_name: str) -> date | None:
    """'GIS_Report_September2026' -> 2026-09-01 (the month the report covers)."""
    m = re.search(r"GIS_Report_([A-Za-z]+)_?(\d{4})", friendly_name)
    if not m or m[1].lower() not in _MONTHS:
        return None
    return date(int(m[2]), _MONTHS.index(m[1].lower()) + 1, 1)
