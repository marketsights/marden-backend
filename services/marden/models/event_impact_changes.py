"""The dated changes in a site's event inputs to the Marden price (event impact model).

The price engine reads three kinds of event input for a site's debt (docs/methods/pricing-v0.1.md):

- power and permit signals (price_signals): permit denials, power delays of 3+ months and force-majeure
  notices citing power, each counted from the public date of the record that states it. A day changes the
  inputs when its new records change the engine's counts; records that restate what was already counted
  change nothing (the engine counts a development once);
- the projected completion (price_chain): the cushion model's P50, from its latest run on or before the day;
- dealer quotes and other observations of the debt (price_marks), which move the residual spread.

Each change is dated by the day the engine first sees it and carries the records behind it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from marden.core.db import Conn
from marden.models.price_chain import _completion
from marden.models.price_signals import Signal, counts

KINDS = ("permit_denial", "power_delay", "force_majeure")
KIND_WORDS = {"permit_denial": ("permit denial", "permit denials"),
              "power_delay": ("power delay of 3+ months", "power delays of 3+ months"),
              "force_majeure": ("force-majeure notice citing power", "force-majeure notices citing power")}
QUOTE_KINDS = ("dealer", "quote")


def kind_words(kind: str, n: int = 1) -> str:
    one, many = KIND_WORDS.get(kind, (kind.replace("_", " "), kind.replace("_", " ")))
    return one if n == 1 else many


def without(sigs: Iterable[Signal], day: date, kinds: Iterable[str] | None = None) -> list[Signal]:
    """The signals with the records first public on `day` left out (only those of `kinds` when given)."""
    ks = set(kinds) if kinds is not None else None
    return [s for s in sigs if not (s.public == day and (ks is None or s.kind in ks))]


@dataclass
class SignalChange:
    day: date
    before: tuple[int, int]  # (denials, delays incl. force majeure) without the day's new records
    after: tuple[int, int]
    kinds: list[str]  # the kinds whose records, left out alone, change the counts
    records: list[Signal] = field(default_factory=list)

    def label(self) -> str:
        """'Reported: 2 permit denials, a power delay of 3+ months and a force-majeure notice citing power'."""
        parts = []
        denials = self.after[0] - self.before[0]
        if "permit_denial" in self.kinds and denials > 0:
            parts.append("a permit denial" if denials == 1 else f"{denials} permit denials")
        if "power_delay" in self.kinds:
            parts.append("a power delay of 3+ months")
        if "force_majeure" in self.kinds:
            parts.append("a force-majeure notice citing power")
        if not parts:
            return "Power and permit setbacks reported"
        return "Reported: " + (parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1])


def day_change(sigs: Sequence[Signal], day: date) -> SignalChange | None:
    """What the records first public on `day` change in the engine's counts (None: they restate)."""
    after = counts(sigs, day)
    before = counts(without(sigs, day), day)
    if after == before:
        return None
    kinds = [k for k in KINDS if counts(without(sigs, day, [k]), day) != after]
    return SignalChange(day, before, after, kinds, [s for s in sigs if s.public == day])


def signal_days(sigs: Iterable[Signal], since: date) -> list[date]:
    return sorted({s.public for s in sigs if s.public >= since})


def month_text(d: date | None) -> str:
    return "none" if d is None else f"{d:%b} {d.year}"


@dataclass(frozen=True)
class CompletionChange:
    day: date  # the cushion run that moved it
    before: date | None
    after: date | None

    def label(self) -> str:
        if self.before is None:
            return f"First projected completion: {month_text(self.after)}"
        if self.after is None:
            return f"Projected completion dropped (was {month_text(self.before)})"
        return f"Projected completion moved from {month_text(self.before)} to {month_text(self.after)}"


def completion_changes(conn: Conn, slug: str, since: date, until: date) -> list[CompletionChange]:
    """Each cushion run in [since, until] that moved the completion the engine uses (price_chain._completion)."""
    rows = conn.execute(
        """SELECT DISTINCT as_of FROM model_output WHERE model = 'cushion' AND subject_type = 'site'
            AND subject_id = %s AND as_of BETWEEN %s AND %s ORDER BY as_of""", (slug, since, until)).fetchall()
    out = []
    for r in rows:
        d: date = r["as_of"]
        now, _ = _completion(conn, slug, d)
        was, _ = _completion(conn, slug, d - timedelta(days=1))
        if now != was:
            out.append(CompletionChange(d, was, now))
    return out


@dataclass(frozen=True)
class QuoteChange:
    day: date
    instrument_id: str
    price: float
    source_name: str
    source_url: str | None
    tag: str

    def label(self) -> str:
        return f"Dealer quote {self.price:g} reported ({self.source_name})"

    def matches(self, o: Any) -> bool:
        """The engine's observation of this quote (price_marks.Obs)."""
        return o.source == "dealer" and o.day == self.day and abs(o.price - self.price) < 1e-6


def quote_changes(conn: Conn, ids: Sequence[str], since: date, until: date) -> list[QuoteChange]:
    if not ids:
        return []
    rows = conn.execute(
        """SELECT instrument_id, obs_date, price, source_name, source_url, tag FROM price_obs
            WHERE instrument_id = ANY(%s) AND kind = ANY(%s) AND price IS NOT NULL AND obs_date BETWEEN %s AND %s
            ORDER BY obs_date, id""", (list(ids), list(QUOTE_KINDS), since, until)).fetchall()
    return [QuoteChange(r["obs_date"], r["instrument_id"], float(r["price"]), r["source_name"], r["source_url"],
                        str(r["tag"]).strip()) for r in rows]


def record_tags(conn: Conn, slug: str) -> dict[str, str]:
    """source_url -> tag of the site's facts and events (a signal keeps its record's url, not its tag)."""
    rows = conn.execute(
        """SELECT source_url, min(tag) AS tag FROM (
             SELECT source_url, tag FROM fact WHERE subject_type = 'site' AND subject_id = %(s)s
             UNION ALL SELECT source_url, tag FROM event WHERE subject_type = 'site' AND subject_id = %(s)s) t
            WHERE source_url IS NOT NULL GROUP BY source_url""", {"s": slug}).fetchall()
    return {r["source_url"]: str(r["tag"]).strip() for r in rows}


def record_doc(s: Signal, tags: dict[str, str]) -> dict[str, Any]:
    """Evidence (types.ts) for a record the engine read, with the kind it was read as."""
    return {"date": s.public.isoformat(), "text": s.text, "tag": tags.get(s.source_url or "", "R"),
            "sourceName": s.source_name, "sourceUrl": s.source_url or "", "kind": s.kind,
            "kindWords": kind_words(s.kind, s.count)}
