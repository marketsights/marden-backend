"""FERC eLibrary search responses -> filings (collectors/ferc).

The eLibrary web app's AdvancedSearch answers JSON with one hit per accession: category (Issuance or
Submittal), FERC's own class/type labels, filed / issued / posted dates (MM/DD/YYYY), the docket
numbers it sits in, filers (AUTHOR affiliations), availability ('P' public, 'N' non-public) and its
files ("transmittals"). Nothing here is inferred: a field the hit leaves blank stays None.

`queue_reasons` says which filings the extraction workers read: orders, deficiency letters (and the
responses to them), in-service notices and certificate filings, all from FERC's labels or title.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from marden.collectors.rates_common import ParseError

ELIBRARY = "https://elibrary.ferc.gov/eLibrary"
DOCKET = re.compile(r"^[A-Z]{2}\d{2}-\d{1,5}$")  # 'CP26-80' (sub-docket left off: every sub-docket)
ACCESSION = re.compile(r"^\d{8}-\d{4}$")
MAX_FILE_BYTES = 40 * 1024 * 1024  # a main file larger than this is listed, not fetched (counted in run meta)
# Files FERC withholds inside a public filing carry the label in their name ('(PRIV) Attachment C ...').
_WITHHELD = re.compile(r"^\s*\((?:PRIV|CEII|CUI)", re.I)

_ORDER_TITLE = re.compile(r"^(?:errata (?:notice )?to )?order\b|\bletter order\b", re.I)
_DEFICIENCY = re.compile(r"\bdeficien(?:t|cy)\b", re.I)
_IN_SERVICE = re.compile(
    r"\bin[- ]service\b|\bplaced (?:in|into) service\b|\bcommence(?:d|ment of)? (?:service|operations?)\b", re.I
)
_CERTIFICATE = re.compile(
    r"certificate of public convenience|blanket certificate|\bsection 7\(c\)|\bcertificate application\b", re.I
)


def check_docket(number: str) -> str:
    """'cp26-80 ' -> 'CP26-80'; a malformed number is a ValueError (never searched)."""
    text = (number or "").strip().upper()
    if not DOCKET.match(text):
        raise ValueError(f"not a FERC docket number: {number!r}")
    return text


def _day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%m/%d/%Y").date()
    except ValueError:
        raise ParseError(f"eLibrary date is not MM/DD/YYYY: {value!r}") from None


def _text(value: Any) -> str | None:
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class FileRef:
    file_id: str
    name: str
    file_type: str
    size: int | None
    description: str | None

    @property
    def url(self) -> str:
        """The public eLibrary link for this file (the web app downloads it with a POST)."""
        return f"{ELIBRARY}/filedownload?fileid={self.file_id}"

    @property
    def withheld(self) -> bool:
        return bool(_WITHHELD.match(self.name))

    def as_meta(self) -> dict[str, Any]:
        return {"file_id": self.file_id, "name": self.name, "type": self.file_type, "size": self.size,
                "description": self.description, "url": self.url}


@dataclass(frozen=True)
class Filing:
    accession: str
    document_guid: str | None
    category: str
    title: str
    filed: date | None
    issued: date | None
    posted: date | None
    class_types: tuple[tuple[str, str], ...]
    dockets: tuple[str, ...]
    availability: str | None
    filers: tuple[str, ...]
    files: tuple[FileRef, ...]
    libraries: tuple[str, ...]

    @property
    def day(self) -> date:
        """The record's own date: issued for an issuance, filed for a submittal."""
        first = (self.issued, self.filed) if self.category == "Issuance" else (self.filed, self.issued)
        for d in (*first, self.posted):
            if d is not None:
                return d
        raise ParseError(f"eLibrary {self.accession}: no filed, issued or posted date")

    @property
    def filelist_url(self) -> str:
        return f"{ELIBRARY}/filelist?accession_number={self.accession}"

    @property
    def public(self) -> bool:
        return self.availability == "P"

    @property
    def doc_type(self) -> str:
        return "ferc_issuance" if self.category == "Issuance" else "ferc_submittal"

    def main_file(self) -> tuple[FileRef | None, str | None]:
        """(the file to fetch, None) or (None, why nothing is fetched). The first public PDF, else the
        first public file: eLibrary lists the filing's main document first."""
        if not self.public:
            return None, "not_public"
        listed = [f for f in self.files if not f.withheld]
        if not listed:
            return None, "withheld_files" if self.files else "no_files"
        main = next((f for f in listed if f.file_type.upper() == "PDF"), listed[0])
        if main.size is not None and main.size > MAX_FILE_BYTES:
            return None, "too_large"
        return main, None

    def queue_reasons(self) -> list[str]:
        """Why the extraction workers should read this filing (empty: they skip it)."""
        types = [t for _, t in self.class_types]
        reasons: list[str] = []
        substantive = [t for c, t in self.class_types if c == "Order/Opinion" and not t.startswith("Dissent")]
        if substantive or (self.category == "Issuance" and _ORDER_TITLE.search(self.title)):
            reasons.append("order")
        if "Deficiency Letter" in types or (self.category == "Issuance" and _DEFICIENCY.search(self.title)):
            reasons.append("deficiency_letter")
        if "Deficiency Letter/Data Response" in types:
            reasons.append("deficiency_response")
        if _IN_SERVICE.search(self.title):
            reasons.append("in_service_notice")
        if any(_CERTIFICATE.search(t) for t in types) or _CERTIFICATE.search(self.title):
            reasons.append("certificate_filing")
        return reasons

    def as_meta(self) -> dict[str, Any]:
        def iso(d: date | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "accession": self.accession, "elibrary_document_id": self.document_guid, "category": self.category,
            "class_types": [{"class": c, "type": t} for c, t in self.class_types], "dockets": list(self.dockets),
            "filed_date": iso(self.filed), "issued_date": iso(self.issued), "posted_date": iso(self.posted),
            "availability": self.availability, "filers": list(self.filers), "libraries": list(self.libraries),
            "files": [f.as_meta() for f in self.files], "filelist_url": self.filelist_url,
        }


def _file(raw: Any, accession: str) -> FileRef:
    if not isinstance(raw, dict) or not raw.get("fileId"):
        raise ParseError(f"eLibrary {accession}: transmittal without fileId: {raw!r}"[:200])
    size = raw.get("fileSize")
    return FileRef(
        file_id=str(raw["fileId"]), name=_text(raw.get("fileName")) or "", file_type=str(raw.get("fileType") or ""),
        size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        description=_text(raw.get("fileDesc")),
    )


def _filing(hit: Any) -> Filing:
    if not isinstance(hit, dict):
        raise ParseError(f"eLibrary search hit is not an object: {hit!r}"[:200])
    accession = str(hit.get("acesssionNumber") or hit.get("accessionNumber") or "").strip()  # sic, eLibrary's key
    if not ACCESSION.match(accession):
        raise ParseError(f"eLibrary search hit without a valid accession number: {accession!r}")
    title = _text(hit.get("description"))
    category = hit.get("category")
    if title is None or category not in ("Issuance", "Submittal"):
        raise ParseError(f"eLibrary {accession}: missing description or unknown category {category!r}")
    classes = tuple(
        (str(c.get("documentClass") or ""), str(c.get("documentType") or ""))
        for c in hit.get("classTypes") or [] if isinstance(c, dict)
    )
    filers: list[str] = []
    for a in hit.get("affiliations") or []:
        name = _text(a.get("affiliation")) if isinstance(a, dict) and a.get("afType") == "AUTHOR" else None
        if name and name not in filers:
            filers.append(name)
    return Filing(
        accession=accession, document_guid=hit.get("documentId"), category=category, title=title,
        filed=_day(hit.get("filedDate")), issued=_day(hit.get("issuedDate")), posted=_day(hit.get("postedDate")),
        class_types=classes, dockets=tuple(str(d) for d in hit.get("docketNumbers") or []),
        availability=hit.get("availCode"), filers=tuple(filers),
        files=tuple(_file(t, accession) for t in hit.get("transmittals") or []),
        libraries=tuple(str(x) for x in hit.get("libraries") or []),
    )


def parse_search(data: bytes) -> tuple[int, list[Filing]]:
    """(totalHits, filings in response order) from one AdvancedSearch response page."""
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ParseError(f"eLibrary search response is not JSON: {exc}") from None
    if not isinstance(payload, dict) or "success" not in payload or "totalHits" not in payload:
        raise ParseError("eLibrary search response has no success/totalHits")
    if payload["success"] is not True:
        raise ParseError(f"eLibrary search failed: {payload.get('errorMessage')!r}")
    hits = payload.get("searchHits")
    total = payload["totalHits"]
    if not isinstance(hits, list) or not isinstance(total, int):
        raise ParseError("eLibrary search response: searchHits is not a list or totalHits not a number")
    out: dict[str, Filing] = {}
    for hit in hits:
        filing = _filing(hit)
        out.setdefault(filing.accession, filing)
    return total, list(out.values())
