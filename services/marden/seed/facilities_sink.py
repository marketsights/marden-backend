"""Fact and event writing for the `facilities` loader: tag, source, as-of, n/d and materiality.

Facts: a missing or "not disclosed" value is never written (the UI shows n/d). The tag comes from
the record's URL host (``tag_for_url``) or its ``basis`` (``tag_for_basis``); values this loader
derives by a fixed rule (an enum mapping, a count, a county lookup) are M with ``RULES_VERSION``;
values the archive itself judged (risk levels, presumed links) are M with the archive file as the
method version (``legacy-<file>@2026-10-20``).

Events: published unless material. A material event dated on or after the archive date
(2026-10-20) stays a candidate (material=true) with an open ``review_item``. A material event
dated before it was already reviewed by the archive: it is published as non-material history and
the materiality judgment is kept in a ``materiality_note`` fact (M) on the same subject.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.jsonutil import jsonb
from marden.core.records import EventWriter
from marden.seed.common import parse_date, producer, source_name_for_url, tag_for_basis, tag_for_url

LOADER = "facilities"
PRODUCER = producer(LOADER)
ARCHIVE_DATE = "2026-10-20"
ARCHIVE_CUTOFF = date(2026, 10, 20)  # events before this day were reviewed by the archive
RULES_VERSION = "seed-facilities-rules@2026-10-21"
EPOCH_URL = "https://epoch.ai/data/ai-data-centers"
EPOCH_NOTICE = "Epoch AI, 'AI data centers' (CC BY 4.0), https://epoch.ai/data/ai-data-centers"
# Epoch '#confident/#likely/#speculative' and the archive's high/medium/low research confidence.
CONFIDENCE = {"confident": 0.9, "high": 0.9, "likely": 0.6, "medium": 0.6, "med": 0.6,
              "speculative": 0.3, "low": 0.3}
SEVERITY = {3: "red", 2: "amber", -1: "green"}  # archive sev -1..3; 0/1 = none

_ND = re.compile(
    r"^\s*(n/d|n/a|none|null|unknown|not disclosed|not public|not found|undisclosed|tbd|-+)\s*(\.|$|\()", re.I
)
_URL = re.compile(r"https?://[^\s'\"\]\),]+")


def legacy_method(relpath: str) -> str:
    return f"legacy-{relpath}@{ARCHIVE_DATE}"


def urls_in(value: Any) -> list[str]:
    """Public URLs in a record's source field: a URL, a list of URLs, or a stringified list."""
    items = value if isinstance(value, list | tuple) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            for u in _URL.findall(item):
                if u not in out:
                    out.append(u)
    return out


def first_url(value: Any) -> str | None:
    found = urls_in(value)
    return found[0] if found else None


def is_nd(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or bool(_ND.match(value))
    if isinstance(value, list | dict | tuple):
        return len(value) == 0
    return False


def num(value: Any) -> float | int | None:
    """A real number from a field (numeric strings from the CSVs included); None otherwise."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    try:
        f = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def usd_bn(billions: Any) -> Decimal | None:
    v = num(billions)
    return None if v is None else Decimal(str(v)) * 1_000_000_000


def confidence(label: Any) -> float | None:
    return CONFIDENCE.get(str(label).strip().lower().lstrip("#")) if label else None


def clean(text: Any) -> str | None:
    return " ".join(text.split()) if isinstance(text, str) and text.strip() else None


@dataclass(frozen=True)
class Src:
    """A seed file: its archive path, its `document` id and its file-level as-of date."""

    relpath: str
    doc_id: int
    as_of: date


@dataclass
class FactSink:
    conn: Conn
    counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    created: int = 0
    seen: int = 0

    def __post_init__(self) -> None:
        self.writer = FactWriter(self.conn)

    def add(
        self,
        subject_type: str,
        subject_id: str,
        field_name: str,
        value: Any,
        *,
        src: Src,
        url: str | None = None,
        basis: str | None = None,
        tag: str | None = None,
        rule: bool = False,
        judged: bool = False,
        quote: str | None = None,
        unit: str | None = None,
        valid_from: date | None = None,
        confidence: float | None = None,
        source_name: str | None = None,
    ) -> int | None:
        """Write one published fact; returns its id, or None when the value is n/d.

        rule=True: derived here by a fixed rule (M, RULES_VERSION). judged=True: the archive's own
        judgment (M, legacy-<file>). Otherwise the basis or the URL host decides the tag."""
        if is_nd(value):
            return None
        if rule:
            tag, method = "M", RULES_VERSION
        elif judged:
            tag, method = "M", legacy_method(src.relpath)
        else:
            if tag is None:
                tag = tag_for_basis(basis, url) if basis else tag_for_url(url)
            method = legacy_method(src.relpath) if tag == "M" else None  # presumed: the archive's call
        res = self.writer.add(
            subject_type, subject_id, field_name, value,
            tag=tag, source_name=source_name or source_name_for_url(url), source_url=url,
            valid_from=valid_from or src.as_of, producer=PRODUCER, status="published", unit=unit,
            document_id=src.doc_id, quote=clean(quote), method_version=method, confidence=confidence,
        )
        self.counts[(subject_type, field_name)] += 1
        self.seen += 1
        if res.created:
            self.created += 1
        return res.id

    def fields(self) -> list[str]:
        return sorted({f"{st}.{f}" for st, f in self.counts})


@dataclass
class EventSink:
    conn: Conn
    facts: FactSink
    counts: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    created: int = 0
    reviews: int = 0
    notes: int = 0

    def __post_init__(self) -> None:
        self.writer = EventWriter(self.conn)

    def add(
        self,
        subject_type: str,
        subject_id: str,
        event_date: Any,
        kind: str,
        title: Any,
        *,
        src: Src,
        url: str | None = None,
        severity: str | None = None,
        material_rule: str | None = None,
        tag: str | None = None,
        source_name: str | None = None,
        source: str = "",
    ) -> int | None:
        """Write one event (see the module docstring for the materiality rule)."""
        day = parse_date(event_date) if isinstance(event_date, str) else event_date
        text = clean(title)
        if day is None or text is None:
            self.skipped[f"{source or src.relpath}: no date or title"] += 1
            return None
        tag = tag or tag_for_url(url)
        name = source_name or source_name_for_url(url)
        pending = bool(material_rule) and day >= ARCHIVE_CUTOFF
        res = self.writer.add(
            subject_type, subject_id, day, kind, text, tag=tag, source_name=name, source_url=url,
            severity=severity, rule_code=None, material=pending, document_id=src.doc_id,
            status="candidate" if pending else "published",
        )
        self.counts[(subject_type, kind, res.status)] += 1
        self.created += int(res.created)
        if pending:
            self._review(res.id, text, material_rule or "", subject_type, subject_id, url)
        elif material_rule:
            note = {"event_date": day.isoformat(), "kind": kind, "title": text, "judgment": "material",
                    "rule": material_rule, "event_source_url": url}
            before = self.facts.created
            self.facts.add(subject_type, subject_id, "materiality_note", note, src=src, url=url,
                           rule=True, valid_from=min(day, ARCHIVE_CUTOFF), source_name=name)
            self.notes += self.facts.created - before
        return res.id

    def _review(self, event_id: int, title: str, rule: str, st: str, sid: str, url: str | None) -> None:
        row = self.conn.execute(
            """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
               VALUES ('event', 'event', %s, %s, %s, %s)
               ON CONFLICT (kind, ref_type, ref_id) DO NOTHING RETURNING id""",
            (str(event_id), title[:300], f"material event ({rule}): a person publishes it",
             jsonb({"subject_type": st, "subject_id": sid, "source_url": url, "producer": PRODUCER})),
        ).fetchone()
        self.reviews += int(row is not None)


def material_text(*texts: Any) -> str | None:
    """The materiality rule for item titles (a status change, a denial, force majeure): an order
    or decision denying, rejecting, revoking or dismissing; an application, permit or certificate
    denied, withdrawn, rejected or revoked; a deficiency letter / deficient filing; a moratorium
    adopted; a contract terminated; an injunction or stop-work order. Procedural noise is not
    material: confidentiality designations ('DENY CONFIDENTIAL'), discovery fights, counsel
    withdrawals, cancelled meetings, permit-review step names ('deficiency cycle')."""
    for t in texts:
        if not isinstance(t, str) or _NOISE.search(t):
            continue
        for pattern in _MATERIAL:
            m = pattern.search(t)
            if m:
                return " ".join(m.group(0).lower().split())[:80]
    return None


_NOISE = re.compile(
    r"deny confidential|denial of discovery|motion to compel|withdraw(al)? (of|as) (counsel|appearance)|"
    r"deficiency cycle|deficiency due|agenda cancel|meeting (is )?cancel|cancel+ed\s*\[|\bcancel+ed\b.{0,20}\bmeeting",
    re.I,
)
_MATERIAL = tuple(re.compile(p, re.I) for p in (
    r"force majeure",
    r"\border\s+(denying|rejecting|revoking|dismissing|terminating)\b",
    r"\b(application|permit|petition|request|certificate|cpcn|ccn|interconnection|rezoning|zoning)\b[^.;]{0,50}"
    r"\b(denied|withdrawn|rejected|revoked|dismissed|terminated)\b",
    r"\b(den(y|ies|ied)|reject(s|ed)?|revok(e|es|ed)|withdr[ae]w(s|n)?)\b[^.;]{0,20}\b(application|permit|petition|"
    r"certificate|cpcn|ccn|request for (a )?(permit|certificate|rezoning))\b",
    r"\bnotice of withdrawal of (the |its )?(application|petition|request|filing|complaint)\b",
    r"\bdeficiency letter\b|\bfiling is deficient\b|\bdeficient filing\b",
    r"\b(adopt\w*|approv\w*|pass(es|ed)?|enact\w*|impos\w*|extend\w*)\b[^.;]{0,40}\bmoratorium\b",
    r"\bmoratorium\b[^.;]{0,30}\b(adopted|approved|passed|enacted|imposed|extended)\b",
    r"\bterminat(es|ed|ion of)\b[^.;]{0,40}\b(lease|agreement|contract|ppa|esa|offtake)\b",
    r"\b(preliminary |temporary )?injunction (granted|issued)\b|\bgrant(s|ed)? (a |an )?(preliminary |temporary )?"
    r"injunction\b|\bstop[- ]work order\b",
))


def tags_weakest(tags: Iterable[str]) -> str:
    order = {"F": 0, "R": 1, "M": 2}
    tags = list(tags)
    return max(tags, key=lambda t: order[t]) if tags else "R"
