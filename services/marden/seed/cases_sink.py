"""Fact, event and model-output writing for the `cases` loader: tags, n/d, as-of, quarantine.

Facts: a missing or "not disclosed" value is never written (the UI shows n/d). The tag comes from
the record's ``basis`` (``tag_for_basis``) or the URL host (``tag_for_url``); the archive's own
research judgments (risk levels, summaries, modeled values) are M with the file as method version
(``legacy-<file>@2026-10-20``); values this loader maps by a fixed rule are M with
``RULES_VERSION``. valid_from is the record's own date when it has one (never after today), else
the file's as-of. Quotes are kept verbatim, except a quote that cites a blocked price source,
which is dropped (cases_quarantine).

Model outputs: legacy runs go to ``model_output`` (one ``model_run`` per model and version, found
again on reruns), never to ``price_obs``. Each new output marks its subject's read models dirty.

Every section of the load runs in its own short transaction (``Sections``): each write marks shared
``read_model_dirty`` rows, so one long transaction would block, or deadlock with, other writers.
"""

from __future__ import annotations

import copy
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, TypeVar

import psycopg

from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.jsonutil import canonical_json, hash_parts, jsonb
from marden.core.records import EventWriter
from marden.seed.cases_quarantine import mentions_blocked
from marden.seed.common import as_of, parse_date, producer, source_name_for_url, tag_for_basis, tag_for_url

LOADER = "cases"
PRODUCER = producer(LOADER)
ARCHIVE_DATE = "2026-10-20"
RULES_VERSION = "seed-cases-rules@2026-10-21"
LEGACY_VERSION = "legacy@2026-10-20"  # model_output version of every legacy model table
T = TypeVar("T")

# The whole text says n/d (optionally with a short parenthetical); "Not disclosed. Model: ..." is kept.
_ND = re.compile(
    r"^\s*(n/d|n/a|none|null|unknown|not disclosed|not public|not found|undisclosed|tbd|-+)\s*"
    r"(\.|\([^)]{0,80}\))?\s*$", re.I)
_URL = re.compile(r"https?://[^\s'\"\]\),;]+")
_PRESS_BASIS = re.compile(r"reuters|bloomberg|press|reported|\bft\b|wsj|dcd|news", re.I)


def legacy_method(relpath: str) -> str:
    return f"legacy-{relpath}@{ARCHIVE_DATE}"


def urls_in(value: Any) -> list[str]:
    """Public URLs in a source field: a URL, a list of URLs (or [url, label] pairs), or text."""
    items = value if isinstance(value, list | tuple) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, list | tuple):
            out += [u for u in urls_in(list(item)) if u not in out]
        elif isinstance(item, dict):
            out += [u for u in urls_in([item.get("u"), item.get("url")]) if u not in out]
        elif isinstance(item, str):
            out += [u.rstrip(".") for u in _URL.findall(item) if u.rstrip(".") not in out]
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


def compact(value: Any) -> Any:
    """Drop n/d members of dicts (recursively) and n/d items of lists."""
    if isinstance(value, dict):
        out = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if not is_nd(v)}
    if isinstance(value, list):
        return [v for v in (compact(x) for x in value) if not is_nd(v)]
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def num(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    try:
        f = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def usd_m(millions: Any) -> Decimal | None:
    """USD from the archive's $m figures (exact decimals)."""
    v = num(millions)
    return None if v is None else Decimal(str(v)) * 1_000_000


def usd_bn(billions: Any) -> Decimal | None:
    v = num(billions)
    return None if v is None else Decimal(str(v)) * 1_000_000_000


def basis_tag(basis: Any, url: str | None = None) -> str:
    """tag_for_basis, except that 'disclosed (Reuters)'-style bases name a press source (R)."""
    text = str(basis or "")
    tag = tag_for_basis(text, url) if text.strip() else tag_for_url(url)
    return "R" if tag == "F" and _PRESS_BASIS.search(text) else tag


def weakest(tags: list[str]) -> str:
    order = {"F": 0, "R": 1, "M": 2}
    return max(tags, key=lambda t: order[t]) if tags else "R"


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
    skipped: Counter[str] = field(default_factory=Counter)
    created: int = 0
    seen: int = 0

    def __post_init__(self) -> None:
        self.writer = FactWriter(self.conn)

    def add(self, subject_type: str, subject_id: str, field_name: str, value: Any, *, src: Src,
            url: str | None = None, basis: Any = None, tag: str | None = None, judged: bool = False,
            rule: bool = False, quote: Any = None, unit: str | None = None, valid_from: Any = None,
            source_name: str | None = None, confidence: float | None = None) -> int | None:
        """Write one published fact; None when the value is n/d. judged=True: the archive's own
        judgment (M, legacy-<file>); rule=True: mapped here by a fixed rule (M, RULES_VERSION)."""
        value = compact(value)
        if is_nd(value):
            self.skipped[f"{subject_type}.{field_name}: n/d"] += 1
            return None
        if rule:
            tag, method = "M", RULES_VERSION
        elif judged:
            tag, method = "M", legacy_method(src.relpath)
        else:
            tag = tag or (basis_tag(basis, url) if basis else tag_for_url(url))
            method = legacy_method(src.relpath) if tag == "M" else None
        text = " ".join(quote.split()) if isinstance(quote, str) and quote.strip() else None
        if text and mentions_blocked(text):
            self.skipped["quote citing a blocked price source (quote not kept)"] += 1
            text = None
        day = valid_from if isinstance(valid_from, date) else parse_date(valid_from)
        res = self.writer.add(
            subject_type, subject_id, field_name, value, tag=tag,
            source_name=source_name or source_name_for_url(url), source_url=url,
            valid_from=as_of(day, src.as_of), producer=PRODUCER, status="published", unit=unit,
            document_id=src.doc_id, quote=text, method_version=method, confidence=confidence,
        )
        self.counts[(subject_type, field_name)] += 1
        self.seen += 1
        self.created += int(res.created)
        return res.id

    def fields(self) -> list[str]:
        return sorted({f"{st}.{f}" for st, f in self.counts})

    def by_subject(self) -> dict[str, int]:
        out: Counter[str] = Counter()
        for (st, _), n in self.counts.items():
            out[st] += n
        return dict(sorted(out.items()))


@dataclass
class EventSink:
    conn: Conn
    counts: Counter[str] = field(default_factory=Counter)
    created: int = 0

    def add(self, subject_type: str, subject_id: str, day: date, kind: str, title: str, *, src: Src,
            url: str | None, severity: str | None) -> None:
        res = EventWriter(self.conn).add(
            subject_type, subject_id, day, kind, " ".join(title.split()), tag=tag_for_url(url),
            source_name=source_name_for_url(url), source_url=url, severity=severity,
            document_id=src.doc_id, status="published")
        self.counts[kind] += 1
        self.created += int(res.created)


@dataclass
class ModelSink:
    """model_run / model_output rows for the archive's legacy model tables."""

    conn: Conn
    counts: Counter[str] = field(default_factory=Counter)
    created: int = 0
    runs: dict[str, int] = field(default_factory=dict)

    def _run(self, model: str) -> int:
        if model not in self.runs:
            row = self.conn.execute(
                """SELECT id FROM model_run WHERE model = %s AND version = %s AND trigger = %s
                   ORDER BY id LIMIT 1""", (model, LEGACY_VERSION, PRODUCER)).fetchone()
            if row is None:
                row = self.conn.execute(
                    """INSERT INTO model_run (model, version, trigger, finished_at, ok, inputs_hash)
                       VALUES (%s, %s, %s, now(), true, %s) RETURNING id""",
                    (model, LEGACY_VERSION, PRODUCER, hash_parts(model, LEGACY_VERSION, ARCHIVE_DATE))).fetchone()
            self.runs[model] = row["id"]
        return self.runs[model]

    def add(self, model: str, subject_type: str, subject_id: str, day: date, output: dict[str, Any]) -> None:
        from marden.readmodels.deps import mark_dirty_for

        body = compact(output)
        canonical_json(body)  # JSON-safe before the insert
        row = self.conn.execute(
            """INSERT INTO model_output (run_id, model, version, subject_type, subject_id, as_of, output)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (model, version, subject_type, subject_id, as_of) DO NOTHING RETURNING id""",
            (self._run(model), model, LEGACY_VERSION, subject_type, subject_id, day, jsonb(body))).fetchone()
        self.counts[f"{model}:{subject_type}"] += 1
        if row is not None:
            self.created += 1
            mark_dirty_for(subject_type, subject_id, "@model", conn=self.conn, reason=f"model_output:{row['id']}")


RETRYABLE = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure,
             psycopg.errors.LockNotAvailable)
_STATE = ("counts", "skipped", "created", "seen")


@dataclass
class Sections:
    """Run each part of the load in its own transaction, retrying lock conflicts. The sinks'
    counters and `stats` are restored before a retry so the run statistics stay exact."""

    conn: Conn
    sinks: tuple[Any, ...]
    stats: dict[str, Any]
    retries: dict[str, int] = field(default_factory=dict)

    def run(self, name: str, fn: Callable[[], T]) -> T:
        for attempt in range(1, 5):
            snap = ([{a: copy.deepcopy(getattr(s, a)) for a in _STATE if hasattr(s, a)} for s in self.sinks],
                    copy.deepcopy(self.stats))
            try:
                with self.conn.transaction():
                    return fn()
            except RETRYABLE:
                for sink, state in zip(self.sinks, snap[0], strict=True):
                    for attr, value in state.items():
                        setattr(sink, attr, value)
                self.stats.clear()
                self.stats.update(snap[1])
                if attempt == 4:
                    raise
                self.retries[name] = attempt
                time.sleep(1.5 * attempt)
        raise AssertionError("unreachable")
