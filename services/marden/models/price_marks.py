"""Price observations the price model may use on a run's as-of day (point in time; pricing-v0.1 "Data").

Kinds read from `price_obs` (never the model's own 'marden' rows or 'legacy' rows):

- N-PORT fund marks: the per-fund rows ('N-PORT: <fund> (<period>)') of one instrument and period are
  collapsed to their median across distinct funds, with the holder count and the min-max range. A mark
  is usable only once public, NPORT_LAG_DAYS (60) after its period end. Pre-computed 'N-PORT median of
  N funds' rows are used only where no per-fund row exists.
- BDC schedule-of-investments marks ('BDC 10-Q ...' / 'BDC 10-K ...'), public BDC_LAG_DAYS after the
  period (45 for a 10-Q, 90 for a 10-K); the unfunded-adjusted M rows win over the raw F rows of the
  same fund and period.
- TRACE end-of-day prints (kind 'trade') and exchange quotes (kind 'exchange'), usable on their date.
- Press-reported dealer quotes (kind 'dealer', tag R): usable on their date, with a wide noise.

When MARDEN_SHOW_UNLICENSED_PRICES is false, rows from unlicensed sources (marden.core.licensing) are
dropped here, so they never reach an input. Observations are returned per instrument, oldest first.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from marden.core.db import Conn
from marden.core.licensing import show_unlicensed_prices, unlicensed_source_names
from marden.models import pit

NPORT_LAG_DAYS = 60
BDC_LAG_DAYS = {"10-Q": 45, "10-K": 90}
_MEDIAN = re.compile(r"median of (\d+) funds?", re.I)
_PERIOD = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")
_BDC_FORM = re.compile(r"^BDC (10-[QK])")

# Observation noise (standard deviation, points of price) by source; a fund median's noise shrinks
# with the holder count. Judgment values for v0.1, used by the Kalman filter (price_kalman).
NOISE_PTS = {"trace": 0.75, "exchange": 1.5, "nport": 1.0, "bdc": 2.0, "dealer": 3.0}


@dataclass(frozen=True)
class Obs:
    instrument_id: str
    day: date  # the price's own date (trade date, fund period end)
    public: date  # the first day the model may use it
    price: float  # clean, percent of par
    source: str  # 'trace' | 'exchange' | 'nport' | 'bdc' | 'dealer'
    source_name: str
    source_url: str | None
    holders: int = 1
    low: float | None = None
    high: float | None = None

    @property
    def noise(self) -> float:
        base = NOISE_PTS[self.source]
        if self.source in ("nport", "bdc"):
            spread = (self.high - self.low) if self.high is not None and self.low is not None else 0.0
            return max(base / max(self.holders, 1) ** 0.5, spread / 4.0, 0.25)
        return base

    @property
    def stale(self) -> bool:
        """Fund marks are smoothed, lagged readings (Getmansky-Lo-Makarov)."""
        return self.source in ("nport", "bdc")


def _period(name: str, fallback: date) -> date:
    m = _PERIOD.search(name)
    return date.fromisoformat(m.group(1)) if m else fallback


def load(conn: Conn, as_of: date, instrument_ids: Sequence[str], *, kinds: Sequence[str] | None = None,
         since: date | None = None) -> dict[str, list[Obs]]:
    """Every observation usable on `as_of` for these instruments, by instrument, oldest first."""
    params: dict[str, object] = {"ids": list(instrument_ids), "day": as_of,
                                 "since": since or date(2000, 1, 1)}
    sql = """SELECT instrument_id, obs_date, kind, price, tag, source_name, source_url FROM price_obs
              WHERE instrument_id = ANY(%(ids)s) AND obs_date <= %(day)s AND obs_date >= %(since)s
                AND price IS NOT NULL AND kind IN ('fund_mark', 'trade', 'exchange', 'dealer')"""
    if not show_unlicensed_prices():
        sql += " AND source_name <> ALL(%(hidden)s)"
        params["hidden"] = unlicensed_source_names()
    rows = conn.execute(sql + " ORDER BY instrument_id, obs_date, id", params).fetchall()
    nport: dict[tuple[str, date], dict[str, float]] = defaultdict(dict)
    nport_median: dict[tuple[str, date], tuple[float, int, str, str | None]] = {}
    bdc: dict[tuple[str, date, str], dict[str, tuple[float, str, str | None]]] = defaultdict(dict)
    out: dict[str, list[Obs]] = defaultdict(list)
    for r in rows:
        iid, name, price = r["instrument_id"], r["source_name"], float(r["price"])
        if r["kind"] == "fund_mark":
            if name.startswith("N-PORT"):
                period = _period(name, r["obs_date"])
                m = _MEDIAN.search(name)
                if m:
                    nport_median[(iid, period)] = (price, int(m.group(1)), name, r["source_url"])
                elif r["tag"].strip() == "F":
                    nport[(iid, period)][name] = price
            elif name.startswith("BDC"):
                form = _BDC_FORM.match(name)
                if form is None or _MEDIAN.search(name):
                    continue
                period = _period(name, r["obs_date"])
                fund = name.replace(" unfunded-adjusted", "")
                key = (iid, period, form.group(1))
                if r["tag"].strip() == "M" or fund not in bdc[key]:
                    bdc[key][fund] = (price, name, r["source_url"])
            continue
        source = {"trade": "trace", "exchange": "exchange", "dealer": "dealer"}[r["kind"]]
        out[iid].append(Obs(iid, r["obs_date"], r["obs_date"], price, source, name, r["source_url"]))
    for (iid, period), funds in nport.items():
        public = period + timedelta(days=NPORT_LAG_DAYS)
        if public > as_of:
            continue
        prices = list(funds.values())
        out[iid].append(Obs(iid, period, public, statistics.median(prices), "nport",
                            f"N-PORT median of {len(prices)} funds ({period.isoformat()})", None, len(prices),
                            min(prices), max(prices)))
    for (iid, period), (price, n, name, url) in nport_median.items():
        public = period + timedelta(days=NPORT_LAG_DAYS)
        if (iid, period) in nport or public > as_of:
            continue
        out[iid].append(Obs(iid, period, public, price, "nport", name, url, n))
    for (iid, period, form), funds in bdc.items():
        public = period + timedelta(days=BDC_LAG_DAYS[form])
        if public > as_of:
            continue
        prices = [p for p, _, _ in funds.values()]
        out[iid].append(Obs(iid, period, public, statistics.median(prices), "bdc",
                            f"BDC {form} median of {len(prices)} funds ({period.isoformat()})", None, len(prices),
                            min(prices), max(prices)))
    result = {k: sorted(v, key=lambda o: (o.day, o.source)) for k, v in out.items()}
    if pit.auditing():
        for iid, items in result.items():
            for o in items:
                pit.note("price_obs", f"instrument:{iid}", o.source, o.day, o.public, o.source_name,
                         "fund mark: public after the filing lag" if o.stale else "")
    return result


def daily(obs: Sequence[Obs], sources: Sequence[str] = ("trace", "exchange", "nport")) -> dict[date, Obs]:
    """One observation per day for bond-implied intensities: the best source that day (TRACE, then an
    exchange quote, then a fund median)."""
    rank = {s: i for i, s in enumerate(sources)}
    best: dict[date, Obs] = {}
    for o in obs:
        if o.source not in rank:
            continue
        cur = best.get(o.day)
        if cur is None or (rank[o.source], -o.holders) < (rank[cur.source], -cur.holders):
            best[o.day] = o
    return best
