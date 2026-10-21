"""Fact writing for the `sites` seed loader: tag, source, as-of and n/d rules in one place.

Every value goes through ``FactSink.add``. A value that is missing or says "not disclosed" is not
written (the UI shows n/d). The tag comes from the record's ``basis`` when it has one, else from
the URL host (``seed.common.tag_for_url``); presumed / inferred values are M with the archive
file as their method version (``legacy-<file>@2026-10-20``); values this loader derives by a
fixed rule (an enum mapping, an edge status) are M with ``RULES_VERSION``.
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
from marden.seed.common import producer, source_name_for_url, tag_for_basis, tag_for_url

LOADER = "sites"
PRODUCER = producer(LOADER)
ARCHIVE_DATE = "2026-10-20"
RULES_VERSION = "seed-sites-rules@2026-10-21"
TAG_ORDER = {"F": 0, "R": 1, "M": 2}

_ND = re.compile(
    r"^\s*(n/d|n/a|none|null|unknown|not disclosed|not public|not found|undisclosed|tbd|-+)\s*(\.|$|\()", re.I
)
_JUDGED = re.compile(r"\b(inferred|presumed|assumed|unverified)\b", re.I)


def legacy_method(relpath: str) -> str:
    return f"legacy-{relpath}@{ARCHIVE_DATE}"


def public_urls(urls: Iterable[Any] | Any) -> list[str]:
    """The http(s) URLs in a record's source list (``local:`` pointers are archive-internal)."""
    if isinstance(urls, str):
        urls = [urls]
    out: list[str] = []
    for u in urls or []:
        if isinstance(u, str) and u.strip().lower().startswith(("http://", "https://")) and u.strip() not in out:
            out.append(u.strip())
    return out


def pick_url(urls: Iterable[Any] | Any, *, tag: str | None = None) -> str | None:
    """The record's original public URL: its lead citation (the first public URL it lists), or the
    first URL with the given tag ('R' for a third-party estimate cited next to a filing)."""
    found = public_urls(urls)
    if tag is not None:
        found = [u for u in found if tag_for_url(u) == tag]
    return found[0] if found else None


def usd(millions: Any) -> Decimal | None:
    """USD from the archive's $m figures (exact decimal arithmetic, so 555.2 -> 555200000)."""
    if millions is None or isinstance(millions, bool):
        return None
    try:
        return Decimal(str(millions)) * 1_000_000
    except ArithmeticError:
        return None


def number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return value
    return None


def is_nd(value: Any) -> bool:
    """True for a value nobody has sourced: None, empty, or text that only says n/d."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or bool(_ND.match(value))
    if isinstance(value, list | dict | tuple):
        return len(value) == 0
    return False


def judged(*texts: Any) -> bool:
    """The text marks itself as a Marden judgment (inferred, presumed, assumed, unverified)."""
    return any(isinstance(t, str) and _JUDGED.search(t) for t in texts)


def weakest(tags: Iterable[str]) -> str:
    """The weakest tag of a set (F < R < M): a list fact is only as strong as its weakest item."""
    tags = list(tags)
    return max(tags, key=lambda t: TAG_ORDER[t]) if tags else "R"


@dataclass(frozen=True)
class Src:
    """A seed file: its path in the archive, its `document` id and its file-level as-of date."""

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
        quote: str | None = None,
        unit: str | None = None,
        valid_from: date | None = None,
    ) -> int | None:
        """Write one published fact; returns its id, or None when the value is n/d."""
        if is_nd(value):
            return None
        if tag is None and judged(basis, value if isinstance(value, str) else None):
            tag = "M"  # the record marks the value itself as presumed / inferred
        if tag is None:
            tag = tag_for_basis(basis, url) if basis else tag_for_url(url)
        method = None
        if tag == "M":
            method = RULES_VERSION if rule else legacy_method(src.relpath)
        quote = quote.strip() if isinstance(quote, str) and quote.strip() else None
        res = self.writer.add(
            subject_type, subject_id, field_name, value,
            tag=tag, source_name=source_name_for_url(url), source_url=url,
            valid_from=valid_from or src.as_of, producer=PRODUCER, status="published", unit=unit,
            document_id=src.doc_id, quote=quote, method_version=method,
        )
        self.counts[(subject_type, field_name)] += 1
        self.seen += 1
        if res.created:
            self.created += 1
        return res.id

    def fields(self) -> list[str]:
        return sorted({f"{st}.{f}" for st, f in self.counts})
