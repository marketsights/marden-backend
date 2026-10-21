"""The names a record must carry to be about a financed site (Completion's 'Last evidence').

A site is known by its own name, its SPV names (`tracking_names`), its linked campuses and plants,
and its chain parties (tenant, end user, developer, lenders: the chain label, the party entity's
name and aliases). A record names the site when its text carries one of those names as a whole
phrase; a place name used for the county or town government ('Childress County', 'City of
Abilene', 'Saline, Township') does not count. A chain party's own SEC filing (its CIK in the
EDGAR URL) is that party's record even when its title names no one ('8-K: Other Events').
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from marden.core.db import Conn, Row

US_STATE_NAMES = frozenset((
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
    "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska",
    "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming", "puerto rico",
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky",
    "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh",
    "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "pr", "us", "usa",
))
# Words that name no one: kinds of thing, not names ('data center', 'Phase II', 'SPV', 'GPU loans').
GENERIC = frozenset((
    "project", "projects", "stargate", "data", "center", "centre", "centers", "campus", "campuses", "site", "sites",
    "phase", "building", "buildings", "loans", "loan", "gpu", "gpus", "abs", "ddtl", "servers", "server", "spv",
    "spvs", "company", "entity", "issuer", "issuers", "co-issuer", "construction", "holdings", "holdco", "the",
    "and", "of", "for", "with", "ai", "llc", "inc", "lp", "ltd", "i", "ii", "iii", "iv", "area", "plants", "plant",
    "lateral", "generation", "parent", "funds", "fund", "affiliated", "tactical", "opportunities", "credit",
    "insurance", "agent", "presumed", "assumed", "verify", "group", "jv",
))
# Chain labels that name nobody ('Noteholders', 'About 20 banks', 'Colocation customers').
PLACEHOLDER = re.compile(r"\b(?:holders?|noteholders|bondholders|banks|customers|hyperscalers|not named)\b", re.I)
CORPORATE = re.compile(r",?\s+(?:Inc|Corp|Corporation|Ltd|Limited|LLC|L\.L\.C|LP|L\.P|plc|Co|Pty)\.?$", re.I)
# A part of a name that is a county, parish or township, or a street address, is a place, not the site.
PLACE_PART = re.compile(r"\b(?:County|Parish|Township|Twp|City of|Town of)\b|^\W*\d", re.I)
SPLIT = re.compile(r"[()\[\]\"“”‘’;/,~]|'(?=\s|$)|(?<=\s)'| [-–—] | → | w/ | with ")
QUALIFIER = re.compile(r"\s+(?:Phase|Building|Buildings|Unit|Units|Horizon)\b.*$", re.I)
CIK_URL = re.compile(r"/edgar/data/0*(\d+)/", re.I)


def _trim(part: str) -> str:
    part = re.sub(r"\s+", " ", part).strip(" .-–:")
    while (cut := CORPORATE.sub("", part).strip()) != part:
        part = cut
    part = QUALIFIER.sub("", part).strip()
    words = part.split(" ")
    while len(words) > 1 and (words[-1].lower() in GENERIC or words[-1].lower() in US_STATE_NAMES
                              or re.fullmatch(r"[\d.\-–]+[A-Za-z]?", words[-1])):
        words.pop()  # 'Polaris Forge 2' -> 'Polaris Forge', 'Port Washington WI' -> 'Port Washington'
    while len(words) > 1 and words[0].lower() in GENERIC:
        words.pop(0)  # 'The Barn' stays (two words); 'Project Jupiter' is kept whole below
    return " ".join(words)


def _usable(name: str) -> bool:
    low = name.lower()
    if not name or PLACE_PART.search(name) or low in US_STATE_NAMES or low in GENERIC:
        return False
    if " " not in name:
        return len(name) >= 4 or (len(name) >= 3 and name.isupper())
    return not all(w.lower() in GENERIC for w in name.split(" "))


def phrases(text: str | None, drop_leading: Iterable[str] = ()) -> set[str]:
    """Name phrases in a site, campus, plant or SPV name: each part ('Project Jupiter (STACK/Oracle,
    Stargate New Mexico), Santa Teresa' -> Project Jupiter, STACK, Oracle, Stargate New Mexico, Santa
    Teresa) and that part without a leading party or generic word ('IREN Childress' -> Childress,
    'Stargate Abilene' -> Abilene; 'Stargate New Mexico' stays whole: 'New Mexico' is a state)."""
    out: set[str] = set()
    leaders = sorted({d for d in drop_leading if d} | {"Stargate", "Project"}, key=len, reverse=True)
    for raw in SPLIT.split(text or ""):
        whole = re.sub(r"\s+", " ", raw).strip(" .-–:")
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
        text = re.sub(r"\(.*?\)", " ", text)
        for part in re.split(r"\s*(?:/|,|;| and | with | including )\s*", text):
            part = re.sub(r"\s+", " ", part).strip(" .-–:")
            if PLACEHOLDER.search(part):
                continue
            for name in (part, _trim(part)):
                if _usable(name):
                    out.add(name)
    return out


@dataclass(frozen=True)
class SiteNames:
    pattern: re.Pattern[str] | None
    ciks: frozenset[str]

    def named_in(self, *texts: str | None) -> bool:
        return self.pattern is not None and any(t and self.pattern.search(t) for t in texts)

    def party_filing(self, url: str | None) -> bool:
        match = CIK_URL.search(url or "")
        return bool(match and match.group(1) in self.ciks)

    def about(self, url: str | None, *texts: str | None) -> bool:
        return self.named_in(*texts) or self.party_filing(url)


def names_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """Whole-phrase, case-insensitive; not when the phrase names the county / town government."""
    alts = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    if not alts:
        return None
    return re.compile(
        rf"(?<!City of )(?<!Town of )(?<!County of )(?<![\w&-])(?:{alts})(?![\w&-])"
        r"(?!,?\s+(?:County|Parish|Township|Twp\b|City Council|Co\.))",
        re.I,
    )


def site_names(conn: Conn, site: Row, campuses: list[Row], plants: list[Row]) -> SiteNames:
    rows = conn.execute(
        """SELECT cp.label, e.name, e.aliases, e.cik FROM chain_party cp LEFT JOIN entity e ON e.id = cp.entity_id
            WHERE cp.site_slug = %s""",
        (site["slug"],),
    ).fetchall()
    parties: set[str] = set()
    for r in rows:
        parties |= party_names(r["label"], r if r.get("name") else None)
    names = set(parties)
    for text in [site["name"], *(site.get("tracking_names") or []), *(c["name"] for c in campuses),
                 *(p["name"] for p in plants)]:
        names |= phrases(text, parties)
    ciks = frozenset(str(int(r["cik"])) for r in rows if r.get("cik") and str(r["cik"]).isdigit())
    return SiteNames(names_pattern(names), ciks)
