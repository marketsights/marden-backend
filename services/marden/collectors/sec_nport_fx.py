"""Which way a filing's N-PORT exchange rate points, for valuing non-USD positions in their currency.

Form N-PORT asks for "the exchange rate used to calculate value" without fixing its direction, and
filers differ. In the 2026q3 DERA data set at 2026-07-31, 85% of JPY positions carry about 159.1 (yen
per US dollar) and 15% about 0.0063 (dollars per yen); EUR splits 0.867 / 1.153 the same way (51
filer agents against 10). Read the wrong way, a EUR bond at 85 shows as 113.

So each run tallies, per currency and report date, the rates filings report (one vote per filing:
every filing read in the run, matching or not, the DERA data set when one is read, and the stored
holdings). The most common rate is the reference R; it is the currency-per-dollar convention most
filers use. A position's rate r is read as currency per dollar when it is within 3% of R, as
dollars per currency when 1/r is, and otherwise (or with fewer than 3 votes) not used: the holding
is kept with its rate, and no mark is written for it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal, InvalidOperation

from marden.core.db import Conn

PER_USD = "currency_per_usd"
USD_PER = "usd_per_currency"
UNVERIFIED = "unverified"
MIN_VOTES = 3
TOLERANCE = Decimal("0.03")

_COND = re.compile(rb"<(?:\w+:)?currencyConditional\b([^>]*)>")
_CUR = re.compile(rb"\bcurCd=\"([A-Z]{3})\"")
_RATE = re.compile(rb"\bexchangeRt=\"([0-9.Ee+-]+)\"")
_PERIOD = re.compile(rb"<(?:\w+:)?repPdDate>\s*(\d{4}-\d{2}-\d{2})\s*<")


def _bucket(rate: Decimal) -> Decimal:
    """Two significant digits: 0.86719 and 0.865914 -> 0.87, 159.22 -> 1.6E+2; a rate and its
    reciprocal never share a bucket unless both are within about 5% of 1."""
    return Decimal(f"{rate:.2g}")


def parse_rate(text: str | bytes | None) -> Decimal | None:
    try:
        value = Decimal(text.decode() if isinstance(text, bytes) else str(text or ""))
    except (InvalidOperation, UnicodeDecodeError):
        return None
    return value if value.is_finite() and value > 0 else None


def document_rates(data: bytes) -> tuple[date | None, set[tuple[str, Decimal]]]:
    """The report date and the (currency, rate) pairs of an N-PORT primary_doc.xml (byte regexes)."""
    m = _PERIOD.search(data)
    period = date.fromisoformat(m.group(1).decode()) if m else None
    pairs: set[tuple[str, Decimal]] = set()
    for attrs in _COND.findall(data):
        cur, rate = _CUR.search(attrs), _RATE.search(attrs)
        if cur and rate and cur.group(1) != b"USD" and (r := parse_rate(rate.group(1))) is not None:
            pairs.add((cur.group(1).decode(), r))
    return period, pairs


class FxVotes:
    """(currency, report date) -> rate bucket -> the filings (accessions) that reported it."""

    def __init__(self) -> None:
        self.votes: dict[tuple[str, date], dict[Decimal, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.values: dict[tuple[str, date, Decimal], list[Decimal]] = defaultdict(list)
        self.used: Counter[tuple[str, date, str]] = Counter()

    def add(self, accession: str, period: date | None, pairs: Iterable[tuple[str, Decimal]]) -> None:
        if period is None:
            return
        for cur, rate in pairs:
            b = _bucket(rate)
            if accession not in self.votes[(cur, period)][b]:
                self.votes[(cur, period)][b].add(accession)
                self.values[(cur, period, b)].append(rate)

    def load_stored(self, conn: Conn, since: date) -> int:
        """Votes from holdings already stored (their cur_cd and exchange_rt facts)."""
        rows = conn.execute(
            """SELECT h.accession, h.period_end, c.value #>> '{}' AS cur, r.value #>> '{}' AS rate
               FROM holding h
               JOIN fact c ON c.subject_type = 'holding' AND c.subject_id = h.id::text AND c.field = 'cur_cd'
               JOIN fact r ON r.subject_type = 'holding' AND r.subject_id = h.id::text AND r.field = 'exchange_rt'
               WHERE h.period_end >= %s AND c.status = 'published' AND r.status = 'published'""",
            (since,),
        ).fetchall()
        for row in rows:
            if (rate := parse_rate(row["rate"])) is not None and row["cur"] and row["cur"] != "USD":
                self.add(row["accession"], row["period_end"], [(row["cur"], rate)])
        return len(rows)

    def reference(self, cur: str, period: date) -> Decimal | None:
        buckets = self.votes.get((cur, period))
        if not buckets:
            return None
        ranked = Counter({b: len(a) for b, a in buckets.items()}).most_common()
        top, n = ranked[0]
        if n < MIN_VOTES or (len(ranked) > 1 and ranked[1][1] == n):
            return None
        vals = sorted(self.values[(cur, period, top)])
        return vals[len(vals) // 2]

    def convention(self, cur: str | None, rate: Decimal | None, period: date) -> str:
        """PER_USD, USD_PER or UNVERIFIED for a non-USD position's rate."""
        out = UNVERIFIED
        ref = self.reference(cur, period) if cur and rate is not None and rate > 0 else None
        if ref is not None and rate is not None:
            if abs(rate / ref - 1) <= TOLERANCE:
                out = PER_USD
            elif abs(1 / (rate * ref) - 1) <= TOLERANCE:
                out = USD_PER
        self.used[(cur or "", period, out)] += 1
        return out

    def summary(self) -> dict[str, dict[str, object]]:
        """For each currency and report date a position needed: votes, the reference and the decisions."""
        out: dict[str, dict[str, object]] = {}
        for (cur, period, decision), n in sorted(self.used.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2])):
            ref = self.reference(cur, period)
            entry = out.setdefault(f"{cur} {period.isoformat()}", {
                "filings": sum(len(a) for a in self.votes.get((cur, period), {}).values()),
                "reference": str(ref) if ref is not None else None})
            entry[decision] = n
        return out
