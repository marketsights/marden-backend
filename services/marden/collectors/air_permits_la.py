"""Louisiana adapter of `air_permits`: LDEQ air permit public notices for our Louisiana plants.

Where the records are (checked 2026-10-21): LDEQ's document system EDMS (edms.deq.louisiana.gov) is
robots-disallowed ("User-agent: * Disallow: /") and is never requested (`ldeq_edms`, blocked). The public notice
listing on www.deq.louisiana.gov (robots.txt: "User-Agent: * Disallow: /admin") is a table with a keyword and date
filter: company and facility, Agency Interest (AI) number, activity numbers, permit numbers (2460-00036-V0;
PSD-LA-869; 2460-00036-IV0), subject (e.g. "PROPOSED INITIAL PART 70 AIR OPERATING PERMIT, PREVENTION OF
SIGNIFICANT DETERIORATION (PSD) PERMIT, INITIAL ACID RAIN PERMIT"), media, publication date, comment deadline,
hearing date and issue date. Its attachments link into EDMS and are not followed.

Run: one listing keyword query per operator phrase of our Louisiana plants (stored raw when its rows changed) ->
the Air Quality rows -> one Facility per AI number with its permits and dated actions (notice published, hearing,
issued). The parish comes from the facility name ('Richland Parish 1 and 2 Power Station')
checked against the Census parish list. Rules: User-Agent "Marden research cody@marden.ai", one request per 2 s,
no retries, stop on 401/403/429 or a challenge page.
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlencode

from marden.collectors import air_permits_text as text
from marden.collectors.air_permits_match import (
    GENERATION,
    Action,
    Facility,
    Match,
    Permit,
    PlantProfile,
    counties_in,
)
from marden.collectors.air_permits_tx import get
from marden.collectors.iso_queue_match import county_words
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, documents, http
from marden.core.http import Fetcher
from marden.core.sources import declare_source

AGENCY = "LDEQ"
HOST = "www.deq.louisiana.gov"
LIST_URL = f"https://{HOST}/public-notices"
TERMS = (
    "Checked 2026-10-21. LDEQ public notices (https://www.deq.louisiana.gov/public-notices): robots.txt \"User-Agent: "
    "* Disallow: /admin\". The page: \"Permit records are available for review at the DEQ Public Records Center\". "
    "LDEQ privacy policy (https://www.deq.louisiana.gov/about-ldeq/privacy-page, 2022-07-25): \"LDEQ systems employ "
    "various security mechanisms for monitoring and protecting our systems from attempted or actual improper use\"; "
    "no reuse restriction is stated. The notices' attachments are on EDMS (edms.deq.louisiana.gov), whose "
    "robots.txt reads \"User-agent: * Disallow: /\": they are not requested. Marden: User-Agent \"Marden research "
    "cody@marden.ai\", one request per 2 s, one query per operator name, weekly; stops on any refusal or challenge."
)
SOURCE = declare_source("ldeq_public_notices", "LDEQ public notices", "portal", LIST_URL, 7 * 24 * 60, TERMS)
http.set_rate_limit(HOST, 0.5)
ROW = re.compile(r"<tr>\s*(<td>.*?)</tr>", re.S)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
COLUMNS = ("company", "ai", "activity", "permits", "subject", "media", "published", "comment_deadline", "hearing",
           "issued")
EGU = re.compile(r"\b(power station|generating (plant|station)|electric(al)? generating|acid rain)\b", re.I)


def rows(page: bytes) -> list[dict[str, str]]:
    body = page.decode("utf-8", errors="replace")
    i = body.find('id="pn-table-body"')
    if i < 0:
        raise ParseError("LDEQ public notices: no notice table on the page")
    out = []
    for m in ROW.finditer(body[i:]):
        cells = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split()) for c in CELL.findall(m.group(1))]
        if len(cells) >= len(COLUMNS):
            out.append(dict(zip(COLUMNS, cells, strict=False)))
    return out


def _parish_list() -> list[tuple[str, ...]]:
    with db.transaction() as conn:
        names = conn.execute("SELECT name FROM us_county WHERE stusps = 'LA'").fetchall()
    return [w for r in names if (w := county_words(r["name"]))]


def facility(ai: str, notices: list[dict[str, str]], parishes: list[tuple[str, ...]], url: str,
             document_id: int) -> Facility:
    name = notices[0]["company"]
    # 'Entergy Louisiana, LLC - X', '... LLC / X', '... LLC & X', '... LLC, X': the company comes first
    company = re.split(r"\s+[-/]\s+|(?<=\bLLC)\s+&\s+|,\s+(?=[A-Z][a-z])", name)[0].strip()
    parish = counties_in(name, parishes)
    ids = list(dict.fromkeys(p.strip() for n in notices for p in re.split(r"[;,]|\band\b", n["permits"]) if p.strip()))
    acts: list[Action] = []
    for n in notices:
        quote = " | ".join(n[k] for k in COLUMNS if n.get(k) and n[k] != "-")
        for key, kind in (("published", "notice"), ("hearing", "hearing"), ("issued", "final_action")):
            if (day := text.to_date(n.get(key))) is not None:
                what = f"{n['subject'].capitalize()} ({key.replace('_', ' ')})"
                acts.append(Action(day, kind, what, url, quote=quote))
    issued = sorted(a.day for a in acts if a.kind == "final_action")
    psd = next((i for i in ids if i.upper().startswith("PSD")), None)
    permit = Permit(
        AGENCY, psd or ids[0], ids, type="PSD" if psd else ("Title V" if any("-V" in i for i in ids) else None),
        status="issued" if issued else "pending", agency_status=notices[-1]["subject"].capitalize(),
        applicant=company, issued=issued[0] if issued else None, actions=sorted(acts, key=lambda a: a.day),
        url=url, quote=" | ".join(notices[0][k] for k in COLUMNS if notices[0].get(k) and notices[0][k] != "-"),
        quote_loc="LDEQ public notices listing", document_id=document_id,
    )
    gen = next((f"{n['company']}: {n['subject']}" for n in notices
                if EGU.search(n["company"] + " " + n["subject"]) or GENERATION.search(n["company"])), None)
    return Facility(SOURCE.id, AGENCY, "LA", ai, name, " ".join(w.title() for w in parish[0]) if parish else None,
                    [company], gen, [permit], url, document_id, quote=permit.quote)


def run_la(plants: list[PlantProfile], fetcher: Fetcher, stats: RunStats, info: dict[str, Any],
           match: Any) -> list[Match]:
    terms = sorted({" ".join(w) for p in plants for w, _ in p.phrases if len(" ".join(w)) >= 4})
    info["terms"] = terms
    by_ai: dict[str, list[dict[str, str]]] = {}
    where: dict[str, tuple[str, int]] = {}
    for term in terms:
        url = f"{LIST_URL}?{urlencode({'q': term})}"  # the listing's keyword search covers every year it keeps
        resp = get(fetcher, SOURCE.id, url, info)
        found = rows(resp.content)
        prior = documents.latest_document(SOURCE.id, url)
        if prior is not None and rows(prior.read()) == found:
            doc = prior  # same notices: the page differs only in markup tokens
        else:
            doc = documents.record_document(SOURCE.id, url, resp.content, content_type=resp.content_type,
                                            doc_type="ldeq_notices_html", title=f"LDEQ public notices: {term}",
                                            meta={"rows": len(found)})
        stats.document(doc)
        for n in found:
            if n["media"] == "Air Quality" and (ai := n["ai"].replace("AI", "").strip()):
                by_ai.setdefault(f"AI {ai}", []).append(n)
                where.setdefault(f"AI {ai}", (f"{LIST_URL}?{urlencode({'q': ai})}", doc.id))
    info["facilities"] = {ai: ns[0]["company"] for ai, ns in sorted(by_ai.items())}
    parishes = _parish_list()
    facilities = [facility(ai, sorted(ns, key=lambda n: n["published"]), parishes, *where[ai])
                  for ai, ns in sorted(by_ai.items())]
    return match(facilities)
