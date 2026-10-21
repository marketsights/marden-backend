"""New Mexico adapter of `air_permits`: NMED Air Quality Bureau permit applications for our New Mexico plants.

Where the records are (checked 2026-10-21): NMED publishes every Air Quality Bureau permitting action as a post on
its public-notices site (https://www.env.nm.gov/public-notices/, WordPress): the post title names the applicant
and facility ('Yucca Growth Infrastructure, LLC – YGI Microgrid — (Project Jupiter)', '(Application Withdrawn
4/27/2026)') and links the application, completeness letter, the legal notice, drafts and hearing notices. The
legal notice ('Public Notice for Air Quality Construction Permit Application <no>') states the permit number,
the county, the equipment and the emission estimates. Contested applications also have an Office of Public
Facilitation docket post (https://www.env.nm.gov/opf/docketed-matters/) listing dated filings. Both sites answer
the WordPress REST API (/wp-json/wp/v2/search, /posts/<id>); robots.txt: "User-agent: * Disallow: /wp-admin/".
Linked files sit behind service.web.env.nm.gov short links (no robots.txt) that redirect to cloud.env.nm.gov.

Run: REST searches for the name and project phrases of our New Mexico plants -> each matching post (stored raw)
-> its English legal notice PDF (stored raw, read) -> one Facility per application, with dated actions from the
post's link texts and the docket's filings. Rules: User-Agent "Marden research cody@marden.ai", one request per
2 s, no retries, stop on 401/403/429 or a challenge page.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any
from urllib.parse import quote

from marden.collectors import air_permits_text as text
from marden.collectors.air_permits_match import GENERATION, Action, Facility, Match, Permit, PlantProfile
from marden.collectors.air_permits_tx import get
from marden.collectors.iso_queue_match import contains, tokens
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import documents, http
from marden.core.http import Fetcher
from marden.core.sources import declare_source

AGENCY = "NMED"
HOST = "www.env.nm.gov"
NOTICES = f"https://{HOST}/public-notices/wp-json/wp/v2"
DOCKETS = f"https://{HOST}/opf/wp-json/wp/v2"
TERMS = (
    "Checked 2026-10-21. NMED Air Quality Bureau public notices (https://www.env.nm.gov/public-notices/) and Office "
    "of Public Facilitation docketed matters (https://www.env.nm.gov/opf/docketed-matters/), both read through "
    "the WordPress REST API (/wp-json/wp/v2). www.env.nm.gov/robots.txt: \"User-agent: * Disallow: /wp-admin/ "
    "Allow: /wp-admin/admin-ajax.php\"; service.web.env.nm.gov (file short links) has no robots.txt. The notices "
    "say: \"The permit application is available for review on the AQB Public Notices of Permitting Actions website "
    "www.env.nm.gov/public-notices.\" Marden: User-Agent \"Marden research cody@marden.ai\", one request per 2 s, "
    "weekly; stops on any refusal or challenge."
)
SOURCE = declare_source("nmed_aqb", "NMED Air Quality Bureau public notices and dockets", "portal",
                        f"https://{HOST}/public-notices/", 7 * 24 * 60, TERMS)
http.set_rate_limit(HOST, 0.5)
http.set_rate_limit("service.web.env.nm.gov", 0.5)
http.set_rate_limit("cloud.env.nm.gov", 0.5)
NUMBER = re.compile(r"assigned Permit No\.?\s*(\d{3,6})")
APPLICANT = re.compile(r"\b([A-Z][\w&.,' -]{2,80}?(?:LLC|L\.L\.C\.|Inc\.?|Company|Corporation|LP))\s*(?:,)?\s+at\s+")
FACILITY = re.compile(r"permit to construct the [\"“]([^\"”]+?)\.?[\"”]")
COUNTY = re.compile(r"\bin\s+([A-Z][a-zñ]+(?:\s[A-Z][a-zñ]+)?)\s+County\b")
EMISSION = re.compile(r"(Nitrogen Oxides|Carbon Monoxide|Volatile Organic Compounds|Sulfur Dioxide|Particulate Matter "
                      r"10 microns or less|Particulate Matter 2\.5 microns or less|total Hazardous Air Pollutants)"
                      r"\s*\(([^)]+)\)\s*at\s*(>?\s*[\d,]+(?:\.\d+)?)\s*(?:tons per year\s*)?\(?tpy\)?", re.I)
LINK = re.compile(r"<a\s[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
SHORT_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})")


def search(fetcher: Fetcher, base: str, term: str, stats: RunStats, info: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{base}/search?search={quote(term)}&per_page=20&subtype=post"
    resp = get(fetcher, SOURCE.id, url, info)
    try:
        hits = json.loads(resp.content)
    except ValueError:
        raise ParseError(f"NMED search {term!r}: not JSON") from None
    doc = documents.record_document(SOURCE.id, url, resp.content, content_type="application/json",
                                    doc_type="wp_search_json", title=f"NMED search: {term}", meta={"hits": len(hits)})
    stats.document(doc)
    return [h for h in hits if isinstance(h, dict) and h.get("id")]


def post(fetcher: Fetcher, base: str, post_id: int, stats: RunStats, info: dict[str, Any]) -> tuple[dict[str, Any], int]:
    url = f"{base}/posts/{post_id}"
    resp = get(fetcher, SOURCE.id, url, info)
    data = json.loads(resp.content)
    doc = documents.record_document(SOURCE.id, url, resp.content, content_type="application/json", doc_type="wp_post_json",
                                    title=html.unescape(data.get("title", {}).get("rendered", "")),
                                    doc_date=(data.get("modified") or "")[:10] or None, external_id=str(post_id))
    stats.document(doc)
    return data, doc.id


def links(data: dict[str, Any]) -> list[tuple[str, str]]:
    body = data.get("content", {}).get("rendered", "")
    return [(u, " ".join(html.unescape(re.sub(r"<[^>]+>", "", t)).split())) for u, t in LINK.findall(body)]


def _short_date(s: str) -> date | None:
    m = SHORT_DATE.search(s)
    if not m:
        return None
    y = int(m.group(3))
    try:
        return date(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def notice_actions(items: list[tuple[str, str]]) -> list[Action]:
    out = []
    for url, label in items:
        if re.search(r"Spanish|Español|espa", label, re.I):
            continue
        day = _short_date(label)
        if day is None:
            continue
        kind = ("application" if re.search(r"Application.*received", label, re.I) else
                "completeness" if re.search(r"Completion Letter", label, re.I) else
                "draft" if re.search(r"^DRAFT", label, re.I) else
                "hearing" if re.search(r"Notice of Hearing", label, re.I) else None)
        if kind:
            out.append(Action(day, kind, label, url, quote=label))
    return out


def legal_notice(body: str) -> dict[str, Any]:
    flat = text.squash(body)
    number = NUMBER.search(flat)
    applicant = APPLICANT.search(flat)
    facility = FACILITY.search(flat)
    county = COUNTY.search(flat)
    gen = GENERATION.search(flat)
    sentence = next((s for s in re.split(r"(?<=\.)\s+", flat) if gen and gen.group(0) in s), None)
    emissions = [{"pollutant": m.group(2), "value": float(m.group(3).replace(">", "").replace(",", "").strip()),
                  "unit": "tpy", "less_than": False, "more_than": ">" in m.group(3), "quote": m.group(0)}
                 for m in EMISSION.finditer(flat)]
    return {
        "number": number.group(1) if number else None, "number_quote": number.group(0) if number else None,
        "applicant": applicant.group(1).strip() if applicant else None,
        "facility": facility.group(1).strip() if facility else None,
        "county": county.group(1) if county else None, "county_quote": county.group(0) if county else None,
        "generation": sentence, "units": text.unit_statements(flat), "emissions": emissions,
    }


def application(data: dict[str, Any], doc_id: int, fetcher: Fetcher, stats: RunStats, info: dict[str, Any]) -> Facility | None:
    title = " ".join(html.unescape(data.get("title", {}).get("rendered", "")).split())
    items = links(data)
    pn = next(((u, t) for u, t in items if re.match(r"Public Notice\b.*Application\s*\d*", t, re.I)
               and not re.search(r"Spanish|Español", t, re.I)), None)
    if pn is None:
        info.setdefault("posts_without_notice", []).append(title)
        return None
    doc = documents.latest_document(SOURCE.id, pn[0])
    if doc is None:
        resp = get(fetcher, SOURCE.id, pn[0], info)
        if not resp.content.startswith(b"%PDF"):
            raise ParseError(f"NMED legal notice {pn[0]} is not a PDF")
        doc = documents.record_document(SOURCE.id, pn[0], resp.content, content_type="application/pdf",
                                        doc_type="nmed_legal_notice", title=pn[1], extract_state="queued",
                                        meta={"post": data.get("link"), "final_url": resp.final_url})
    stats.document(doc)
    ln = legal_notice(text.pdf_text(doc.read()))
    if not ln["number"]:
        info.setdefault("notices_unread", []).append(pn[0])
        return None
    withdrawn = re.search(r"Application Withdrawn\s*([\d/]+)", title)
    acts = notice_actions(items)
    if withdrawn and (d := _short_date(withdrawn.group(1))):
        acts.append(Action(d, "withdrawal", withdrawn.group(0), data.get("link", ""), quote=title))
    project = re.search(r"\((Project [^)]+)\)", title)
    src = {"url": pn[0], "document_id": doc.id}
    permit = Permit(
        AGENCY, f"NSR {ln['number']}", [ln["number"]], type="NSR", status="withdrawn" if withdrawn else "pending",
        agency_status="Application Withdrawn" if withdrawn else "Application under review",
        applicant=ln["applicant"], filed=min((a.day for a in acts if a.kind == "application"), default=None),
        units=[dict(u, **src) for u in ln["units"]], emission_limits=[dict(e, **src) for e in ln["emissions"]],
        actions=sorted(acts, key=lambda a: a.day), url=data.get("link", pn[0]), quote=ln["number_quote"],
        quote_loc=f"NMED legal notice ({pn[1]})", document_id=doc.id,
    )
    names = [n for n in (ln["applicant"], ln["facility"]) if n]
    return Facility(SOURCE.id, AGENCY, "NM", f"NSR {ln['number']}", ln["facility"] or title, ln["county"], names,
                    ln["generation"], [permit], data.get("link", pn[0]), doc_id, quote=ln["county_quote"],
                    aliases=[project.group(1)] if project else [])


def docket_actions(fetcher: Fetcher, f: Facility, stats: RunStats, info: dict[str, Any]) -> None:
    """Dated filings of the facility's OPF docket (hearing notices, orders), found by its applicant name."""
    term = f.names[0] if f.names else f.name
    for hit in search(fetcher, DOCKETS, term, stats, info)[:3]:
        data, _ = post(fetcher, DOCKETS, int(hit["id"]), stats, info)
        title = " ".join(html.unescape(data.get("title", {}).get("rendered", "")).split())
        if not re.match(r"AQB \d\d-\d+", title):
            continue
        body = html.unescape(re.sub(r"<[^>]+>", " ", data.get("content", {}).get("rendered", "")))
        for url, label in links(data):
            m = re.search(re.escape(label) + r"\s+(\d{2}/\d{2}/\d{4})", " ".join(body.split()))
            if m and re.search(r"Notice of (Public )?Hearing|Notice of Docketing|Final Order|Report and Recommendation",
                               label, re.I) and not re.search(r"Spanish|Español", label, re.I):
                day = text.to_date(m.group(1))
                if day:
                    f.permits[0].actions.append(Action(day, "hearing" if "Hearing" in label else "docket",
                                                       f"{title.split(':')[0]}: {label}", url, quote=m.group(0)))
        f.permits[0].actions.sort(key=lambda a: a.day)


def run_nm(plants: list[PlantProfile], fetcher: Fetcher, stats: RunStats, info: dict[str, Any], match: Any) -> list[Match]:
    terms = sorted({" ".join(w) for p in plants for w, _ in p.phrases if len(" ".join(w)) >= 4}
                   | {f"project {t}" for p in plants for t in re.findall(r"project ([a-z]{4,})", " ".join(p.name_tokens))})
    info["terms"] = terms
    seen: dict[int, dict[str, Any]] = {}
    for term in terms:
        for hit in search(fetcher, NOTICES, term, stats, info):
            # the search also reads post bodies: keep posts whose title carries the phrase
            if contains(tokens(html.unescape(str(hit.get("title", "")))), tuple(term.split())):
                seen.setdefault(int(hit["id"]), hit)
    info["posts"] = {i: html.unescape(str(h.get("title"))) for i, h in sorted(seen.items())}
    facilities = []
    for post_id in sorted(seen):
        data, doc_id = post(fetcher, NOTICES, post_id, stats, info)
        if f := application(data, doc_id, fetcher, stats, info):
            facilities.append(f)
    matches = match(facilities)
    for m in matches:
        if m.plant is not None:
            docket_actions(fetcher, m.facility, stats, info)
    return matches
