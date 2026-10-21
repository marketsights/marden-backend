"""Pure parsers and rules for the `sec_index` collector (no network, no database).

- `parse_form_index`: EDGAR daily index `form.YYYYMMDD.idx` (fixed-width, sorted by form type).
- `parse_submissions`: data.sec.gov submissions JSON (`CIK##########.json` and its `-submissions-NNN`
  pages) -> `Filing` rows.
- `parse_filing_index`: a filing's `<accession>-index.htm` page -> form, dates, 8-K items, the
  document table and the filer/subject parties.
- Rules: which forms and documents are relevant, which are queued for extraction, event titles.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urljoin

from lxml import html as lxml_html

WWW = "https://www.sec.gov"
ARCHIVES = f"{WWW}/Archives/edgar"
SUBMISSIONS = "https://data.sec.gov/submissions"

ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
FORM_WIDTH = 17  # the form-type column of form.idx

# Forms this collector records (amendments included). 424B* is matched by prefix.
RELEVANT_BASE_FORMS = frozenset(
    {"8-K", "10-Q", "10-K", "10-KT", "S-1", "F-1", "ABS-15G", "10-D", "SC 13D", "SCHEDULE 13D"}
)
PERIODIC_FORMS = frozenset({"10-Q", "10-K", "10-KT", "10-D"})
SCHEDULE_13D_FORMS = frozenset({"SC 13D", "SCHEDULE 13D"})
# Documents of these forms are read by the extraction workers (plus every EX-10, whatever the form).
QUEUED_BASE_FORMS = frozenset({"8-K", "S-1", "F-1"})
EXHIBIT = re.compile(r"^EX-(?:4|10|99)(?:\.\S*)?$")  # EX-4.1 indentures, EX-10.x agreements, EX-99.x
CREDIT_EXHIBIT = re.compile(r"^EX-10(?:\.\S*)?$")
DOC_EXTENSIONS = (".htm", ".html", ".txt", ".xml", ".pdf")
ITEM_EXHIBITS = "9.01"  # 8-K "Financial Statements and Exhibits": left out of event titles
MAX_TITLE = 300


def normalize_cik(value: Any) -> str | None:
    """CIK without leading zeros ('0001769628' -> '1769628'); None when it is not a CIK."""
    text = str(value or "").strip()
    if not text.isdigit() or len(text) > 10:
        return None
    text = text.lstrip("0")
    return text or None


def cik10(cik: str) -> str:
    return f"{int(cik):010d}"


def quarter(day: date) -> int:
    return (day.month - 1) // 3 + 1


def daily_index_url(day: date, kind: str = "form") -> str:
    return f"{ARCHIVES}/daily-index/{day.year}/QTR{quarter(day)}/{kind}.{day:%Y%m%d}.idx"


def submissions_url(cik: str) -> str:
    return f"{SUBMISSIONS}/CIK{cik10(cik)}.json"


def submissions_page_url(name: str) -> str:
    return f"{SUBMISSIONS}/{name}"


def filing_folder(cik: str, accession: str) -> str:
    return f"{ARCHIVES}/data/{int(cik)}/{accession.replace('-', '')}"


def filing_index_url(cik: str, accession: str) -> str:
    return f"{filing_folder(cik, accession)}/{accession}-index.htm"


def previous_business_day(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


# ---------------------------------------------------------------- forms and documents
def base_form(form: str) -> str:
    form = " ".join(form.upper().split())
    return form[:-2] if form.endswith("/A") else form


def is_relevant_form(form: str) -> bool:
    base = base_form(form)
    return base in RELEVANT_BASE_FORMS or base.startswith("424B")


def extract_state_for(form: str, doc_type: str) -> str:
    """'queued' for 8-K, 424B*, S-1 and F-1 documents and every EX-10 exhibit; else 'none'."""
    base = base_form(form)
    if CREDIT_EXHIBIT.match(doc_type.upper()):
        return "queued"
    if base in QUEUED_BASE_FORMS or base.startswith("424B"):
        return "queued"
    return "none"


# ---------------------------------------------------------------- daily index
@dataclass(frozen=True)
class IndexLine:
    form: str
    company: str
    cik: str
    filed: date
    filename: str
    accession: str


_INDEX_TAIL = re.compile(r"\s(\d{1,10})\s+(\d{8})\s+(edgar/data/\S+)\s*$")


def parse_form_index(text: str) -> tuple[list[IndexLine], int]:
    """Lines of a daily `form.YYYYMMDD.idx`, and how many body lines did not parse."""
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.startswith("-----")), None)
    if start is None:
        raise ValueError("not an EDGAR form index: no header rule")
    out: list[IndexLine] = []
    unparsed = 0
    for line in lines[start:]:
        if not line.strip():
            continue
        m = _INDEX_TAIL.search(line)
        accession = ACCESSION.search(m.group(3)) if m else None
        if m is None or accession is None:
            unparsed += 1
            continue
        left = line[: m.start()].rstrip()
        parts = re.split(r"\s{2,}", left.strip(), maxsplit=1)
        if len(parts) == 2:
            form, company = parts
        else:
            form, company = left[:FORM_WIDTH].strip(), left[FORM_WIDTH:].strip()
        cik = normalize_cik(m.group(1))
        raw = m.group(2)
        if not form or cik is None:
            unparsed += 1
            continue
        out.append(IndexLine(form, company, cik, date(int(raw[:4]), int(raw[4:6]), int(raw[6:])),
                             m.group(3), accession.group(0)))
    return out, unparsed


# ---------------------------------------------------------------- submissions API
@dataclass(frozen=True)
class Filing:
    cik: str
    accession: str
    form: str
    filing_date: date
    report_date: date | None = None
    items: tuple[str, ...] = ()
    primary_document: str | None = None


@dataclass(frozen=True)
class SubmissionsPage:
    cik: str | None
    name: str | None
    filings: list[Filing]
    files: list[dict[str, Any]]


def _iso(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def parse_submissions(content: bytes | str, cik: str | None = None) -> SubmissionsPage:
    """The filings in a submissions JSON (the main file's `filings.recent`, or a page file)."""
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("submissions JSON is not an object")
    if "filings" in data:
        arrays = data["filings"].get("recent") or {}
        files = list(data["filings"].get("files") or [])
        cik = normalize_cik(data.get("cik")) or cik
        name = data.get("name")
    else:
        arrays, files, name = data, [], None
    if cik is None:
        raise ValueError("submissions JSON has no CIK")
    accessions = arrays.get("accessionNumber") or []

    def col(key: str) -> list[Any]:
        values = arrays.get(key) or []
        return values if len(values) == len(accessions) else [None] * len(accessions)

    filings = []
    for acc, form, filed, report, items, primary in zip(
        accessions, col("form"), col("filingDate"), col("reportDate"), col("items"), col("primaryDocument"),
        strict=True,
    ):
        filed_on = _iso(filed)
        if not acc or not form or filed_on is None or not ACCESSION.fullmatch(str(acc)):
            continue
        filings.append(Filing(
            cik=cik, accession=str(acc), form=str(form).strip(), filing_date=filed_on, report_date=_iso(report),
            items=tuple(i.strip() for i in str(items or "").split(",") if i.strip()),
            primary_document=str(primary) if primary else None,
        ))
    return SubmissionsPage(cik=cik, name=name, filings=filings, files=files)


def pages_needed(page: SubmissionsPage, since: date) -> list[str]:
    """Names of older page files to read when `recent` does not reach back to `since`."""
    if page.filings and min(f.filing_date for f in page.filings) <= since:
        return []
    names = []
    for entry in page.files:
        to = _iso(entry.get("filingTo"))
        if entry.get("name") and to is not None and to >= since:
            names.append(str(entry["name"]))
    return names


# ---------------------------------------------------------------- filing index page
@dataclass(frozen=True)
class IndexDocument:
    seq: str
    description: str
    filename: str
    url: str
    doc_type: str
    size: int | None


@dataclass(frozen=True)
class Party:
    name: str
    cik: str | None
    role: str | None


@dataclass(frozen=True)
class FilingIndex:
    accession: str
    form: str
    form_description: str
    filing_date: date
    accepted: str | None
    period: date | None
    items: tuple[tuple[str, str], ...]
    documents: tuple[IndexDocument, ...]
    parties: tuple[Party, ...]


def _text(node: Any) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


_PARTY = re.compile(r"^(?P<name>.*?)\s*\((?P<role>[^()]*)\)\s*CIK\s*:\s*(?P<cik>\d+)")
_ITEM = re.compile(r"^Item\s+(\d+\.\d+)\s*:\s*(.+)$")


def parse_filing_index(content: bytes | str, url: str) -> FilingIndex:
    """Parse `<accession>-index.htm`. Document links are absolute; inline-XBRL viewer links
    (`/ix?doc=`) are turned into their /Archives document."""
    tree = lxml_html.fromstring(content)
    name_div = tree.find(".//div[@id='formName']")
    strong = name_div.find("strong") if name_div is not None else None
    form = _text(strong).removeprefix("Form ").strip()
    description = " ".join((strong.tail or "").split()).strip(" -:") if strong is not None else ""
    acc = ACCESSION.search(_text(tree.find(".//div[@id='secNum']")))
    if not form or acc is None:
        raise ValueError(f"not an EDGAR filing index page: {url}")
    info: dict[str, Any] = {}
    for grouping in tree.findall(".//div[@class='formGrouping']"):
        head = None
        for child in grouping:
            cls = child.get("class")
            if cls == "infoHead":
                head = _text(child)
            elif cls == "info" and head:
                info[head] = child
                head = None
    filed = _iso(_text(info.get("Filing Date")))
    if filed is None:
        raise ValueError(f"filing index without a filing date: {url}")
    items: list[tuple[str, str]] = []
    if (node := info.get("Items")) is not None:
        for chunk in node.itertext():
            m = _ITEM.match(" ".join(chunk.split()))
            if m:
                items.append((m.group(1), m.group(2)))
    return FilingIndex(
        accession=acc.group(0), form=form, form_description=description, filing_date=filed,
        accepted=_text(info.get("Accepted")) or None, period=_iso(_text(info.get("Period of Report"))),
        items=tuple(items), documents=tuple(_documents(tree, url)), parties=tuple(_parties(tree)),
    )


def _documents(tree: Any, url: str) -> list[IndexDocument]:
    out = []
    for table in tree.findall(".//table[@class='tableFile']"):
        if table.get("summary") != "Document Format Files":
            continue
        for row in table.findall("tr"):
            cells = row.findall("td")
            if len(cells) < 5:
                continue
            link = cells[2].find(".//a")
            href = link.get("href") if link is not None else None
            if not href:
                continue
            href = href.split("/ix?doc=", 1)[-1] if "/ix?doc=" in href else href
            size = _text(cells[4])
            out.append(IndexDocument(
                seq=_text(cells[0]), description=_text(cells[1]), filename=_text(link),
                url=urljoin(url, href), doc_type=_text(cells[3]).upper(),
                size=int(size) if size.isdigit() else None,
            ))
    return out


def _parties(tree: Any) -> list[Party]:
    out = []
    for span in tree.findall(".//span[@class='companyName']"):
        m = _PARTY.match(_text(span))
        if m:
            out.append(Party(m.group("name").strip(), normalize_cik(m.group("cik")), m.group("role").strip()))
    return out


def relevant_documents(fi: FilingIndex) -> tuple[list[tuple[IndexDocument, bool]], int]:
    """(document, is_primary) for the primary document and its EX-4/EX-10/EX-99 exhibits, plus the
    number of documents left out (XBRL, graphics, rendered copies, other exhibits)."""
    keep: list[tuple[IndexDocument, bool]] = []
    primary_seen = False
    form = " ".join(fi.form.upper().split())
    for doc in fi.documents:
        path = doc.url.lower()
        if "/xsl" in path or not path.endswith(DOC_EXTENSIONS):
            continue
        if not primary_seen and doc.doc_type == form:
            keep.append((doc, True))
            primary_seen = True
        elif EXHIBIT.match(doc.doc_type):
            keep.append((doc, False))
    return keep, len(fi.documents) - len(keep)


def event_title(fi: FilingIndex, cik: str) -> str:
    """A filing event's title, built only from the filing index page (stable across reruns)."""
    base = base_form(fi.form)
    title = fi.form
    items = [t for code, t in fi.items if not (base == "8-K" and code == ITEM_EXHIBITS)]
    if items:
        title += ": " + "; ".join(items)
    elif fi.period is not None and base in PERIODIC_FORMS:
        title += f" for the period ended {fi.period.isoformat()}"
    elif fi.form_description:
        title += f": {fi.form_description}"
    if base in SCHEDULE_13D_FORMS:
        mine = {p.role for p in fi.parties if p.cik == cik}
        if "Subject" in mine:
            by = sorted({p.name for p in fi.parties if p.role == "Filed by"})
            title += f" (filed by {', '.join(by)})" if by else ""
        else:
            subject = sorted({p.name for p in fi.parties if p.role == "Subject"})
            title += f" (subject: {', '.join(subject)})" if subject else ""
    return title if len(title) <= MAX_TITLE else title[: MAX_TITLE - 1] + "…"
