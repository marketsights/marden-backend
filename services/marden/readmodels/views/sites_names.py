"""The names a record must carry to be about a financed site (Completion's 'Last evidence').

A site is known by its own name, its SPV names (`tracking_names`), its linked campuses, the name of
each plant serving them, and its chain parties (tenant, end user, developer, lenders: the chain
label, the party entity's name and aliases). A record names the site when its text carries one of
those names as a whole phrase, as written or in capitals ('Lake Mariner', 'ORACLE'; brand names such
as 'TeraWulf' in any case); a place name used for the county or town government ('Childress County',
'City of Abilene', 'Saline, Township') does not count. A record whose title names no one is still about
the site when its document is: the publisher's URL slug names it ('.../four-more-buildings-in-cedar-creek-
texas/'), it is a chain party's own SEC filing (the party's CIK in the EDGAR URL: '8-K: Other Events'), or
it is the source of the site's own debt terms (a fact of one of the site's instruments cites it).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from marden.core.db import Conn, Row
from marden.core.licensing import hidden_source_names

US_STATES = frozenset((
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
    "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska",
    "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming", "puerto rico",
))
STATE_CODE = re.compile(r"[A-Z]{2}")
# Words that name no one: kinds of thing, not names ('data center', 'Phase II', 'SPV', 'GPU loans').
GENERIC = frozenset((
    "project", "projects", "stargate", "data", "center", "centre", "centers", "campus", "campuses", "site", "sites",
    "phase", "building", "buildings", "loans", "loan", "gpu", "gpus", "abs", "ddtl", "servers", "server", "spv",
    "spvs", "company", "entity", "issuer", "issuers", "co-issuer", "construction", "holdings", "holdco", "the",
    "and", "of", "for", "with", "ai", "llc", "inc", "lp", "ltd", "i", "ii", "iii", "iv", "area", "plants", "plant",
    "lateral", "generation", "parent", "funds", "fund", "affiliated", "agent", "presumed", "assumed", "verify",
    "group", "jv", "compute", "finance", "member", "dc", "managing", "borrower", "facility", "investor", "us",
))
# Chain labels that name nobody ('Noteholders', 'About 20 banks', 'Colocation customers').
PLACEHOLDER = re.compile(r"\b(?:holders?|noteholders|bondholders|banks|customers|hyperscalers|not named)\b", re.I)
CORPORATE = re.compile(r",?\s+(?:Inc|Corp|Corporation|Ltd|Limited|LLC|L\.L\.C|LP|L\.P|plc|Co|Pty)\.?$", re.I)
# A county, parish or township, or a street address, is a place, not the site.
PLACE_PART = re.compile(r"\b(?:County|Parish|Township|Twp|City of|Town of)\b|^\W*\d", re.I)
SPLIT = re.compile(r"[()\[\]\"\u201c\u201d\u2018\u2019;/,~]|'(?=\s|$)|(?<=\s)'| [-\u2013\u2014] | \u2192 | w/ | with ")
QUALIFIER = re.compile(r"(?:^|\s+)(?:Phase|Building|Buildings|Unit|Units|Horizon)\b.*$", re.I)
STRIP = " .-\u2013:"  # spaces, dots, hyphens, en dashes and colons around a name part
CIK_URL = re.compile(r"/edgar/data/0*(\d+)/", re.I)
GOVERNMENT_AFTER = r"(?!(?i:,?\s+(?:county|parish|township|twp\b|city|town\b|village|board|common council|" \
                   r"plan(?:ning)? commission|zoning|isd\b)))"


def _trim(part: str) -> str:
    part = re.sub(r"\s+", " ", part).strip(STRIP)
    while (cut := CORPORATE.sub("", part).strip()) != part:
        part = cut
    words = QUALIFIER.sub("", part).strip().split(" ")
    while len(words) > 1 and (words[-1].lower() in GENERIC or STATE_CODE.fullmatch(words[-1])
                              or re.fullmatch(r"[\d.\-\u2013]+[A-Za-z]?", words[-1])):
        words.pop()  # 'Polaris Forge 2' -> 'Polaris Forge', 'Memphis TN' -> 'Memphis'
    while len(words) > 1 and words[0].lower() in GENERIC - {"the"}:
        words.pop(0)  # 'The Barn' stays whole
    return " ".join(words)


def _usable(name: str) -> bool:
    """A proper name: capitalised (or 'xAI'), not a state, a place part or a generic word."""
    if not name or PLACE_PART.search(name) or name.lower() in US_STATES or STATE_CODE.fullmatch(name):
        return False
    if all(w.lower() in GENERIC for w in name.split(" ")):
        return False
    capitals = sum(c.isupper() for c in name)
    if " " not in name:
        return (len(name) >= 4 and name[0].isupper()) or (len(name) >= 3 and capitals >= 2)
    return name[0].isupper()


def phrases(text: str | None, drop_leading: Iterable[str] = ()) -> set[str]:
    """Name phrases in a site, campus or SPV name: each part ('Project Jupiter (STACK/Oracle,
    Stargate New Mexico), Santa Teresa' -> Project Jupiter, STACK, Oracle, Stargate New Mexico, Santa
    Teresa) and that part without a leading party or generic word ('IREN Childress' -> Childress,
    'Stargate Abilene' -> Abilene; 'Stargate New Mexico' stays whole: 'New Mexico' is a state)."""
    out: set[str] = set()
    leaders = sorted({d for d in drop_leading if d} | {"Stargate", "Project"}, key=len, reverse=True)
    for raw in SPLIT.split(text or ""):
        whole = re.sub(r"\s+", " ", raw).strip(STRIP)
        for part in {whole, _trim(whole)}:
            if _usable(part):
                out.add(part)
            for lead in leaders:
                if part.lower().startswith(lead.lower() + " "):
                    rest = _trim(part[len(lead) + 1:])
                    if _usable(rest):
                        out.add(rest)
    return out


def party_names(label: str | None, entity: Row | None) -> set[str]:
    """A chain party's names: the label's parts ('Crusoe / Blue Owl', 'MUFG; Morgan Stanley'), the
    entity's name, aliases and name without 'Inc.' / 'Corp.'; placeholders name no one."""
    texts = [label or ""]
    if entity:
        texts += [entity.get("name") or "", *(entity.get("aliases") or [])]
    out: set[str] = set()
    for text in texts:
        for part in re.split(r"\s*(?:/|,|;| and | with | including )\s*", re.sub(r"\(.*?\)", " ", text)):
            part = re.sub(r"\s+", " ", part).strip(STRIP)
            if not PLACEHOLDER.search(part):
                out.update(n for n in (part, _trim(part)) if _usable(n))
    return out


def names_pattern(names: Iterable[str], *, any_case: bool = False) -> re.Pattern[str] | None:
    """Whole phrases as written or in capitals; brand names with inner capitals ('TeraWulf',
    'CoreWeave') in any case; every name in any case with `any_case` (URL slugs are lower case).
    Not when the phrase names the county or town government."""
    names = set(names)
    brands = names if any_case else {
        n for n in names if " " not in n and any(c.isupper() for c in n[1:]) and any(c.islower() for c in n)}
    exact = {v for n in names - brands for v in (n, n.upper())}
    alts = [re.escape(n) for n in sorted(exact, key=len, reverse=True)]
    if brands:
        alts.append("(?i:" + "|".join(re.escape(n) for n in sorted(brands, key=len, reverse=True)) + ")")
    if not alts:
        return None
    return re.compile(rf"(?<!(?i:city of ))(?<!(?i:town of ))(?<!(?i:county of ))(?<![\w&-])"
                      rf"(?:{'|'.join(alts)})(?![\w&-]){GOVERNMENT_AFTER}")


def url_words(url: str | None) -> str:
    """The words of a URL's path ('/news/applications-filed-for-four-more-buildings-in-cedar-creek-texas/'
    -> 'news applications filed for four more buildings in cedar creek texas'): a publisher's slug is the
    record's own title."""
    path = re.sub(r"^[a-z]+://[^/]+", "", url or "", flags=re.I).split("?")[0]
    return re.sub(r"[-_/.+%]+", " ", path).strip()


@dataclass(frozen=True)
class SiteNames:
    pattern: re.Pattern[str] | None
    slug_pattern: re.Pattern[str] | None
    ciks: frozenset[str]
    sources: frozenset[str]  # documents behind the facts of the site's own instruments

    def named_in(self, *texts: str | None) -> bool:
        return self.pattern is not None and any(t and self.pattern.search(t) for t in texts)

    def party_filing(self, url: str | None) -> bool:
        match = CIK_URL.search(url or "")
        return bool(match and match.group(1) in self.ciks)

    def strength(self, url: str | None, *texts: str | None) -> int | None:
        """0: the record's text names the site or a party; 1: its document does (URL slug, a party's
        own SEC filing, the source of the site's debt terms); None: not about the site."""
        if self.named_in(*texts):
            return 0
        slug = url_words(url)
        if (self.slug_pattern is not None and slug and self.slug_pattern.search(slug)) or self.party_filing(url):
            return 1
        return 1 if url and url in self.sources else None


def site_names(conn: Conn, site: Row, campuses: list[Row], plants: list[Row], known_by: date) -> SiteNames:
    """The site's names (above). A plant name is a description ('Entergy Louisiana Hyperion CCGTs
    (first 3 plants, Richland/Franklin area)'): only its lead part before the first bracket counts."""
    rows = conn.execute(
        """SELECT cp.label, e.name, e.aliases, e.cik FROM chain_party cp LEFT JOIN entity e ON e.id = cp.entity_id
            WHERE cp.site_slug = %s""",
        (site["slug"],),
    ).fetchall()
    parties: set[str] = set()
    for r in rows:
        parties |= party_names(r["label"], r if r.get("name") else None)
    names = set(parties)
    texts = [site["name"], *(site.get("tracking_names") or []), *(c["name"] for c in campuses),
             *(re.split(r"[(,;]| [-\u2013\u2014] ", p["name"])[0] for p in plants)]
    for text in texts:
        names |= phrases(text, parties)
    ciks = frozenset(str(int(r["cik"])) for r in rows if r.get("cik") and str(r["cik"]).isdigit())
    sources = conn.execute(
        """SELECT DISTINCT source_url FROM fact WHERE subject_type = 'instrument' AND source_url IS NOT NULL
             AND subject_id IN (SELECT id FROM instrument WHERE site_slug = %s)
             AND status IN ('published', 'superseded') AND learned_at < %s::date AND source_name <> ALL(%s)""",
        (site["slug"], known_by, hidden_source_names()),
    ).fetchall()
    return SiteNames(names_pattern(names), names_pattern(names, any_case=True), ciks,
                     frozenset(r["source_url"] for r in sources))
