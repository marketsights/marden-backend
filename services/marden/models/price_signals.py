"""Site signals for completion and force majeure risk (docs/methods/pricing-v0.1.md "Jupiter backtest
(locked assumptions)"), counted from the public date of the record that states them.

Locked rules:
- each agency denial of a critical-path power permit multiplies the completion-failure rate by 1.75,
  capped at 6x;
- each confirmed power delay of 3 months or more adds 0.25 to the probability of a force majeure delay
  (capped at 1), charged at 0.15 a year until completion.

Reading the records (code, no model): a site's published events (titles) and its power, permit, risk and
schedule facts, through the status model's fixed text rules (status_text.power_denied: a denial word next
to a power object, not a procedural ruling, not a voluntary switch of technology; status_dates.stated_slip
for the months of a slip; status_text.force_majeure). Records restate the same events, so counts are
not summed across them: the denial count on a day is the larger of the distinct denial events and the
most denials any one record states ('denied ... twice (Apr, Jul 2026)' states two); the delay count is
the larger of the distinct delay events and one when any record states a slip of 3+ months. A force
majeure notice citing power counts as one more confirmed delay (v0.1 reading of the rules; the
methodology lists it as an event, not as a rule). Each signal keeps its record, date and source.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.models import pit
from marden.models import status_text as tx
from marden.models.status_dates import stated_slip

DENIAL_MULT = 1.75
DENIAL_CAP = 6.0
FM_ADD = 0.25
FM_RATE = 0.15
DELAY_MIN_MONTHS = 3
SITE_FIELDS = ("risk_power", "power_detail", "power_contract", "risk_evidence", "schedule_change", "risk_permits",
               "issue", "materiality_note", "onsite_generation")
_POWERISH = re.compile(r"\b(?:power|gas|pipeline|lateral|energi[sz]\w*|interconnect\w*|in[- ]service|substation"
                       r"|transmission|turbines?|fuel cells?|microgrid|grid)\b", re.I)
_COUNT_WORDS = {"twice": 2, "two": 2, "three times": 3, "thrice": 3}
_PLURAL = re.compile(r"\b(?:denials|rejections|refusals|withdrawals)\b", re.I)


@dataclass(frozen=True)
class Signal:
    kind: str  # 'permit_denial' | 'power_delay' | 'force_majeure'
    public: date
    count: int  # how many such events the record states
    event: bool  # True for an event row (one occurrence); False for a statement in a fact
    text: str
    source_name: str
    source_url: str | None
    months: int | None = None

    def doc(self) -> dict[str, Any]:
        return {"kind": self.kind, "public": self.public.isoformat(), "count": self.count, "text": self.text[:240],
                "sourceName": self.source_name, "sourceUrl": self.source_url,
                **({"months": self.months} if self.months is not None else {})}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _denials_stated(clause: str) -> int:
    """How many denials one statement reports: a count word ('twice'), else two for a plural
    ('denials', 'rejections'), else one."""
    low = clause.lower()
    for word, n in _COUNT_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            return n
    return 2 if _PLURAL.search(clause) else 1


def read(text: str, public: date, source_name: str, source_url: str | None, *, event: bool) -> list[Signal]:
    out: list[Signal] = []
    hit = tx.power_denied(text)
    if hit is not None:
        out.append(Signal("permit_denial", public, 1 if event else _denials_stated(hit.clause), event, hit.clause,
                          source_name, source_url))
    for clause in tx.clauses(text):
        months = stated_slip(clause)
        if months is not None and months >= DELAY_MIN_MONTHS and _POWERISH.search(clause):
            out.append(Signal("power_delay", public, 1, event, clause, source_name, source_url, months))
            break
    fm = tx.force_majeure(text)
    if fm is not None and _POWERISH.search(text):
        out.append(Signal("force_majeure", public, 1, event, fm.clause, source_name, source_url))
    return out


def from_records(conn: Conn, as_of: date, slug: str) -> list[Signal]:
    """Signals in the site's records public on `as_of`."""
    out: list[Signal] = []
    for r in pit.fact_rows(conn, as_of, "site", [slug], SITE_FIELDS):
        for s in _strings(r["value"]):
            out.extend(read(s, pit.public_date(r), r["source_name"], r["source_url"], event=False))
    for e in pit.event_rows(conn, as_of, "site", [slug]):
        if e["event_date"] <= as_of:
            out.extend(read(e["title"], e["event_date"], e["source_name"], e["source_url"], event=True))
    seen: set[tuple[str, date, str]] = set()
    unique = []
    for s in sorted(out, key=lambda s: (s.public, s.kind, s.text)):
        key = (s.kind, s.public, s.text[:120])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def counts(signals: Iterable[Signal], day: date) -> tuple[int, int]:
    """(permit denials, confirmed power delays) public by `day`."""
    known = [s for s in signals if s.public <= day]
    denial_events = len({(s.public, s.text[:80]) for s in known if s.kind == "permit_denial" and s.event})
    denial_stated = max((s.count for s in known if s.kind == "permit_denial" and not s.event), default=0)
    delay_events = len({(s.public, s.text[:80]) for s in known if s.kind == "power_delay" and s.event})
    delay_stated = 1 if any(s.kind == "power_delay" and not s.event for s in known) else 0
    fm = 1 if any(s.kind == "force_majeure" for s in known) else 0
    return max(denial_events, denial_stated), max(delay_events, delay_stated) + fm


def completion_multiplier(denials: int) -> float:
    return min(DENIAL_MULT**denials, DENIAL_CAP)


def fm_probability(delays: int) -> float:
    return min(FM_ADD * delays, 1.0)


# The methodology's Jupiter event list (pricing-v0.1 "Jupiter backtest"), with its public dates and the
# correction it records (the 2026-04-27 turbine change was a voluntary switch, not a denial). Used only by
# the validation that reproduces the methodology's Results table, never by a production run.
JUPITER_LOCKED: Sequence[Signal] = (
    Signal("permit_denial", date(2026, 3, 31), 1, True, "State Land Office rejects pipeline right-of-way",
           "pricing-v0.1 Jupiter backtest", None),
    Signal("permit_denial", date(2026, 7, 16), 1, True, "Right-of-way reconsideration denied",
           "pricing-v0.1 Jupiter backtest", None),
    Signal("power_delay", date(2026, 8, 17), 1, True, "FERC filings show pipeline in-service slips to Feb 2027",
           "pricing-v0.1 Jupiter backtest", None, 6),
    Signal("force_majeure", date(2026, 9, 24), 1, True, "Oracle force majeure notice reported",
           "pricing-v0.1 Jupiter backtest", None),
)
