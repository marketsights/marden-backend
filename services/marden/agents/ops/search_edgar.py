"""EDGAR full-text search for the researcher (spec 22: "EDGAR full-text search API finds filings").

    GET https://efts.sec.gov/LATEST/search-index?q="Polaris Forge 2" "outside date"&dateRange=custom
        &startdt=2025-10-22&enddt=2026-10-22[&ciks=0001144879,...]

Every query is two exact phrases ANDed (a subject phrase and a gap term; EDGAR's search does not take OR),
limited to the root forms in ROOT_FORMS.
The JSON answer is stored raw first (`document`, source `edgar_fts`, doc_type 'edgar_fts'), then parsed:
one hit per file of a filing (`_id` = '<accession>:<filename>'). A hit is fetched, if its file is a filing's
main document or a material exhibit (EX-10, EX-99, EX-4, EX-2, EX-1), from its public EDGAR archive path
(/Archives/edgar/data/<cik>/<accession>/<file>, allowed by sec.gov's robots.txt) and stored raw.

Every SEC host shares one bucket of at most 5 requests per second with the declared SEC_USER_AGENT
(`marden.core.http`). A 401/403/407/429/451 (on a search or on a filing's file), or a search answer that is
not JSON, stops every EDGAR search of the run and is recorded on the source; nothing is retried with other
headers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from marden.core import documents, http, sources
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError
from marden.core.sources import declare_source

SOURCE_ID = "edgar_fts"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"
STOP_STATUSES = frozenset({401, 403, 407, 429, 451})
# Root forms (EDGAR's `forms` filter takes root forms: '8-K' also finds 8-K/A; naming '8-K/A' breaks it). Fund
# holdings reports (NPORT-P) are left out: an SPV's name in a fund's schedule is not a filing about it.
ROOT_FORMS = ("8-K", "10-Q", "10-K", "S-1", "S-4", "F-1", "6-K", "20-F", "424B1", "424B2", "424B3", "424B4",
              "424B5", "ABS-15G", "SC 13D", "DEFM14A")
EXHIBITS = ("EX-10", "EX-99", "EX-4", "EX-2", "EX-1")
TERMS_NOTE = (
    'SEC: "Information presented on sec.gov is considered public information and may be copied or further '
    "distributed by users of the web site without the SEC's permission.\" (https://www.sec.gov/about/"
    "privacy-information). Fair access: \"Current max request rate: 10 requests/second.\" and \"Please declare "
    'your user agent in request headers" (https://www.sec.gov/search-filings/edgar-search-assistance/'
    "accessing-edgar-data). EDGAR full-text search (https://efts.sec.gov/LATEST/search-index, the API behind "
    "https://www.sec.gov/edgar/search/) needs no key; efts.sec.gov serves no robots.txt (403 JSON 'Forbidden' "
    "for that path, checked 2026-10-22); www.sec.gov/robots.txt allows /Archives/edgar/data and disallows "
    "/cgi-bin and /search/, which are not used. The researcher sends SEC_USER_AGENT, shares the 5 req/s cap "
    "of every SEC collector, runs weekly, takes the first page of each query only and fetches at most a few "
    "files per query. A 401/403/429 or a page that is not JSON stops the run (no evasion)."
)
SOURCE = declare_source(SOURCE_ID, "SEC EDGAR full-text search (researcher)", "api", SEARCH_URL, 10080, TERMS_NOTE)


class SearchBlocked(FetchError):
    """EDGAR answered with a refusal or a page that is not the API's JSON: stop searching."""


@dataclass(frozen=True)
class Hit:
    accession: str
    filename: str
    cik: str  # the first filer's CIK, 10 digits
    form: str
    file_type: str
    filed: date | None
    filer: str
    score: float

    @property
    def url(self) -> str:
        return f"{ARCHIVE}/{int(self.cik)}/{self.accession.replace('-', '')}/{self.filename}"

    @property
    def wanted(self) -> bool:
        """A main document of a relevant form, or a material exhibit of one."""
        if self.form.upper().removesuffix("/A") not in ROOT_FORMS:
            return False
        ftype = self.file_type.upper()
        return ftype == self.form.upper() or ftype.startswith(EXHIBITS)

    def doc(self) -> dict[str, Any]:
        return {"accession": self.accession, "filename": self.filename, "cik": self.cik, "form": self.form,
                "file_type": self.file_type, "filed": self.filed.isoformat() if self.filed else None,
                "filer": self.filer, "url": self.url}


def params(query: str, since: date, until: date, ciks: list[str] | None = None) -> dict[str, str]:
    out = {"q": query, "dateRange": "custom", "startdt": since.isoformat(), "enddt": until.isoformat(),
           "forms": ",".join(ROOT_FORMS)}
    if ciks:
        out["ciks"] = ",".join(sorted(ciks))
    return out


def search_url(query: str, since: date, until: date, ciks: list[str] | None = None) -> str:
    return str(httpx.URL(SEARCH_URL, params=params(query, since, until, ciks)))


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def parse(data: bytes) -> tuple[int, list[Hit]]:
    """(total hits, hits in score order) from one search-index answer; ValueError if it is not one."""
    payload = json.loads(data)
    hits = (payload.get("hits") or {}) if isinstance(payload, dict) else {}
    total = (hits.get("total") or {}).get("value")
    if not isinstance(hits.get("hits"), list) or not isinstance(total, int):
        raise ValueError("EDGAR full-text search answer without hits.total / hits.hits")
    out: list[Hit] = []
    for h in hits["hits"]:
        src = h.get("_source") or {}
        accession, _, filename = str(h.get("_id") or "").partition(":")
        ciks = src.get("ciks") or []
        if not (accession and filename and ciks):
            continue
        names = src.get("display_names") or []
        out.append(Hit(accession=accession, filename=filename, cik=str(ciks[0]).zfill(10),
                       form=str(src.get("form") or ""), file_type=str(src.get("file_type") or ""),
                       filed=_day(src.get("file_date")), filer=str(names[0]) if names else "",
                       score=float(h.get("_score") or 0.0)))
    return total, out


class EdgarSearch:
    """Searches and fetches through one Fetcher (the shared SEC bucket); counts requests."""

    def __init__(self, fetcher: Fetcher | None = None):
        self.fetcher = fetcher or http.default_fetcher()
        self.requests = 0

    def search(self, query: str, since: date, until: date, ciks: list[str] | None = None,
               ) -> tuple[DocumentRow, int, list[Hit]]:
        """Run one query; the answer is stored raw before it is parsed."""
        sources.require_collectable(SOURCE_ID)
        self.requests += 1
        try:
            resp = self.fetcher.get(SEARCH_URL, params=params(query, since, until, ciks), use_cache=False)
        except FetchError as exc:
            if exc.status in STOP_STATUSES:
                raise SearchBlocked(exc.url, exc.status, f"stopped: HTTP {exc.status} (no evasion attempted)") from None
            raise
        if "json" not in (resp.content_type or "").lower():
            raise SearchBlocked(resp.url, resp.status, f"stopped: not JSON ({resp.content_type or 'no type'})")
        doc = documents.record_document(
            SOURCE_ID, search_url(query, since, until, ciks), resp.content, content_type=resp.content_type,
            doc_type="edgar_fts", title=f"EDGAR full-text search: {query}"[:300], doc_date=until,
            meta={"producer": "agent:researcher", "query": query, "since": since.isoformat(),
                  "until": until.isoformat(), "ciks": sorted(ciks or []), "http": {"status": resp.status}})
        try:
            total, hits = parse(resp.content)
        except ValueError as exc:
            raise SearchBlocked(resp.url, resp.status, f"stopped: unreadable answer ({exc})") from None
        return doc, total, hits

    def fetch(self, hit: Hit, meta: dict[str, Any]) -> DocumentRow:
        """The hit's file from the EDGAR archive, stored raw (the researcher queues it once it is checked)."""
        self.requests += 1
        try:
            return http.fetch_document(
                SOURCE_ID, hit.url, doc_type=hit.file_type or hit.form,
                title=f"{hit.form} {hit.file_type} {hit.filer}"[:300],
                doc_date=hit.filed, external_id=hit.accession, extract_state="none", fetcher=self.fetcher,
                meta={"producer": "agent:researcher", "cik": hit.cik.lstrip("0"), "form": hit.form,
                      "filename": hit.filename, **meta})
        except FetchError as exc:  # sec.gov refusing a file (403 'undeclared automated tool', 429) stops the run
            if exc.status in STOP_STATUSES:
                raise SearchBlocked(exc.url, exc.status, f"stopped: HTTP {exc.status} on a filing "
                                                         "(no evasion attempted)") from None
            raise
