"""FERC eLibrary text search for the researcher: filings in any docket that name a subject.

The eLibrary web app's Advanced Search (`collectors.ferc_api`: POST /eLibraryWebAPI/api/Search/AdvancedSearch)
with `searchText` = the subject phrase in quotes, every docket, posted in the window. The answer is stored raw
(source `ferc_fts`, doc_type 'ferc_search') before it is parsed; a filing's main public file is fetched raw
the same way the docket collector does (POST File/DownloadP8File, stored under its public filedownload link)
with extract_state 'none'; the researcher queues it once it has checked the file is about the subject. The
docket collector (`collect ferc`) already follows the dockets tied to our subjects every hour; this finds
filings in dockets Marden does not follow yet. One request every 2 s on the eLibrary host, declared
User-Agent; a challenge, 401/403 or a persisting 429 stops the run (`Challenged`).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.collectors.ferc import TERMS_NOTE as FERC_TERMS
from marden.collectors.ferc_api import SEARCH_URL, ElibraryClient, search_body
from marden.collectors.ferc_parse import FileRef, Filing, parse_search
from marden.core import documents, sources
from marden.core.documents import DocumentRow
from marden.core.sources import declare_source

SOURCE_ID = "ferc_fts"
PAGE_SIZE = 20
TERMS_NOTE = ("FERC eLibrary text search by the researcher: the same host, endpoints, terms and politeness as "
              "the docket collector `ferc`, whose note follows. " + FERC_TERMS)
SOURCE = declare_source(SOURCE_ID, "FERC eLibrary text search (researcher)", "portal",
                        "https://elibrary.ferc.gov/eLibrary", 10080, TERMS_NOTE)


def text_body(phrase: str, since: date, until: date) -> dict[str, Any]:
    body = search_body("", since, until, 0)
    body.update({"searchText": f'"{phrase}"', "docketSearches": [], "resultsPerPage": PAGE_SIZE, "curPage": 0})
    return body


def text_url(phrase: str, since: date, until: date) -> str:
    """The document identity of one text search (the exact POST body is in the document's meta)."""
    return f"{SEARCH_URL}#text={phrase}&posted={since.isoformat()}..{until.isoformat()}"


def rank(filings: list[Filing]) -> list[Filing]:
    """Commission issuances first, then filings the docket collector would also read, then the rest."""
    return sorted(filings, key=lambda f: (f.category != "Issuance", not f.queue_reasons(), -(f.day.toordinal())))


class FercSearch:
    def __init__(self, client: ElibraryClient | None = None):
        self.client = client or ElibraryClient(SOURCE_ID)

    @property
    def requests(self) -> int:
        return self.client.requests

    def search(self, phrase: str, since: date, until: date) -> tuple[DocumentRow, int, list[Filing]]:
        sources.require_collectable(SOURCE_ID)
        body = text_body(phrase, since, until)
        resp = self.client.post(SEARCH_URL, body, expect_json=True)
        doc = documents.record_document(
            SOURCE_ID, text_url(phrase, since, until), resp.content, content_type=resp.content_type,
            doc_type="ferc_search", title=f'eLibrary text search "{phrase}" posted {since}..{until}'[:300],
            doc_date=until, meta={"producer": "agent:researcher", "phrase": phrase,
                                  "http": {"status": resp.status, "method": "POST", "endpoint": SEARCH_URL,
                                           "body": body}})
        total, filings = parse_search(resp.content)
        return doc, total, rank(filings)

    def fetch(self, filing: Filing, main: FileRef, meta: dict[str, Any]) -> DocumentRow:
        return self.client.download(filing, main, meta={**filing.as_meta(), "producer": "agent:researcher", **meta},
                                    extract_state="none")
