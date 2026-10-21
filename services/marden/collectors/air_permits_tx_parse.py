"""Parsers for the Texas adapter of `air_permits` (TCEQ). See air_permits_tx for the sources.

- `facilities`: Central Registry rows (data.texas.gov) -> one Facility per regulated entity (RN) with its air
  authorizations: AIRNSR (NSR permits, standard permits, PBR registrations, PSDTX / GHGPSDTX numbers) and AIROP
  (federal operating permits, shown as O<number>). Ten-digit AIRNSR ids are TCEQ air account numbers, not permits.
- `ro_rows`: a TCEQ Records Online result page -> RoRow (content id, record series, primary / secondary id,
  title (the document type: Final Action, Technical Review ...), begin date, description, file URL).
- `families`: Records Online rows -> permit families: ids that share a project number (182126, PSDTX1688 and
  GHGPSDTX263 under project 400428) are one permit; each family keeps its projects' dated actions.
- `technical_review` / `final_action`: what a TCEQ technical review or final-action letter states: header fields,
  equipment statements, emission totals, expiration date. Values come only from the document's words.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

from marden.collectors import air_permits_text as text
from marden.collectors.air_permits_match import GENERATION, Action, Facility, Permit

AGENCY = "TCEQ"
RO_BASE = "https://records.tceq.texas.gov/cs/idcplg"
FILE_URL = RO_BASE + "?IdcService=TCEQ_EXTERNAL_SEARCH_GET_FILE&dID={}&Rendition=Web&searchType=External"
PERMIT_ID = re.compile(r"^(GHGPSDTX\d+|PSDTX\d+|HAP\d+|\d{4,6}[A-Z]?)$")
CR_STATUS = {"ACTIVE": "issued", "PENDING": "pending", "CANCELLED": "cancelled", "INACTIVE": "inactive",
             "VOID": "cancelled", "VOIDED": "cancelled", "EXPIRED": "inactive", "DENIED": "denied"}
NAICS_POWER = re.compile(r"^2211\d*\b|^221\b")
TR_LABELS = ["Company", "Registration Number", "Registration No.", "Permit Numbers", "Permit Number", "Permit No.",
             "Project Number", "Project No.", "County", "Project Type", "Regulated Entity Number",
             "Regulated Entity No.",
             "Project Received Date", "Received Date", "Site Name", "Unit Name", "City", "Nearest City", "PBR No(s).",
             "Customer Reference Number", "Customer Reference No."]
ACTION_KINDS = {  # Records Online title -> action kind; other titles (modeling, RBLC, MAERT drafts) are not actions
    "Initial Application": "application", "Final Action": "final_action", "Public Notices & Comments": "notice",
    "Preliminary Determination Summary": "draft", "Statement of Basis": "draft",
}


# ---------------------------------------------------------------- Central Registry
def _generation(rows: list[dict[str, Any]]) -> str | None:
    for r in rows:
        for key in ("reg_ent_desc_txt", "reg_ent_name"):
            if (v := r.get(key)) and GENERATION.search(v):
                return f"{key}: {v}"
        for key in ("indus_type_cd_name",):
            if (v := r.get(key)) and NAICS_POWER.search(v):
                return f"{key}: {v}"
        if (v := r.get("indus_typ_cd_1")) == "4911":
            return f"indus_typ_name_1: {r.get('indus_typ_name_1') or '4911'}"
    return None


def cr_permit_ids(rows: list[dict[str, Any]]) -> dict[str, tuple[str, list[str]]]:
    """Permit-like id -> (its Central Registry status, its current customers). AIROP ids get the 'O' prefix TCEQ
    writes them with; a customer whose affiliation has ended is left out."""
    out: dict[str, tuple[str, list[str]]] = {}
    for r in rows:
        raw = (r.get("additional_id_text") or "").strip().upper()
        if not raw:
            continue
        pid = f"O{raw}" if r.get("program_code") == "AIROP" and raw.isdigit() else raw
        if r.get("program_code") == "AIRNSR" and not PERMIT_ID.match(raw):
            continue  # 4844100274: an air account number, not a permit
        _, customers = out.setdefault(pid, ((r.get("additional_id_status") or "").strip().upper(), []))
        name = (r.get("princ_legal_name") or r.get("princ_name") or "").strip()
        if name and (r.get("affil_end_dt") or "3000").startswith("3000") and name not in customers:
            customers.append(name)
    return out


RECORD_URL = "https://data.texas.gov/resource/{}.json?ref_num_txt={}"


def cr_permit(rn: str, pid: str, st: str, customers: list[str], dataset: str, document_id: int | None) -> Permit:
    """A permit as the Central Registry lists it: number, status, current customers, kind from the number."""
    kind = "Title V" if pid[:1] == "O" else "PSD" if pid.startswith(("PSDTX", "GHGPSDTX")) else None
    number = pid[1:] if pid[:1] == "O" else pid
    return Permit(AGENCY, pid, [pid], type=kind, status=CR_STATUS.get(st, st.lower() or None), agency_status=st or None,
                  applicant=", ".join(sorted(customers)) or None, document_id=document_id,
                  url=RECORD_URL.format(dataset, rn) + f"&additional_id_text={number}",
                  quote=f"{rn} {pid} {st}" + (f" ({'; '.join(sorted(customers))})" if customers else ""),
                  quote_loc="TCEQ Central Registry Files row (data.texas.gov)")


def facilities(rows: list[dict[str, Any]], *, url: str, document_id: int | None, system: str,
               dataset: str) -> list[Facility]:
    """One Facility per regulated entity. `url` is the stored query; each permit links to the same data set
    filtered to its RN and id (what a reader opens), the facility to its RN."""
    by_rn: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if rn := (r.get("ref_num_txt") or "").strip():
            by_rn[rn].append(r)
    out = []
    for rn, rs in sorted(by_rn.items()):
        first = rs[0]
        ids = cr_permit_ids(rs)
        if not ids:
            continue
        names = sorted({n.strip() for r in rs for n in (r.get("princ_name"), r.get("princ_legal_name"))
                        if n and n.strip()})
        permits = [cr_permit(rn, pid, st, customers, dataset, document_id)
                   for pid, (st, customers) in sorted(ids.items())]
        county = (first.get("re_phys_loc_addr_county") or "").strip().title() or None
        out.append(Facility(system, AGENCY, "TX", rn, (first.get("reg_ent_name") or rn).strip(), county, names,
                            _generation(rs), permits, RECORD_URL.format(dataset, rn), document_id,
                            quote=f"{rn} {first.get('reg_ent_name')}, {first.get('re_phys_loc_addr_county')} County: "
                                  f"{first.get('reg_ent_desc_txt') or ''}".strip()))
    return out


# ---------------------------------------------------------------- Records Online result pages
@dataclass(frozen=True)
class RoRow:
    content_id: str
    did: str
    series: str
    primary: str
    secondary: str
    title: str
    begin: date | None
    description: str
    re_name: str

    @property
    def url(self) -> str:
        return FILE_URL.format(self.did)


class _ResultTable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.rows: list[tuple[dict[str, str], list[str]]] = []
        self._row: tuple[dict[str, str], list[str]] | None = None
        self._cell: list[str] | None = None
        self._header: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        cls = a.get("class", "")
        if tag == "tr":
            self._row = ({}, [])
        elif tag == "td" and "xuiListHeaderCell" in cls:
            self._header = []
        elif tag == "td" and "xuiListContentCell" in cls and self._row is not None:
            self._cell = []
        elif tag == "input" and a.get("type") == "hidden" and a.get("name") and self._row is not None:
            self._row[0][a["name"]] = a.get("value", "")
        elif tag == "script" and self._cell is not None:
            self._cell.append("\x00")  # cell scripts (action menus) are cut off below

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._header is not None:
            self.headers.append(" ".join("".join(self._header).split()))
            self._header = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row[1].append(" ".join("".join(self._cell).split("\x00")[0].split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row[0].get("dDocName"):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        elif self._header is not None:
            self._header.append(data)


DEFAULT_COLUMNS = ["Select", "Content ID", "Record Series", "Primary ID", "Secondary ID", "Document Type", "Title",
                   "Begin Date", "End Date", "Litigation Hold", "Regulated Entity Name", "Media", "Description",
                   "Security Group", "Actions"]


def _ro_date(value: str) -> date | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", value or "")
    return date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def ro_rows(page: bytes) -> list[RoRow]:
    p = _ResultTable()
    p.feed(page.decode("utf-8", errors="replace"))
    headers = [h for h in p.headers if h]
    cols = headers if "Content ID" in headers and "Title" in headers else DEFAULT_COLUMNS
    out = []
    for hidden, cells in p.rows:
        cell = dict(zip(cols, cells, strict=False))
        out.append(RoRow(
            content_id=hidden.get("dDocName", ""), did=hidden.get("dID", ""),
            series=cell.get("Record Series", ""), primary=cell.get("Primary ID", "").upper(),
            secondary=cell.get("Secondary ID", ""), title=cell.get("Title", "") or hidden.get("dDocTitle", ""),
            begin=_ro_date(cell.get("Begin Date", "")), description=html.unescape(cell.get("Description", "")),
            re_name=cell.get("Regulated Entity Name", ""),
        ))
    return sorted(out, key=lambda r: (r.series, r.primary, r.secondary, r.title, r.begin or date.min, r.content_id))


def row_signature(rows: list[RoRow]) -> list[tuple[Any, ...]]:
    """What a result page says, without its per-request tokens: unchanged pages are not stored again."""
    return [(r.content_id, r.did, r.series, r.primary, r.secondary, r.title, r.begin, r.description) for r in rows]


# ---------------------------------------------------------------- permit families
@dataclass
class Family:
    ids: list[str]
    series: str
    projects: dict[str, list[RoRow]] = field(default_factory=dict)

    @property
    def fop(self) -> bool:
        return "Federal Operating" in self.series

    @property
    def number(self) -> str:
        nums = [i for i in self.ids if i.isdigit()] or self.ids
        main = sorted(nums, key=lambda i: (not i.isdigit(), i))[0]
        return f"O{main}" if self.fop and main.isdigit() else main


def families(rows: list[RoRow]) -> list[Family]:
    """Permits per record series. Each numeric permit is its own family; a PSDTX / GHGPSDTX / HAP number joins the
    one numeric permit it shares a project with (182126 + PSDTX1688 + GHGPSDTX263 under project 400428). Two
    numeric permits processed under one project (a standard permit and a PBR registration) stay apart."""
    air = [r for r in rows if r.series.startswith("AIR /") and r.primary]
    numeric_by_project: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in air:
        if r.primary.isdigit() and r.secondary:
            numeric_by_project[(r.series, r.secondary)].add(r.primary)
    home: dict[tuple[str, str], str] = {}
    for r in air:
        if r.primary.isdigit() or not r.secondary:
            continue
        hosts = numeric_by_project.get((r.series, r.secondary), set())
        if len(hosts) == 1:
            home.setdefault((r.series, r.primary), next(iter(hosts)))
    groups: dict[tuple[str, str], Family] = {}
    for r in air:
        root = (r.series, home.get((r.series, r.primary), r.primary))
        fam = groups.setdefault(root, Family([], r.series))
        if r.primary not in fam.ids:
            fam.ids.append(r.primary)
        fam.projects.setdefault(r.secondary or "-", []).append(r)
    for fam in groups.values():
        fam.ids.sort(key=lambda i: (not i.isdigit(), i))
    return sorted(groups.values(), key=lambda f: f.number)


def project_rows(fam: Family, project: str) -> list[RoRow]:
    """One row per document (the family's ids repeat each document under every id)."""
    seen: dict[str, RoRow] = {}
    for r in fam.projects.get(project, []):
        seen.setdefault(r.content_id, r)
    return sorted(seen.values(), key=lambda r: (r.begin or date.min, r.title))


def actions(fam: Family) -> list[Action]:
    out = []
    for project in sorted(fam.projects):
        for r in project_rows(fam, project):
            kind = ACTION_KINDS.get(r.title)
            if r.title == "Final Action" and re.search(r"withdraw", r.description, re.I):
                kind = "withdrawal"
            if kind is None or r.begin is None:
                continue
            what = r.title + (f": {r.description}" if r.description and not r.description.isdigit() else "")
            out.append(Action(r.begin, kind, what, r.url, None if project == "-" else project,
                              quote=f"{r.series} {r.primary} project {r.secondary or 'n/a'}: {r.title}, "
                                    f"{r.begin.isoformat()}" + (f" ({r.description})" if r.description else "")))
    return _dedupe(out)


def _dedupe(items: list[Action]) -> list[Action]:
    """One action per (project, kind, day): Records Online files English and Spanish notices separately."""
    seen: set[tuple[Any, ...]] = set()
    out = []
    for a in sorted(items, key=lambda a: (a.day, a.project or "", a.kind)):
        key = (a.project, a.kind, a.day)
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


# ---------------------------------------------------------------- documents
def technical_review(body: str) -> dict[str, Any]:
    head = text.labelled(body, TR_LABELS)
    first = " ".join(ln.strip() for ln in body.strip().splitlines()[:3] if ln.strip())
    kind = ("Standard" if re.search(r"Standard Permit", first, re.I) else
            "PBR" if re.search(r"Permit by Rule", first, re.I) else
            "Title V" if re.search(r"Operating Permit|Title V", first, re.I) else
            "NSR" if re.search(r"Construction Permit", first, re.I) else None)
    received = head.get("Project Received Date") or head.get("Received Date")
    received_label = "Project Received Date" if "Project Received Date" in head else "Received Date"
    emissions = (text.emission_totals(body, "Proposed Allowable Emission Rates (tpy)")
                 or text.emission_totals(body, "TOTAL EMISSIONS (TPY):"))
    return {
        "title": first, "type": kind, "header": head, "received": text.to_date(received),
        "received_quote": f"{received_label} {received}" if received else None,
        "project_type": head.get("Project Type"), "county": head.get("County"),
        "units": text.unit_statements(body), "emissions": emissions,
    }


def final_action(body: str) -> dict[str, Any]:
    flat = text.squash(body)
    expires = re.search(rf"Expiration Date:\s*((?:{text.MONTHS})\s+\d{{1,2}},\s*\d{{4}})", flat)
    applied = re.search(rf"submitted an application on ((?:{text.MONTHS})\s+\d{{1,2}},\s*\d{{4}})", flat)
    understand = re.search(r"(We understand that this registration is for [^.]+\.)", flat)
    decided = re.search(r"(has made a decision to (?:grant|deny)[^.]*\.)", flat)
    letter = text.to_date(flat[:600])
    return {
        "letter_date": letter,
        "expires": text.to_date(expires.group(1)) if expires else None,
        "expires_quote": expires.group(0) if expires else None,
        "applied": text.to_date(applied.group(1)) if applied else None,
        "applied_quote": applied.group(0) if applied else None,
        "statement": understand.group(1) if understand else (decided.group(1) if decided else None),
        "units": text.unit_statements(understand.group(1)) if understand else [],
        "emissions": text.emission_totals(body, "TOTAL EMISSIONS (TPY):"),
    }


def day_of(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value[:10]).date()
        except ValueError:
            return None
    return None
