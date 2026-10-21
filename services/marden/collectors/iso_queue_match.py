"""Match interconnection queue entries to Marden's sites, campuses and plants (rule queue-match@0.1).

A queue entry links to one of our subjects only when all of these hold:

1. the entry's state is one of the subject's states;
2. the entry's county (or parish) appears in the subject's county text;
3. a distinctive name phrase of the subject (its name, aliases, tracking/SPV names, or the
   developer, tenant or end-user named in its chain) appears, as whole words, in the entry's
   project name or developer / interconnection customer.

Phrases drop generic words (LLC, data, center, energy, project ...) and single common words
(lake, solar, north ...), so 'Lake Mariner' must appear as 'lake mariner'. Queues publish no
coordinates, so location agreement is county + state. Anything weaker is left unmatched. Every
link carries its evidence (which phrase, from which of our fields, matched which entry field).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from marden.core.db import Conn

METHOD_VERSION = "queue-match@0.1"
LINK_KINDS = ("site", "campus", "plant")  # subject types we link to; facts linked_<kind>

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
STATE_CODES = frozenset(STATE_NAMES.values())

# Words that say nothing about which project it is.
STOP = frozenset("""
llc l l c inc corp corporation co company lp llp ltd limited plc holdings holding holdco the of and a an
project projects campus campuses data center centers centre dc compute computing digital energy energies
power generation generating station facility facilities phase i ii iii iv v vi spv jv joint venture issuer
co-issuer managing construction entity entities site sites investor investors operating operations
development developers developer infrastructure partners group solutions technologies technology services
load interconnection request new expansion mw kv substation borrower facility presumed reported likely
""".split())
# A single remaining word must be at least this long and not a common place or resource word.
MIN_SINGLE = 4
COMMON_SINGLE = frozenset("""
lake river creek north south east west central county city town valley ridge hill hills park point
solar wind storage battery gas hydro nuclear coal green blue red white black gold silver star sun
united american national first texas ohio virginia york state states road highway
""".split())


def tokens(text: Any) -> list[str]:
    if text is None:
        return []
    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", s)


def phrase(text: str) -> tuple[str, ...] | None:
    """The distinctive words of a name, or None when nothing distinctive is left."""
    words = tuple(t for t in tokens(text) if t not in STOP)
    if not words:
        return None
    if len(words) == 1 and (len(words[0]) < MIN_SINGLE or words[0] in COMMON_SINGLE or words[0].isdigit()):
        return None
    return words


def split_names(text: str | None) -> list[str]:
    """'Crusoe / Blue Owl' -> ['Crusoe', 'Blue Owl']; 'Project Jupiter (Stargate New Mexico)' -> both."""
    if not text:
        return []
    parts = re.split(r"[()/;,]|\band\b|\s[-–—]\s|'", text)
    return [p.strip() for p in parts if p and p.strip()]


def states_of(text: str | None) -> frozenset[str]:
    if not text:
        return frozenset()
    found = {code for code in re.findall(r"\b([A-Z]{2})\b", text) if code in STATE_CODES}
    low = " ".join(tokens(text))
    found |= {code for name, code in STATE_NAMES.items() if re.search(rf"\b{name}\b", low)}
    return frozenset(found)


def contains(haystack: list[str], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return any(tuple(haystack[i:i + n]) == needle for i in range(len(haystack) - n + 1))


def county_words(county: str | None) -> tuple[str, ...]:
    words = tuple(t for t in tokens(county) if t not in {"county", "parish", "co", "borough", "city", "of"})
    if not words or words in {("not", "specified"), ("na",), ("n", "a"), ("none",), ("unknown",)}:
        return ()
    return words


@dataclass(frozen=True)
class Target:
    subject_type: str
    id: str
    name: str
    states: frozenset[str]
    places: tuple[str, ...]  # tokens of the subject's county text
    phrases: tuple[tuple[tuple[str, ...], str], ...]  # (words, which of our fields it came from)


def make_target(
    subject_type: str, id: str, name: str, state: str | None, county: str | None,
    names: Iterable[tuple[str | None, str]], place: str | None = None,
) -> Target | None:
    """A matchable subject, or None without a US state, a county and a distinctive name. A phrase made
    only of place words (the county, or the town in `place`, e.g. 'Childress') is not distinctive."""
    places = tuple(tokens(county))
    plain = set(places) | set(tokens(place)) | set(tokens(state))
    seen: dict[tuple[str, ...], str] = {}
    for text, origin in names:
        for part in split_names(text):
            words = phrase(part)
            if words and words not in seen and not set(words) <= plain:
                seen[words] = origin
    states = states_of(state)
    if not (states and places and seen):
        return None
    return Target(subject_type, id, name, states, places, tuple(seen.items()))


def load_targets(conn: Conn) -> list[Target]:
    """Our sites, campuses and plants with their location and name phrases."""
    out: list[Target] = []
    sites = conn.execute(
        """SELECT s.slug, s.name, s.state, s.county, s.tracking_names,
                  coalesce(array_agg(cp.role || '|' || cp.label) FILTER (WHERE cp.label IS NOT NULL), '{}') AS parties,
                  coalesce(array_agg(e.name) FILTER (WHERE e.name IS NOT NULL), '{}') AS entity_names,
                  coalesce((SELECT array_agg(DISTINCT a) FROM chain_party c2 JOIN entity e2 ON e2.id = c2.entity_id,
                                   unnest(e2.aliases) a
                            WHERE c2.site_slug = s.slug AND c2.role IN ('developer', 'tenant', 'end_user')), '{}') AS aliases,
                  (SELECT value #>> '{}' FROM fact_current f WHERE f.subject_type = 'site' AND f.subject_id = s.slug
                     AND f.field = 'place' AND jsonb_typeof(f.value) = 'string') AS place
             FROM site s
             LEFT JOIN chain_party cp ON cp.site_slug = s.slug AND cp.role IN ('developer', 'tenant', 'end_user')
             LEFT JOIN entity e ON e.id = cp.entity_id
            GROUP BY s.slug"""
    ).fetchall()
    for r in sites:
        names: list[tuple[str | None, str]] = [(r["name"], "site name")]
        names += [(t, "site tracking name") for t in r["tracking_names"]]
        names += [(p.split("|", 1)[1], f"site {p.split('|', 1)[0]}") for p in r["parties"]]
        names += [(n, "chain party entity") for n in r["entity_names"]]
        names += [(a, "chain party alias") for a in r["aliases"]]
        if t := make_target("site", r["slug"], r["name"], r["state"], r["county"], names, r["place"]):
            out.append(t)
    campuses = conn.execute(
        """SELECT c.id, c.name, c.aliases, c.county, c.state,
                  (SELECT array_agg(value #>> '{}') FROM fact_current f WHERE f.subject_type = 'campus'
                     AND f.subject_id = c.id AND f.field IN ('developer', 'tenant', 'end_user', 'operator')
                     AND jsonb_typeof(f.value) = 'string') AS parties
             FROM campus c"""
    ).fetchall()
    for r in campuses:
        names = [(r["name"], "campus name")] + [(a, "campus alias") for a in r["aliases"]]
        names += [(p, "campus party") for p in r["parties"] or []]
        if t := make_target("campus", r["id"], r["name"], r["state"], r["county"], names):
            out.append(t)
    plants = conn.execute(
        """SELECT p.id, p.name, p.county, p.state,
                  (SELECT array_agg(value #>> '{}') FROM fact_current f WHERE f.subject_type = 'plant'
                     AND f.subject_id = p.id AND f.field IN ('operator', 'developer')
                     AND jsonb_typeof(f.value) = 'string') AS parties
             FROM power_plant p"""
    ).fetchall()
    for r in plants:
        names = [(r["name"], "plant name")] + [(p, "plant operator") for p in r["parties"] or []]
        if t := make_target("plant", r["id"], r["name"], r["state"], r["county"], names):
            out.append(t)
    return out


def match(values: Mapping[str, Any], targets: Iterable[Target]) -> dict[str, list[dict[str, Any]]]:
    """subject kind -> [{id, name, evidence}] for every target the entry matches (rule in the docstring)."""
    state = (values.get("state") or "").strip().upper()
    county = county_words(values.get("county"))
    out: dict[str, list[dict[str, Any]]] = {}
    if len(state) != 2 or not county:
        return out
    texts = {f: tokens(values.get(f)) for f in ("name", "developer") if values.get(f)}
    for t in targets:
        if state not in t.states or not contains(list(t.places), county):
            continue
        hit = next(
            ((words, origin, f) for words, origin in t.phrases for f, toks in texts.items() if contains(toks, words)),
            None,
        )
        if hit is None:
            continue
        words, origin, entry_field = hit
        out.setdefault(t.subject_type, []).append({
            "id": t.id,
            "name": t.name,
            "evidence": {
                "rule": "state+county+name", "method": METHOD_VERSION, "state": state,
                "county": values.get("county"), "phrase": " ".join(words), "phrase_from": origin,
                "entry_field": entry_field, "entry_text": values.get(entry_field),
            },
        })
    for links in out.values():
        links.sort(key=lambda link: link["id"])
    return out
