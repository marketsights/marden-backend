"""Parsers for EIA-860M: the release page's file list, one monthly inventory workbook, and robots.txt rules.

The workbook (`<month>_generator<year>.xlsx`) has one sheet per inventory: Operating, Planned, Retired,
Canceled or Postponed (and *_PR copies for Puerto Rico). Each sheet starts with a title row ("Inventory of
Planned Generators as of August 2026"), a blank row and the header row; data rows follow, then a blank row and a
NOTES cell. Columns are read by header name, so a reordered sheet still parses; a missing column, an unreadable
status or month, or a title for another month is a ParseError and nothing from the file is written.

Values are copied, never derived: planned and actual months are 'YYYY-MM' from the sheet's month and year cells,
status is the code in parentheses at the start of the Status cell ('(V) Under construction, more than 50
percent complete' -> 'V'). Retired and Canceled or Postponed sheets have no Status column; a generator listed
there gets status 'retired' / 'canceled_or_postponed' (the sheet it is on), with the sheet title as its quote.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from marden.collectors.rates_common import ParseError

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
_MONTH_ALT = "|".join(MONTHS)
FILE_LINK = re.compile(
    r"""href\s*=\s*["']([^"']*/eia860m/(archive/)?xls/(""" + _MONTH_ALT + r""")_generator(\d{4})\.xlsx)["']""",
    re.IGNORECASE,
)
ANY_FILE_LINK = re.compile(r"""href\s*=\s*["']([^"']+\.(?:xlsx|xls|zip))["']""", re.IGNORECASE)
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
TITLE = re.compile(r"as of (" + _MONTH_ALT + r") (\d{4})", re.IGNORECASE)
STATUS = re.compile(r"^\(([A-Z]{1,3})\)\s*(.+)$")

PLANNED, OPERATING, RETIRED, CANCELED = "Planned", "Operating", "Retired", "Canceled or Postponed"
SHEETS = (OPERATING, PLANNED, RETIRED, CANCELED)  # US sheets; the *_PR sheets are Puerto Rico
SHEET_STATUS = {RETIRED: "retired", CANCELED: "canceled_or_postponed"}
PLANNED_CODES = frozenset({"P", "L", "T", "U", "V", "TS", "OT"})
OPERATING_CODES = frozenset({"OP", "SB", "OS", "OA"})
COMMON = ("Entity ID", "Entity Name", "Plant ID", "Plant Name", "Plant State", "County", "Balancing Authority Code",
          "Sector", "Generator ID", "Unit Code", "Nameplate Capacity (MW)", "Net Summer Capacity (MW)",
          "Net Winter Capacity (MW)", "Technology", "Energy Source Code", "Prime Mover Code", "Latitude", "Longitude")
MONTH_COLUMNS = {  # sheet -> [(Generator attribute, month column, year column)]
    OPERATING: [("operating", "Operating Month", "Operating Year")],
    PLANNED: [("planned", "Planned Operation Month", "Planned Operation Year")],
    RETIRED: [("operating", "Operating Month", "Operating Year"), ("retired", "Retirement Month", "Retirement Year")],
    CANCELED: [],
}
DESCRIPTOR = {  # header -> key of the eia_generator 'generator' fact
    "Plant ID": "plant_id", "Plant Name": "plant_name", "Generator ID": "generator_id", "Unit Code": "unit_code",
    "Plant State": "state", "County": "county", "Entity ID": "entity_id", "Entity Name": "entity_name",
    "Balancing Authority Code": "balancing_authority", "Sector": "sector", "Technology": "technology",
    "Energy Source Code": "energy_source", "Prime Mover Code": "prime_mover",
    "Nameplate Capacity (MW)": "nameplate_mw", "Net Summer Capacity (MW)": "summer_mw",
    "Net Winter Capacity (MW)": "winter_mw", "Latitude": "latitude", "Longitude": "longitude",
}


@dataclass(frozen=True, order=True)
class ReportFile:
    year: int
    month: int
    url: str
    archived: bool  # under /archive/ (robots.txt decides whether it may be fetched)

    @property
    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def name(self) -> str:
        return f"{MONTHS[self.month - 1]}_generator{self.year}.xlsx"


@dataclass(frozen=True)
class Generator:
    sheet: str
    row: int
    plant_id: int
    generator_id: str
    state: str
    status: str  # EIA code ('V', 'OP') or the sheet status ('retired', 'canceled_or_postponed')
    status_text: str  # the Status cell, or the sheet title for sheets without one
    descriptor: dict[str, Any]
    planned: str | None = None  # 'YYYY-MM' (Planned sheet)
    operating: str | None = None  # 'YYYY-MM' actual (Operating, Retired sheets)
    retired: str | None = None  # 'YYYY-MM' (Retired sheet)

    @property
    def key(self) -> str:
        return f"{self.plant_id}_{self.generator_id}"


@dataclass
class Report:
    year: int
    month: int
    generators: list[Generator] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)  # sheet -> generator rows read
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> rows

    @property
    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


# ---------------------------------------------------------------- release page
def list_files(html: str, base_url: str) -> tuple[list[ReportFile], int]:
    """Monthly inventory files linked from the release page (oldest first), and how many other spreadsheet or zip
    links it has (old-format annual zips, the codes table). Links inside HTML comments (EIA keeps placeholder rows
    for months not yet released there) are ignored."""
    live = COMMENT.sub("", html)
    found: dict[tuple[int, int], ReportFile] = {}
    for m in FILE_LINK.finditer(live):
        month, year = MONTHS.index(m.group(3).lower()) + 1, int(m.group(4))
        found.setdefault((year, month), ReportFile(year, month, urljoin(base_url, m.group(1)), bool(m.group(2))))
    if not found:
        raise ParseError("EIA-860M page lists no <month>_generator<year>.xlsx files")
    others = {u for u in ANY_FILE_LINK.findall(live)} - {f.group(1) for f in FILE_LINK.finditer(live)}
    return sorted(found.values()), len(others)


# ---------------------------------------------------------------- robots.txt (RFC 9309)
def robots_rules(text: str, agent: str) -> list[tuple[bool, str]]:
    """(allow, pattern) rules of the group for `agent` (product token, case-insensitive), else the '*' group."""
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in ("allow", "disallow") and agents:
            if value:
                rules.append((key == "allow", value))
    if agents:
        groups.append((agents, rules))
    token = agent.lower()
    mine = [r for names, r in groups if any(n != "*" and n in token for n in names)]
    if mine:
        return [rule for r in mine for rule in r]
    return [rule for names, r in groups if "*" in names for rule in r]


def _pattern(rule: str) -> re.Pattern[str]:
    anchored = rule.endswith("$")
    body = re.escape(rule[:-1] if anchored else rule).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def robots_allowed(rules: list[tuple[bool, str]], path: str) -> bool:
    """The longest matching rule wins; on a tie Allow wins; no match allows."""
    best: tuple[int, bool] | None = None
    for allow, rule in rules:
        if _pattern(rule).match(path):
            candidate = (len(rule), allow)
            if best is None or candidate > best:
                best = candidate
    return True if best is None else best[1]


# ---------------------------------------------------------------- workbook
def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: Any) -> str | None:
    if _blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


def _number(value: Any, where: str) -> int | float | None:
    if _blank(value):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        try:
            value = float(str(value).strip())
        except ValueError:
            raise ParseError(f"{where}: not a number: {value!r}") from None
    return int(value) if float(value).is_integer() else float(value)


def _month(row: tuple[Any, ...], idx: dict[str, int], month_col: str, year_col: str, where: str) -> str | None:
    """'YYYY-MM'; 'YYYY' when the sheet gives the year but no month (blank, or the unknown-month codes 0, 88 and 99
    that the Retired sheet uses for old units); None when both are blank."""
    month, year = row[idx[month_col]], row[idx[year_col]]
    if _blank(year):
        if _blank(month):
            return None
        raise ParseError(f"{where}: {month_col} {month!r} without a {year_col}")
    try:
        y = int(year)
        m = None if _blank(month) else int(month)
    except (TypeError, ValueError):
        raise ParseError(f"{where}: unreadable {month_col}/{year_col}: {month!r}/{year!r}") from None
    if not 1850 <= y <= 2100:  # hydro units from the 1890s are still listed
        raise ParseError(f"{where}: {year_col} out of range: {year!r}")
    if m is None or m in (0, 88, 99):
        return f"{y:04d}"
    if not 1 <= m <= 12:
        raise ParseError(f"{where}: {month_col} out of range: {month!r}")
    return f"{y:04d}-{m:02d}"


def _sheet(book: Any, name: str, report: Report, expect: tuple[int, int] | None) -> list[Generator]:
    if name not in book.sheetnames:
        raise ParseError(f"EIA-860M workbook has no {name!r} sheet")
    rows = book[name].iter_rows(values_only=True)
    title = _text((next(rows, None) or (None,))[0]) or ""
    m = TITLE.search(title)
    if not m:
        raise ParseError(f"EIA-860M {name!r} sheet title has no 'as of <Month> <Year>': {title!r}")
    as_of = (int(m.group(2)), MONTHS.index(m.group(1).lower()) + 1)
    if (report.year, report.month) == (0, 0):
        report.year, report.month = as_of
    if as_of != (report.year, report.month) or (expect is not None and as_of != expect):
        raise ParseError(f"EIA-860M {name!r} sheet is as of {as_of}, expected {expect or (report.year, report.month)}")
    next(rows, None)
    header = [_text(h) or "" for h in next(rows, ())]
    idx = {h: i for i, h in enumerate(header) if h}
    needed = [*COMMON, *(c for _, m, y in MONTH_COLUMNS[name] for c in (m, y))]
    needed += [] if name in SHEET_STATUS else ["Status"]
    missing = [h for h in needed if h not in idx]
    if missing:
        raise ParseError(f"EIA-860M {name!r} sheet is missing columns {missing}")
    out: list[Generator] = []
    for n, row in enumerate(rows, start=4):
        row = tuple(row) + (None,) * (len(header) - len(row))
        where = f"{name} row {n}"
        plant = row[idx["Plant ID"]]
        if _blank(plant) or (isinstance(plant, str) and not plant.strip().isdigit()):
            first = _text(row[0]) or ""
            reason = "notes" if first.startswith("NOTES") else "blank" if all(_blank(v) for v in row) else (
                "no_plant_id")
            report.skipped[reason] = report.skipped.get(reason, 0) + 1
            continue
        gen_id = _text(row[idx["Generator ID"]])
        state = _text(row[idx["Plant State"]])
        if gen_id is None or state is None:
            raise ParseError(f"{where}: generator without a Generator ID or Plant State")
        descriptor: dict[str, Any] = {}
        for col, key in DESCRIPTOR.items():
            value = row[idx[col]]
            if key.endswith("_mw") or key in ("latitude", "longitude", "plant_id", "entity_id"):
                value = _number(value, f"{where} {col}")
            else:
                value = _text(value)
            if value is not None:
                descriptor[key] = value
        if name in SHEET_STATUS:
            code, text = SHEET_STATUS[name], title
        else:
            cell = _text(row[idx["Status"]]) or ""
            sm = STATUS.match(cell)
            if not sm:
                raise ParseError(f"{where}: unreadable Status {cell!r}")
            code, text = sm.group(1), cell
        months = {attr: _month(row, idx, m, y, where) for attr, m, y in MONTH_COLUMNS[name]}
        out.append(Generator(name, n, int(descriptor["plant_id"]), gen_id, state, code, text, descriptor, **months))
    report.rows[name] = len(out)
    return out


def parse_workbook(data: bytes, expect: tuple[int, int] | None = None) -> Report:
    """Every US generator in one EIA-860M workbook. `expect` = (year, month) from the file name."""
    import openpyxl

    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # not a workbook
        raise ParseError(f"EIA-860M file is not a readable workbook: {exc}") from None
    report = Report(0, 0)
    try:
        for name in SHEETS:
            report.generators.extend(_sheet(book, name, report, expect))
    finally:
        book.close()
    dupes = sorted(k for k, n in Counter(g.key for g in report.generators).items() if n > 1)[:5]
    if dupes:
        raise ParseError(f"EIA-860M {report.label}: generators listed on two sheets: {dupes}")
    return report
