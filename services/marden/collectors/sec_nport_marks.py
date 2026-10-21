"""Cross-fund N-PORT medians for the `sec_nport` collector (docs/methods/price-sources.md §4-5).

Per instrument and report period, over the fund series holding it (each series' latest filing for
the period, its lines combined as 100 x sum of value / sum of par; a non-USD line's mark is the
holding's `mark_price` fact when the row itself has none): the median as a `fund_mark`
price named 'N-PORT median of N funds (<period>)', and fact `fund_mark_dispersion` on the
instrument with n_funds, median, min, max, the range in points (flagged above 1 point) and the
first and last public (filing) dates, so a backtest knows when the median could have been known
(public_date is null when a constituent's filing date is unknown).
An instrument held under several CUSIPs also gets one median per CUSIP ('..., CUSIP <cusip>)').
Written when at least two funds report (with one, that fund's own row is the observation), the same
rule and names as the holdings seed loader (seed/holdings_prices.py), so equal medians are one row.
Recomputed every run from the stored holdings; unchanged values write nothing. Tag F.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from marden.collectors.sec_nport_parse import id_keys
from marden.collectors.sec_nport_store import FILING_TYPE, Watched
from marden.core.db import Conn
from marden.core.facts import FactWriter
from marden.core.records import PriceWriter
from marden.seed.instruments_nport import PRICE_QUANTUM

MIN_FUNDS = 2
WIDE_DISPERSION_PTS = Decimal(1)  # price-sources.md §5: flag dispersion above 1 point


def median_source_name(n: int, period: date, cusip: str | None = None) -> str:
    tail = f", CUSIP {cusip}" if cusip else ""
    return f"N-PORT median of {n} funds ({period.isoformat()}{tail})"


def fund_marks(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One mark per fund series: its latest filing's lines combined (par-weighted), with that
    filing's public date."""
    latest: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (r["fund_entity_id"], r["filed_at"] or date.min, r["accession"]),
                    reverse=True):
        cur = latest.setdefault(r["fund_entity_id"], {"accession": r["accession"], "filed_at": r["filed_at"],
                                                       "par": Decimal(0), "value": Decimal(0)})
        if cur["accession"] == r["accession"]:
            cur["par"] += r["par"]
            cur["value"] += r["par"] * r["mark_price"] / 100
    return [
        {"fund": f, "accession": v["accession"], "filed_at": v["filed_at"],
         "mark": (100 * v["value"] / v["par"]).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)}
        for f, v in sorted(latest.items())
    ]


def _public_dates(filed: list[date | None]) -> dict[str, Any]:
    """When the median became knowable: the last constituent's filing date (None when a line's filing
    date is unknown, e.g. a seeded line without one)."""
    known = [d for d in filed if d is not None]
    out: dict[str, Any] = {"first_public_date": min(known).isoformat() if known else None,
                           "public_date": max(known).isoformat() if len(known) == len(filed) else None}
    if len(known) < len(filed):
        out["funds_without_public_date"] = len(filed) - len(known)
    return out


@dataclass
class MedianWrite:
    groups: int = 0
    prices_new: int = 0
    facts_new: int = 0
    wide: list[str] = field(default_factory=list)


def _groups(rows: list[dict[str, Any]], watched: Watched) -> dict[tuple[str, date, str | None], list[dict[str, Any]]]:
    """(instrument, period, None) and, for multi-CUSIP instruments, (instrument, period, cusip)."""
    out: dict[tuple[str, date, str | None], list[dict[str, Any]]] = {}
    for r in rows:
        ids = {r["instrument_id"], *watched.instruments(id_keys(r["cusip"], r["isin"]))} - {None}
        for iid in ids:
            out.setdefault((iid, r["period_end"], None), []).append(r)
    for (iid, period, _), group in list(out.items()):
        cusips = sorted({r["cusip"] for r in group if r["cusip"]})
        if len(cusips) > 1:
            for cusip in cusips:
                out[(iid, period, cusip)] = [r for r in group if r["cusip"] == cusip]
    return out


# Every priced N-PORT line from `since`: the row's own mark and filing date, else (a row another
# loader wrote first) the holding facts this collector added for that line.
_ROWS = """
SELECT h.instrument_id, h.cusip, h.isin, h.fund_entity_id, h.accession, h.period_end, h.par,
       coalesce(h.mark_price, (mp.v)::numeric) AS mark_price, coalesce(h.filed_at, (pd.v)::date) AS filed_at
FROM holding h
LEFT JOIN LATERAL (SELECT f.value #>> '{}' AS v FROM fact f WHERE f.subject_type = 'holding'
                   AND f.subject_id = h.id::text AND f.field = 'mark_price' AND f.status = 'published'
                   ORDER BY f.learned_at DESC, f.id DESC LIMIT 1) mp ON h.mark_price IS NULL
LEFT JOIN LATERAL (SELECT f.value #>> '{}' AS v FROM fact f WHERE f.subject_type = 'holding'
                   AND f.subject_id = h.id::text AND f.field = 'public_date' AND f.status = 'published'
                   ORDER BY f.learned_at DESC, f.id DESC LIMIT 1) pd ON h.filed_at IS NULL
WHERE h.filing_type = %s AND h.period_end >= %s AND h.par > 0
  AND coalesce(h.mark_price, (mp.v)::numeric) IS NOT NULL
"""


def write_medians(conn: Conn, watched: Watched, *, since: date, producer: str) -> MedianWrite:
    """Medians and dispersion for every instrument and report period from `since` on."""
    rows = conn.execute(_ROWS, (FILING_TYPE, since)).fetchall()
    out = MedianWrite()
    facts, prices = FactWriter(conn), PriceWriter(conn)
    for (iid, period, cusip), group in sorted(_groups(rows, watched).items(), key=lambda kv: (kv[0][0], kv[0][1],
                                                                                             kv[0][2] or "")):
        marks = fund_marks(group)
        if len(marks) < MIN_FUNDS:
            continue
        values = sorted(m["mark"] for m in marks)
        med = Decimal(statistics.median(values)).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)
        low, high = values[0], values[-1]
        name = median_source_name(len(values), period, cusip)
        out.groups += 1
        out.prices_new += prices.add(iid, period, "fund_mark", med, tag="F", source_name=name).created
        wide = high - low > WIDE_DISPERSION_PTS
        if wide:
            out.wide.append(f"{iid} {period.isoformat()}{f' {cusip}' if cusip else ''}: {low}..{high} "
                            f"({len(values)} funds)")
        value = {
            "period": period.isoformat(), "n_funds": len(values), "median": med, "min": low, "max": high,
            "range_pts": high - low, "wide": wide, "cusip": cusip,
            **_public_dates([m["filed_at"] for m in marks]),
        }
        out.facts_new += facts.add(
            "instrument", iid, "fund_mark_dispersion" if cusip is None else "fund_mark_dispersion_cusip", value,
            tag="F", source_name=name, valid_from=period, producer=producer, status="published", unit="pct_of_par",
        ).created
    return out
