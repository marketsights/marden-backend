"""Is a docket record about the debt, or procedure? (site / plant evidence and event lists).

A regulator's or court's docket carries every paper filed in it. Most change nothing for a lender:
a law firm updating the service list, a utility noticing its appearance, a party joining the case
with no position, a corrected exhibit, a request for more time to answer, an affidavit that papers
were mailed, a compliance report that restates what an order already said, and the hundreds of
comments on a statewide or national rulemaking that do not name the site. Those records stay
stored (the document, the event row); the views just do not list them, nor pick one as the site's
'latest evidence'.

`procedural_reason(title)` says why a record is procedure (a short reason) or None when it may be
substantive. It reads the title only, by these patterns, in this order:

1. Always procedure (whatever else the title says): service-list and mailing-list updates;
   certificates / affidavits of service, publication or mailing; return receipts; cover letters;
   requests for a control number; letters closing a file.
2. Procedure unless the title also states something substantive (`CASE_SUBSTANCE`: a protest, an
   objection, a hearing date or schedule moved, a stay, a dismissal ...; and for papers that name no
   party, `PROJECT_SUBSTANCE`: a tariff or rate change, a construction / in-service deadline):
   notices of appearance and changes of counsel; motions or notices to intervene with no position
   (with comments, only in the site's own case); errata and corrections of testimony; requests for more
   time to file; informational / compliance reports with no tariff or rate change.
3. Comments in a generic proceeding (`generic=True`: a docket shared by several sites, or a
   national / statewide rulemaking by its docket number: FERC AD / RM / PL, ERCOT revision requests),
   unless they name the site (`named=True`).
4. A web page Marden snapshotted rather than a filing (with `url`): a body's home or section page or
   a form, with no date or number in its title or address.

Kept, whatever matches above: orders, rulings, decisions, notices of hearing or of denial,
protests, complaints, interconnection agreements, rate and tariff filings, withdrawals and
denials of an application; and every event a reviewer approved as material (callers check that).
"""

from __future__ import annotations

import re

# 1. Pure procedure.
ALWAYS = (
    ("service list update", re.compile(r"\bservice lists?\b|\bmailing lists?\b|\bmailing / service list\b", re.I)),
    ("proof of service", re.compile(
        r"\b(?:certificate|affidavit|proof|declaration) of (?:electronic |regular )?(?:service|mailing|publication)\b"
        r"|\baffidavit of service\b|\breturn receipt\b", re.I)),
    ("notice of publication", re.compile(r"\b(?:notice of publication|publication of notice)\b", re.I)),
    ("control number request", re.compile(r"\brequest for (?:a )?control number\b", re.I)),
    ("file closed", re.compile(r"\bletter closing\b.*\bfile\b", re.I)),
)

# Decisions are kept whatever else they say ('Order ... For errata see accession ...'); an errata
# notice to an order is not the order.
DECISION = re.compile(
    r"^(?:\(doc-less\)\s*)?(?:order|ruling|decision|final rule|notice of (?:denial|hearing|proposed rul\w*))\b"
    r"|^letter (?:order|informing)\b", re.I)

# 2. Procedure unless the title also says something that changes the case or the schedule.
UNLESS_SUBSTANCE = (
    # the cover letter itself ('[Comments] City of New York: Cover Letter'), not a filing that encloses one
    ("cover letter", re.compile(r"(?:^|[:\]]\s*)(?:\S+\s){0,3}cover letter\b(?!\s+and\b)", re.I)),
    ("appearance of counsel", re.compile(
        r"\b(?:notice|entry|entries) of appearance\b|\bnotice of participation\b"
        r"|\b(?:substitution|withdrawal|removal|change|designation|addition) of (?:\w+\s){0,6}?"
        r"(?:counsel|attorneys?|representatives?|contact person)\b"
        r"|\bas counsel\b|\bnotice of (?:change of )?(?:attorney|counsel)\b", re.I)),
    ("intervention", re.compile(
        r"\b(?:motion|petition|notice|request|application)s? (?:for leave )?(?:\w+\s){0,3}?to intervene\b"
        r"|\bnotice of intervention\b|\brequest for intervention\b|\bmotion to intervene\b", re.I)),
    ("errata", re.compile(
        r"\berrat(?:a|um)\b|\brequest to correct\b"
        r"|\bcorrect(?:ed|ion|ions) (?:to|of) (?:the )?(?:\w+\s){0,2}?(?:testimony|rebuttal|exhibit|transcript|"
        r"filing|brief|comments?)\b", re.I)),
    ("extension of time", re.compile(
        r"\b(?:motion|request|application|letter)s?\b.{0,60}\b(?:extension of time|enlargement of time|"
        r"extension of (?:the )?(?:comment|answer|response|reply|filing) (?:period|deadline|date))\b", re.I)),
    ("informational filing", re.compile(
        r"\binformational (?:report|filing|update)\b|\bcompliance (?:report|filing)\b"
        r"|\bsubmits? (?:its )?(?:\w+\s)?report in compliance\b", re.I)),
)
# A filing that moves the case or its schedule: kept whatever kind of paper it is.
CASE_SUBSTANCE = re.compile(
    r"\bprotests?\b|\bobject(?:ion|s|ing)?\b|\boppos(?:e|es|ition)\b|\bcomplaint\b|\bhearing date\b"
    r"|\b(?:move|moving|postpone|reschedul\w*|continu(?:e|ance))\b.{0,30}\bhearing\b|\bschedul\w*\b|\bstay\b"
    r"|\bdismiss\w*\b|\bdenial\b|\bdeny\b|\bwithdraw(?:al|s|n)? (?:of|the) (?:its |the )?"
    r"(?:application|petition|request|permit|filing)\b|\bterminat\w*\b",
    re.I,
)
# What changes the project: its rates, its build dates. Read only on papers that carry no party name
# in the way (a compliance report, a request for more time, a cover letter): 'Motion to Intervene of
# Association of Businesses Advocating Tariff Equity' is not a tariff filing.
PROJECT_SUBSTANCE = re.compile(
    r"\bconstruct\w*\b|\bin[- ]service\b|\bcommercial operation\b|\bcomplet(?:e|ion)\b|\btariff\b|\brates?\b"
    r"|\bapprov\w*\b|\badjust\w*\b|\binterconnection agreement\b|\brevis\w*\b|\bamend\w*\b",
    re.I,
)
PARTY_PAPERS = frozenset({"appearance of counsel", "intervention", "errata"})

# An intervention that takes a position (comments, an answer, support) is not just joining the case.
POSITION = re.compile(r"\bcomments?\b|\banswer\b|\bsupport\b|\bresponse\b", re.I)

# 3. Comments, and the docket numbers of generic (non-site) proceedings.
COMMENT = re.compile(
    r"^\s*\[(?:COM|PC|Comments?|Public Comments?)\]|\bcomments?\b|\bfeedback\b|\bpublic comment\b", re.I)
GENERIC_DOCKET = re.compile(
    r"\b(?:AD|RM|PL)\d{2}-\d{1,5}\b"  # FERC administrative, rulemaking and policy dockets
    r"|^\s*\d{2,4}(?:PGRR|NPRR|NOGRR|RRGRR|SCR|OBDRR|SMOGRR|LPGRR|VCMRR|COPMGRR|RMGRR)\b",  # ERCOT revision requests
    re.I,
)
# 4. A web page Marden snapshotted, not a filing: a body's home or section page or a form
#    ('Saline, Township (Washtenaw, MI)' at salinetownship.org/, 'Zoning Board of Appeals', 'Committee
#    Application Form'): no date or number in its title or address, no document behind it.
PAGE_URL = re.compile(r"^https?://[^/?#]+(?:/[A-Za-z_\-]+){0,3}(?:/|\.(?:php|aspx?|html?))?$", re.I)
PAGE_SUBSTANCE = re.compile(
    r"\b(?:agenda|minutes|order|notice|hearing|vote|resolution|ordinance|permit|decision|ruling|meeting)\b", re.I)

# A comment that protests or complains stays (a protest is a party's case, not a rulemaking comment).
COMMENT_SUBSTANCE = re.compile(r"\bprotests?\b|\bcomplaint\b", re.I)


def web_page(title: str, url: str | None) -> bool:
    """A snapshotted web page (rule 4), not a dated filing. A news article's address ends in a long slug
    ('/applications-filed-for-four-more-buildings-in-cedar-creek-texas/') and a status line carries a
    colon ('PSCW: Refiled'): neither is a page snapshot."""
    if not (url and PAGE_URL.match(url.strip())) or re.search(r"\d", title) or ":" in title:
        return False
    slug = re.sub(r"\.(?:php|aspx?|html?)$", "", url.strip().rstrip("/").rsplit("/", 1)[-1], flags=re.I)
    path = re.sub(r"^https?://[^/?#]+", "", url.strip()).strip("/")
    words = [w.lower() for w in re.split(r"[-_]+", slug) if w] if path else []
    named = {w.lower() for w in re.findall(r"[A-Za-z]+", title)}
    # the page is the one its title names: the site root, or a section whose address is the title
    # ('Zoning Board of Appeals' at /zoning-board-of-appeals/), not an article or a document id
    return len(words) <= 4 and set(words) <= named and len(title.split()) <= 6 and not PAGE_SUBSTANCE.search(title)


def procedural_reason(title: str | None, *, generic: bool = False, named: bool = False,
                      url: str | None = None) -> str | None:
    """Why the record titled `title` is procedure (a short reason: 'service list update'), or None
    when it may be substantive (module doc). `generic`: the record sits in a proceeding that is not
    about this site (shared by several sites, or a rulemaking); `named`: its text names the site;
    `url`: the record's document, for rule 4 (a web page, not a filing)."""
    text = " ".join((title or "").split())
    if not text:
        return None
    for reason, pattern in ALWAYS:
        if pattern.search(text):
            return reason
    if DECISION.search(text):
        return None
    moves_case = CASE_SUBSTANCE.search(text) is not None
    moves_project = PROJECT_SUBSTANCE.search(text) is not None
    for reason, pattern in UNLESS_SUBSTANCE:
        if not pattern.search(text) or moves_case or (moves_project and reason not in PARTY_PAPERS):
            continue
        if reason == "intervention" and POSITION.search(text) and not (generic or GENERIC_DOCKET.search(text)):
            continue  # a party taking a position in the site's own case (in a rulemaking: still procedure)
        return reason
    if (generic or GENERIC_DOCKET.search(text)) and not named and COMMENT.search(text) \
            and not COMMENT_SUBSTANCE.search(text):
        return "comment in a generic proceeding"
    if web_page(text, url):
        return "web page, not a filing"
    return None


def is_procedural(title: str | None, *, generic: bool = False, named: bool = False, material: bool = False,
                  url: str | None = None) -> bool:
    """True when the record is procedure and no reviewer approved it as material."""
    return not material and procedural_reason(title, generic=generic, named=named, url=url) is not None
