"""One material event per development: reports of the same subject and development a few days apart
(the same force-majeure notice covered by TheStreet, Vocal, Enverus ...) are one event with several sources.

Two reports are the same development when, conservatively, all of these hold:
- the same story (one headline syndicated by several outlets: views/sites_tabs.story_key), or
  * the same kind of record and, when the archive classified both, the same materiality rule;
  * no conflicting identifiers (two docket numbers or two filing dates that differ are two events);
  * the same entities: they share a name of the site, its tracking names or its chain parties, or an
    identifier;
  * the same key terms: the terms they share, weighted by how rare each is among all published records
    of that kind (inverse document frequency, so 'data center' weighs little and 'force majeure' a lot),
    make up at least SIMILARITY of the shorter headline (SAME_RULE when the archive gave both the same
    rule), with at least SHARED_RARE of the shared terms rare (each in at most RARE of the records);
  * a short window: the report is at most GAP_DAYS after the event's latest report and SPAN_DAYS after
    its first.
An event is dated by its earliest report; its summary is the run of words its sources share that
weighs most, taken verbatim from one source headline, so the words are the sources' own and not one
outlet's angle ('Oracle's Force Majeure Notice on Project Jupiter').
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from marden.core.db import Conn, Row
from marden.readmodels.context import day_end
from marden.readmodels.views.sites_common import Doc, event_evidence, material_events, severity
from marden.readmodels.views.sites_tabs import rule_words, story_key

SIMILARITY = 0.4
SAME_RULE = 0.25  # the archive's rule already says both report the same kind of development
RARE = 0.05
SHARED_RARE = 2  # one rare word in common ('Wisconsin', 'woes') is not a development
GAP_DAYS = 7
SPAN_DAYS = 30
OUTLET = re.compile(r"\s+-\s+[^-]{2,40}$")
IDENTIFIER = re.compile(r"\b[A-Z]{1,4}\d{2}-\d{1,5}\b|\b\d{1,2}/\d{1,2}/\d{4}\b")
STOP = frozenset(re.findall(r"\S+", (
    "a an the and or of on in at to for from by with as is are was were be been being its it this that these those"
    " after amid over into about why how what who whom says say said not still while near up out off than more less"
    " just me my we our you your i he she they them their his her has have had will would can could may might do"
    " does did so if but no yes all any")))
SEVERITY_ORDER = ("red", "amber", "green", "none")


def headline(title: str) -> str:
    """The headline without its outlet suffix ('… - TheStreet')."""
    return OUTLET.sub("", title or "").strip()


def terms(text: str) -> set[str]:
    """Content words of a headline: lower case, possessives and plurals folded, stop words dropped."""
    low = headline(text).lower().replace("\u2019", "'").replace("\u2018", "'")
    low = re.sub(r"'s\b", "", low)
    out = set()
    for word in re.findall(r"[a-z0-9$][a-z0-9$.]*", low):
        word = word.rstrip(".")
        if not word or word in STOP:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return out


def identifiers(text: str) -> set[str]:
    return set(IDENTIFIER.findall(text or ""))


@dataclass
class Corpus:
    """Document frequencies of terms among published records of one kind (all sites, known by the build date)."""
    docs: int
    freq: Counter[str]

    def weight(self, term: str) -> float:
        return math.log((self.docs + 1) / (self.freq[term] + 1)) + 1

    def rare(self, term: str) -> bool:
        return self.freq[term] <= max(1, RARE * self.docs)


def corpus(conn: Conn, kind: str) -> Corpus:
    rows = conn.execute(
        """SELECT title FROM event WHERE kind = %s AND status = 'published' AND learned_at < %s
             AND subject_type IN ('site', 'campus', 'plant')""", (kind, day_end())).fetchall()
    freq: Counter[str] = Counter()
    for r in rows:
        freq.update(terms(r["title"]))
    return Corpus(len(rows), freq)


def site_identity(conn: Conn, site: Row) -> set[str]:
    """Terms that name the site: its name, slug, tracking names and chain parties."""
    names: list[str] = [site.get("name") or "", site["slug"].replace("-", " "), *(site.get("tracking_names") or [])]
    for r in conn.execute(
            """SELECT cp.label, e.name FROM chain_party cp LEFT JOIN entity e ON e.id = cp.entity_id
                WHERE cp.site_slug = %s""", (site["slug"],)).fetchall():
        names += [r.get("label") or "", r.get("name") or ""]
    return set().union(*(terms(n) for n in names if n)) if names else set()


@dataclass
class Report:
    row: Row
    rule: str | None
    terms: set[str] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)

    @property
    def when(self) -> date:
        return self.row["event_date"]


def same_development(a: Report, b: Report, identity: set[str], corp: Corpus) -> bool:
    if story_key(a.row["title"]) and story_key(a.row["title"]) == story_key(b.row["title"]):
        return True
    if a.row["kind"] != b.row["kind"] or (a.rule and b.rule and a.rule != b.rule):
        return False
    if a.ids and b.ids and not a.ids & b.ids:
        return False
    if not (a.terms & b.terms & identity) and not (a.ids & b.ids):
        return False
    ka, kb = a.terms - identity, b.terms - identity
    shared = ka & kb
    if sum(corp.rare(t) for t in shared) < SHARED_RARE:
        return False
    shorter = min(sum(corp.weight(t) for t in ka), sum(corp.weight(t) for t in kb))
    return sum(corp.weight(t) for t in shared) / shorter >= (SAME_RULE if a.rule and a.rule == b.rule else SIMILARITY)


def cluster(reports: Iterable[Report], identity: set[str], corpora: Callable[[str], Corpus]) -> list[list[Report]]:
    """Group reports (any order) into developments, oldest report first in each; events oldest first."""
    events: list[list[Report]] = []
    for rep in sorted(reports, key=lambda r: (r.when, r.row["id"])):
        home = None
        for ev in events:
            outside = (rep.when - ev[0].when).days > SPAN_DAYS or (rep.when - ev[-1].when).days > GAP_DAYS
            if outside and not any(story_key(m.row["title"]) == story_key(rep.row["title"]) for m in ev):
                continue  # the same headline joins its story whenever it ran; anything else needs the window
            if any(same_development(m, rep, identity, corpora(rep.row["kind"])) for m in ev):
                home = ev
                break
        if home is None:
            events.append([rep])
        else:
            home.append(rep)
    return events


def _phrase(rep: Report, share: Counter[str], n: int, corp: Corpus) -> tuple[float, str]:
    """The run of the headline's own words, each used by at least a third of the event's sources (stop
    words inside allowed), weighing most: 'Oracle's Force Majeure Notice on Project Jupiter'."""
    best: tuple[float, str] = (0.0, "")
    run: list[str] = []
    score = 0.0
    words = headline(rep.row["title"]).split()
    start = 0
    for i, word in enumerate([*words, None]):
        found = terms(word) if word else set()
        if word is not None and all(share[t] * 3 >= n for t in found):
            if not run:
                start = i
            run.append(word)
            score += sum(corp.weight(t) * share[t] / n for t in found)
            continue
        while run and not terms(run[-1]):
            run.pop()  # a run ends on a content word, and starts on one unless it opens the headline
        while run and start > 0 and not terms(run[0]):
            run.pop(0)
        if sum(len(terms(w)) for w in run) >= 2 and score > best[0]:
            best = (score, " ".join(run).strip(" :;,-\u2013\u2014"))
        run, score = [], 0.0
    return best


def summary(event: Sequence[Report], corp: Corpus) -> str:
    """A neutral line in the sources' own words: the run of words the sources share that weighs most
    (each term weighted by its rarity and by the share of the sources using it), taken from one source's
    headline; one source (or no shared run) keeps its headline, outlet suffix dropped."""
    if len(event) == 1:
        return event[0].row["title"]
    share = Counter(t for rep in event for t in rep.terms)
    score, text = max((_phrase(rep, share, len(event), corp) for rep in event), key=lambda p: p[0])
    return text if score > 0 else headline(event[0].row["title"])


def material_rows(conn: Conn, site: Row, events: Sequence[Row]) -> list[Doc]:
    """SiteDetail.materialEvents: one row per development, newest first, dated by its earliest report,
    with the sources' shared words as its text and, for several reports, every source (oldest first)."""
    reports = [Report(ev, rule_words(ev.get("rule_code") or (note or {}).get("rule")), terms(ev["title"]),
                      identifiers(ev["title"]))
               for ev, note in material_events(conn, site["slug"], events)]
    identity = site_identity(conn, site)
    corpora: dict[str, Corpus] = {}

    def corpus_of(kind: str) -> Corpus:
        if kind not in corpora:
            corpora[kind] = corpus(conn, kind)
        return corpora[kind]

    rows: list[Doc] = []
    for event in cluster(reports, identity, corpus_of):
        lead = event[0].row
        item: Doc = {**event_evidence(lead), "text": summary(event, corpus_of(lead["kind"])),
                     "severity": min((severity(r.row) for r in event), key=SEVERITY_ORDER.index)}
        code = next((r.row["rule_code"] for r in event if r.row.get("rule_code")), None)
        if code:
            item["ruleCode"] = code
        if len(event) > 1:
            item["sources"] = [event_evidence(r.row) for r in event]
        rows.append(item)
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows
