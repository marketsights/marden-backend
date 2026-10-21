"""Texas adapter of `air_permits`: TCEQ air authorizations for our Texas plants.

Where the records are, and what Marden may read (checked 2026-10-21):

- TCEQ's own permit web applications sit on hosts whose robots.txt reads "User-agent: * Disallow: /":
  www2.tceq.texas.gov (Air Permits Allowable Database, NSR project reports), www14 (Commissioners' Integrated
  Database), www15 (Central Registry query), www17. Marden does not crawl them (`tceq_air_permits_web`, blocked).
- The Central Registry is also published by TCEQ on the Texas Open Data Portal (data.texas.gov, Socrata API):
  five regional "Central Registry Files" data sets, one row per regulated entity x customer x program id. The
  AIRNSR (NSR permits, standard permits, PBR registrations, PSDTX / GHGPSDTX) and AIROP (federal operating
  permit) rows give the permit numbers, their status (ACTIVE / PENDING / CANCELLED), the regulated entity (RN),
  its county and description, and the customers (applicant / owner names). Read with SoQL, one query per data
  set and run (`tceq_cr_open_data`).
- TCEQ Records Online (records.tceq.texas.gov, no robots.txt) holds the permit documents: applications, technical
  reviews, notices, final-action letters. Its search page registers each search session through the page's own
  TCEQ_SEARCH_ADD_ACCESS call (Marden makes it as the page does, with an empty client IP) and then answers GET
  searches; files come from TCEQ_EXTERNAL_SEARCH_GET_FILE (`tceq_records_online`).

TCEQ Website Policies: "Unless otherwise noted, content of our site is considered "public domain."" and "we
reserve the right to block their access" for activity that threatens availability. Rules here: User-Agent
"Marden research cody@marden.ai", one request per 2 s to records.tceq.texas.gov and 1 per s to data.texas.gov
(its robots.txt Crawl-delay: 1), no retries; a 401/403/429 answer or a challenge page stops the run and is
recorded; a document already stored is never downloaded again.

Run: Central Registry rows for the names, numbers and project names of our Texas plants -> facilities (one per
RN) -> air_permits_match -> for each matched RN, Records Online searches by RN and by each permit number ->
permit families with their dated actions -> the newest technical review (else final-action letter) of up to
four projects per family, stored raw and read for units, emission totals, received date and expiry.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any
from urllib.parse import urlencode

from marden.collectors import air_permits_text as text
from marden.collectors import air_permits_tx_parse as tx
from marden.collectors.air_permits_match import Facility, Permit, PlantProfile
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, heartbeat, http, sources
from marden.core.documents import DocumentRow
from marden.core.http import Fetcher, FetchError, FetchResponse
from marden.core.sources import declare_source

USER_AGENT = "Marden research cody@marden.ai"
CR_HOST = "data.texas.gov"
RO_HOST = "records.tceq.texas.gov"
CR_DATASETS = {  # TCEQ Central Registry Files on the Texas Open Data Portal, by TCEQ region group
    "msah-s2rv": "Central Texas", "5eqq-7nad": "North Texas", "t34q-qzi3": "Dallas / Fort Worth",
    "tzyg-j7q4": "Coastal & East Texas", "9iad-hrn8": "Border & Permian Basin",
}
CR_URL = "https://data.texas.gov/resource/{}.json"
STOP_STATUSES = frozenset({401, 403, 429})
CR_LIMIT = 5000
CR_PAGES = 4
PROJECTS_PER_FAMILY = 4
NOT_PUBLIC = "tceq_not_public_html"
MAX_DOWNLOADS = 60  # per run; the rest wait for the next weekly run (stored documents are never refetched)
TERMS_CR = (
    "Checked 2026-10-21. TCEQ Central Registry data as published by TCEQ on the Texas Open Data Portal "
    "(data.texas.gov, Socrata), data sets 'Texas Commission on Environmental Quality - Central Registry Files - "
    "<region>' (msah-s2rv, 5eqq-7nad, t34q-qzi3, tzyg-j7q4, 9iad-hrn8), updated daily. data.texas.gov robots.txt: "
    "'User-agent: * Crawl-delay: 1' with Disallow rules only for /browse search pages; the /resource API is not "
    "disallowed. TCEQ Website Policies (https://www.tceq.texas.gov/help/policies/linking_policy.html): \"Unless "
    "otherwise noted, content of our site is considered \"public domain.\"\" The Central Registry query pages on "
    "www15.tceq.texas.gov are robots-disallowed ('Disallow: /') and are not used. Marden: User-Agent \"Marden "
    "research cody@marden.ai\", 1 request/s, 5 queries per weekly run."
)
TERMS_RO = (
    "Checked 2026-10-21. TCEQ Records Online (https://records.tceq.texas.gov/cs/idcplg?IdcService=TCEQ_SEARCH): "
    "no robots.txt (404). TCEQ Website Policies: \"Unless otherwise noted, content of our site is considered "
    "\"public domain.\"\"; Maintaining Public Access: \"if anyone deliberately or accidentally compromises or "
    "threatens to make our information unavailable to the public ... we reserve the right to block their "
    "access\". The search page registers each search through its own TCEQ_SEARCH_ADD_ACCESS call (Marden makes it "
    "as the page does, with an empty client IP; it is not a challenge). Marden: User-Agent \"Marden research "
    "cody@marden.ai\", one request per 2 s, no retries, stored documents never refetched, at most 60 downloads a "
    "run; a 401/403/429 or a challenge page stops the run."
)
TERMS_WEB = (
    "Checked 2026-10-21. robots.txt of www2.tceq.texas.gov (Air Permits Allowable Database, NSR project reports, "
    "airperm), www14 (Commissioners' Integrated Database), www15 (Central Registry query) and www17: "
    "\"User-agent: * Disallow: /\". Not collected; the same Central Registry data come from data.texas.gov and the "
    "permit documents from records.tceq.texas.gov."
)
SOURCE_CR = declare_source("tceq_cr_open_data", "TCEQ Central Registry (Texas Open Data Portal)", "api",
                           "https://data.texas.gov", 7 * 24 * 60, TERMS_CR)
SOURCE_RO = declare_source("tceq_records_online", "TCEQ Records Online", "portal", "https://records.tceq.texas.gov",
                           7 * 24 * 60, TERMS_RO)
SOURCE_WEB = declare_source("tceq_air_permits_web", "TCEQ air permit web applications (APAD, CID, Central "
                            "Registry query)", "portal", "https://www2.tceq.texas.gov", None, TERMS_WEB, blocked=True,
                            blocked_reason="robots.txt 'User-agent: * Disallow: /' on www2, www14, www15, www17")
http.set_rate_limit(CR_HOST, 1.0)
http.set_rate_limit(RO_HOST, 0.5)
_CHALLENGE = re.compile(rb"captcha|cf-chl|Just a moment\.\.\.|Incapsula|Request unsuccessful", re.I)


class Stopped(FetchError):
    """The site refused or challenged a request: the run stops and nothing is worked around."""


def get(fetcher: Fetcher, source_id: str, url: str, info: dict[str, Any], **params: Any) -> FetchResponse:
    sources.require_collectable(source_id)
    info["requests"] = info.get("requests", 0) + 1
    try:
        resp = fetcher.get(url, params=params or None, headers={"User-Agent": USER_AGENT}, use_cache=False)
    except FetchError as exc:
        if exc.status in STOP_STATUSES:
            raise Stopped(exc.url, exc.status, f"HTTP {exc.status}; run stopped, not retried") from None
        raise
    if _CHALLENGE.search(resp.content[:20000]) and not resp.content.startswith(b"%PDF"):
        raise Stopped(resp.url, resp.status, "challenge or CAPTCHA page; run stopped, not worked around")
    return resp


# ---------------------------------------------------------------- Central Registry (discovery)
def _soql_terms(plants: list[PlantProfile]) -> tuple[list[str], list[str]]:
    words: set[str] = set()
    for p in plants:
        words |= {" ".join(w).upper() for w, _ in p.phrases}
        words |= {" ".join(w).upper() for w in _project_phrases(p)}
    numbers = sorted({n for p in plants for n in p.numbers if re.fullmatch(r"[A-Z0-9]+", n)})
    return sorted(w for w in words if len(w) >= 3), numbers


def _project_phrases(p: PlantProfile) -> list[tuple[str, ...]]:
    """Quoted or 'Project X' names inside the plant name ('Project Llano', 'Project Matador')."""
    out = []
    for m in re.finditer(r"\bproject\s+([a-z0-9]+)", " ".join(p.name_tokens)):
        if len(m.group(1)) >= 4:
            out.append(("project", m.group(1)))
    return out


def cr_query(terms: list[str], numbers: list[str]) -> str:
    def q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"
    like = [f"upper({c}) like {q('%' + t + '%')}" for t in terms for c in ("princ_name", "reg_ent_name")]
    if numbers:
        like.append(f"additional_id_text in ({', '.join(q(n) for n in numbers)})")
    return f"program_code in ('AIRNSR', 'AIROP') AND ({' OR '.join(like)})"


def discover(plants: list[PlantProfile], fetcher: Fetcher, stats: RunStats, info: dict[str, Any]) -> list[Facility]:
    terms, numbers = _soql_terms(plants)
    info.update(terms=terms, numbers=numbers)
    where = cr_query(terms, numbers)
    out: list[Facility] = []
    for ds, region in CR_DATASETS.items():
        for page in range(CR_PAGES):
            params = {"$where": where, "$limit": str(CR_LIMIT), "$offset": str(page * CR_LIMIT),
                      "$order": "ref_num_txt,program_code,additional_id_text,ref_num_txt_1,princ_name"}
            url = CR_URL.format(ds) + "?" + urlencode(params)
            resp = get(fetcher, SOURCE_CR.id, url, info)
            try:
                rows = json.loads(resp.content)
            except ValueError:
                raise ParseError(f"Central Registry {ds}: not JSON") from None
            if not isinstance(rows, list):
                raise ParseError(f"Central Registry {ds}: unexpected payload {str(rows)[:200]}")
            doc = documents.record_document(SOURCE_CR.id, url, resp.content, content_type="application/json",
                                            doc_type="cr_rows_json", meta={"dataset": ds, "rows": len(rows)},
                                            title=f"TCEQ Central Registry Files - {region} (air), page {page + 1}")
            stats.document(doc)
            info.setdefault("cr_rows", {})[ds] = info.get("cr_rows", {}).get(ds, 0) + len(rows)
            out += tx.facilities(rows, url=url, document_id=doc.id, system=SOURCE_CR.id, dataset=ds)
            if len(rows) < CR_LIMIT:
                break
        else:
            info.setdefault("cr_truncated", []).append(ds)
    return out


# ---------------------------------------------------------------- Records Online (documents of matched RNs)
class RecordsOnline:
    def __init__(self, fetcher: Fetcher, stats: RunStats, info: dict[str, Any]):
        self.fetcher, self.stats, self.info = fetcher, stats, info
        self.session = False
        self.downloads = 0
        self._searched: dict[tuple[str, str], list[tx.RoRow]] = {}

    def _open(self) -> None:
        if not self.session:  # the search page sets the session cookie; ADD_ACCESS registers the search
            get(self.fetcher, SOURCE_RO.id, tx.RO_BASE, self.info, IdcService="TCEQ_SEARCH")
            get(self.fetcher, SOURCE_RO.id, tx.RO_BASE, self.info, IdcService="TCEQ_SEARCH_ADD_ACCESS", clientIP="",
                searchType="External", IsJson="1")
            self.session = True

    def search(self, field: str, value: str) -> list[tx.RoRow]:
        if (field, value) in self._searched:
            return self._searched[(field, value)]
        self._open()
        query = f"dSecurityGroup <matches> `Public`<AND>({field} <substring> `{value}`)"
        params = {"IdcService": "TCEQ_PERFORM_SEARCH", "QueryText": query, "SortField": "dInDate", "SortOrder": "Desc",
                  "ResultCount": "200", "SearchEngineName": "ELASTICSEARCH", "SearchQueryFormat": "UNIVERSAL",
                  "IsExternalSearch": "1"}
        url = tx.RO_BASE + "?" + urlencode(params)
        resp = get(self.fetcher, SOURCE_RO.id, url, self.info)
        if b"Search Results" not in resp.content:
            raise ParseError(f"Records Online answered {field}={value} without a result page")
        rows = tx.ro_rows(resp.content)
        prior = documents.latest_document(SOURCE_RO.id, url)
        if prior is not None and tx.row_signature(tx.ro_rows(prior.read())) == tx.row_signature(rows):
            self.stats.document(prior)  # same rows: the page differs only in its per-request tokens
        else:
            doc = documents.record_document(SOURCE_RO.id, url, resp.content, content_type=resp.content_type,
                                            doc_type="ro_search_html", title=f"TCEQ Records Online: {field} {value}",
                                            meta={"rows": len(rows), "field": field, "value": value})
            self.stats.document(doc)
        self._searched[(field, value)] = rows
        return rows

    def file(self, row: tx.RoRow, *, queue: bool = True) -> DocumentRow | None:
        """The stored copy of a Records Online file, else the file downloaded once (PDFs only). Files of matched
        permits are queued for extraction; files read only to check a near miss are not."""
        if doc := documents.latest_document(SOURCE_RO.id, row.url):
            return doc if doc.doc_type != NOT_PUBLIC else None
        if self.downloads >= MAX_DOWNLOADS:
            self.info["downloads_deferred"] = self.info.get("downloads_deferred", 0) + 1
            return None
        self.downloads += 1
        resp = get(self.fetcher, SOURCE_RO.id, row.url, self.info)
        if not resp.content.startswith(b"%PDF"):
            # TCEQ answers a file without a public rendition with its metadata page: kept, so it is not asked again
            self.info.setdefault("not_public", []).append(row.content_id)
            self.stats.document(documents.record_document(
                SOURCE_RO.id, row.url, resp.content, content_type=resp.content_type, doc_type=NOT_PUBLIC,
                title=f"{row.series} {row.primary} project {row.secondary}: {row.title} (no public rendition)",
                doc_date=row.begin, external_id=row.content_id))
            return None
        doc = documents.record_document(
            SOURCE_RO.id, row.url, resp.content, content_type="application/pdf", doc_type="tceq_" + _slug(row.title),
            title=f"{row.series} {row.primary} project {row.secondary}: {row.title}", doc_date=row.begin,
            external_id=row.content_id, extract_state="queued" if queue else "none",
            meta={"series": row.series, "primary_id": row.primary, "project": row.secondary, "title": row.title,
                  "description": row.description})
        self.stats.document(doc)
        return doc


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def enrich(f: Facility, ro: RecordsOnline) -> Facility:
    """Permit families of a matched regulated entity, with dates, units and emission totals from its records."""
    cr = {p.number: p for p in f.permits}
    rows: dict[str, tx.RoRow] = {}
    for r in ro.search("xRefNumTxt", f.key):
        rows[r.content_id + r.primary] = r
    for pid in sorted(cr):
        number = pid[1:] if pid.startswith("O") and pid[1:].isdigit() else pid
        if number.isdigit():
            for r in ro.search("xPrimaryID", number):
                if r.primary == number:
                    rows[r.content_id + r.primary] = r
    fams = tx.families(list(rows.values()))
    permits = [family_permit(fam, cr, f, ro) for fam in fams if _belongs(fam, cr)]
    known = {i for p in permits for i in p.ids} | {f"O{i}" for p in permits for i in p.ids if i.isdigit()}
    for n, p in sorted(cr.items()):
        if n in known:
            continue
        # A HAP / PSD number with no documents of its own joins the one PSD permit of the same status.
        hosts = [q for q in permits if q.type == "PSD" and q.agency_status == p.agency_status]
        if re.match(r"^(HAP|PSDTX|GHGPSDTX)\d+$", n) and len(hosts) == 1:
            hosts[0].ids.append(n)
        else:
            permits.append(p)  # in the Central Registry, no documents yet
    f.permits = sorted(permits, key=lambda p: p.number)
    return f


def _belongs(fam: tx.Family, cr: dict[str, Permit]) -> bool:
    ids = set(fam.ids) | ({f"O{i}" for i in fam.ids} if fam.fop else set())
    return bool(ids & set(cr))


def family_permit(fam: tx.Family, cr: dict[str, Permit], f: Facility, ro: RecordsOnline) -> Permit:
    ids = [f"O{i}" if fam.fop and i.isdigit() else i for i in fam.ids]
    main = cr.get(fam.number) or next((cr[i] for i in ids if i in cr), None)
    acts = tx.actions(fam)
    reads = []
    psd = any(i.startswith("PSDTX") for i in fam.ids)
    for project in sorted((p for p in fam.projects if p != "-"), reverse=True)[:PROJECTS_PER_FAMILY]:
        docs = tx.project_rows(fam, project)
        # the newest public technical review (each id of a PSD family files its own copy; drafts may be the only
        # public one), and for a standard permit or registration also its final-action letter (expiry, MAERT)
        for wanted in ("Technical Review", None if psd else "Final Action"):
            for row in sorted((r for r in docs if r.title == wanted), key=lambda r: r.begin or date.min,
                              reverse=True)[:3]:
                if doc := ro.file(row):
                    reads.append((project, row, doc))
                    break
    permit = Permit(
        tx.AGENCY, fam.number, ids, type="Title V" if fam.fop else ("PSD" if any(i.startswith("PSDTX") for i in ids)
                                                                     else None),
        status=main.status if main else None, agency_status=main.agency_status if main else None,
        applicant=main.applicant if main else None, url=main.url if main else "", quote=main.quote if main else None,
        quote_loc="TCEQ Central Registry Files (data.texas.gov)", document_id=main.document_id if main else None,
        actions=acts,
    )
    if any(a.kind == "withdrawal" for a in acts) and permit.status in (None, "cancelled", "inactive"):
        permit.status = "withdrawn"
    decided = [a for a in acts if a.kind == "final_action"]
    permit.issued = decided[0].day if decided and permit.status != "pending" else None
    first = min((p for p in fam.projects if p != "-"), default=None)
    for project, row, doc in sorted(reads, key=lambda x: x[1].begin or date.min):
        read_document(permit, project, row, doc, first=project == first)
    if permit.filed is None:
        apps = [a.day for a in acts if a.kind == "application" and a.project == first]
        permit.filed = min(apps) if apps else None
    if permit.filed and permit.issued and permit.filed > permit.issued:
        permit.filed = None  # an amendment's application, not the permit's
    return permit


def read_document(permit: Permit, project: str, row: tx.RoRow, doc: DocumentRow, *, first: bool = False) -> None:
    """Fill the permit from one stored TCEQ document (oldest first, so the newest project's units win). Only the
    permit's first project dates its application."""
    body = text.pdf_text(doc.read())
    if not body:
        return
    src = {"url": row.url, "document_id": doc.id, "project": project, "record": f"{row.title} {row.begin}"}
    if row.title == "Technical Review":
        tr = tx.technical_review(body)
        permit.type = permit.type or tr["type"]
        if first and tr["received"]:
            permit.filed = tr["received"]
        if tr["header"].get("Company") and not permit.applicant:
            permit.applicant = tr["header"]["Company"]
        if tr["units"]:
            permit.units = [dict(u, **src) for u in tr["units"]]
        if tr["emissions"]:
            permit.emission_limits = [dict(e, **src) for e in tr["emissions"]]
        for a in permit.actions:
            if a.project == project and a.kind == "final_action" and tr["project_type"]:
                a.text = f"{a.text} ({tr['project_type']})"
    else:
        fa = tx.final_action(body)
        permit.expires = fa["expires"] or permit.expires
        if first and fa["applied"] and permit.filed is None:
            permit.filed = fa["applied"]
        if fa["units"] and not permit.units:
            permit.units = [dict(u, **src) for u in fa["units"]]
        if fa["emissions"] and not permit.emission_limits:
            permit.emission_limits = [dict(e, **src) for e in fa["emissions"]]
        if fa["statement"]:
            for a in permit.actions:
                if a.project == project and a.kind == "final_action":
                    a.quote, a.document_id = fa["statement"], doc.id


def generation_from_records(f: Facility, ro: RecordsOnline) -> str | None:
    """For a record that would match but for power generation: the newest technical review or preliminary
    determination of its NSR permits, read for a sentence that states generation (turbines, engines, EGUs)."""
    rows = [r for r in ro.search("xRefNumTxt", f.key) if "New Source Review" in r.series
            and r.title in ("Technical Review", "Preliminary Determination Summary")]
    for row in sorted(rows, key=lambda r: (r.begin or date.min, r.content_id), reverse=True)[:2]:
        doc = ro.file(row, queue=False)
        body = text.squash(text.pdf_text(doc.read())) if doc else ""
        if m := re.search(r"[^.]*\b(power plant|electric generating units?|combustion turbines?|turbines?|"
                          r"reciprocating (internal combustion )?engines?|generate (electricity|power))\b[^.]*\.", body, re.I):
            return f"TCEQ {row.title} ({row.primary}, project {row.secondary}, {row.begin}): {m.group(0).strip()}"
    return None


def run_tx(plants: list[PlantProfile], fetcher: Fetcher, stats: RunStats, info: dict[str, Any],
           match: Any) -> list[Any]:
    """Central Registry discovery, the match, then Records Online (its own heartbeat): generation evidence for
    records that lack only that, and the permit documents of every matched record."""
    facilities = discover(plants, fetcher, stats, info)
    matches = match(facilities)
    near = [m for m in matches if m.plant is None and m.reasons == ["record shows no power generation"]]
    if not near and not any(m.plant for m in matches):
        return matches
    sources.upsert_spec(SOURCE_RO)
    ro_info: dict[str, Any] = {}
    info["records_online"] = ro_info
    with heartbeat.run(SOURCE_RO.id) as hb:
        ro = RecordsOnline(fetcher, stats, ro_info)
        try:
            for m in near[:10]:
                if g := generation_from_records(m.facility, ro):
                    m.facility.generation = g
                    ro_info.setdefault("generation_from_records", []).append(m.facility.key)
            if near:
                matches = match(facilities)
            tied = [m for m in matches if m.plant is not None]
            for m in tied:
                enrich(m.facility, ro)
            hb.seen(len(tied))
        finally:
            hb.meta = ro_info
    return matches
