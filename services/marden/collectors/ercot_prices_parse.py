"""Parsers and daily statistics for ERCOT settlement point prices (collector: ercot_prices).

The Public API answers a query with {"_meta": {totalRecords, totalPages, currentPage, ...}, "report":
{"reportEMIL": ...}, "fields": [{"name": ...}, ...], "data": [[...], ...]}; columns are read by field name.
- NP4-190-CD DAM Settlement Point Prices: deliveryDate, hourEnding ("01:00".."24:00"), settlementPoint,
  settlementPointPrice, DSTFlag. One price per hour ending.
- NP6-905-CD Settlement Point Prices at Resource Nodes, Hubs and Load Zones (real time): deliveryDate,
  deliveryHour (1..24), deliveryInterval (1..4, 15 minutes each), settlementPoint, settlementPointType,
  settlementPointPrice, DSTFlag.
Times are ERCOT's operating day (US Central). On the fall-back day hour ending 02 appears twice, the repeat
flagged DSTFlag=true; on the spring-forward day one hour is missing, so a day has 23, 24 or 25 hours.

A point-day is summarised only when every hour (DAM) or every 15-minute interval (RT) of that day is present
exactly once; anything else is returned as incomplete and nothing is written for it. Statistics:
- avg: mean of the day's hourly (DAM) or 15-minute (RT) prices (equal-length periods, so time-weighted);
- on_peak_avg: mean over hours ending 07-22 every day (7x16 on-peak);
- max: the highest price;
- hourly (DAM): [[hour ending, price], ...] in delivery order (the repeated hour twice on the fall-back day);
- intervals_over_1000 (RT): the number of 15-minute intervals priced above 1,000 USD/MWh.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from dateutil import tz

from marden.collectors.rates_common import ParseError

CT = tz.gettz("America/Chicago")
ON_PEAK_HOURS = range(7, 23)  # hours ending 07..22
SPIKE_USD_MWH = Decimal(1000)
# ERCOT's offer cap is 5,000 USD/MWh and the RT floor -251; adders can push past the cap. Far outside this range
# means the feed changed shape, not a market move.
PRICE_RANGE = (Decimal(-2000), Decimal(20000))
AVG_PLACES = Decimal("0.0001")
DAM_EMIL = "NP4-190-CD"
RT_EMIL = "NP6-905-CD"
DAM_FIELDS = ("deliveryDate", "hourEnding", "settlementPoint", "settlementPointPrice", "DSTFlag")
RT_FIELDS = ("deliveryDate", "deliveryHour", "deliveryInterval", "settlementPoint", "settlementPointType",
             "settlementPointPrice", "DSTFlag")


@dataclass(frozen=True)
class Price:
    point: str
    day: date
    hour: int  # hour ending, 1..24
    interval: int  # 1..4 (RT); 0 for an hourly DAM price
    dst: bool  # the repeated hour on the fall-back day
    price: Decimal


@dataclass(frozen=True)
class Page:
    total_records: int
    total_pages: int
    current_page: int
    rows: list[Price]


@dataclass(frozen=True)
class DayStats:
    point: str
    day: date
    avg: Decimal
    on_peak_avg: Decimal
    max: Decimal
    periods: int
    hourly: list[list[Any]] | None = None  # DAM only
    intervals_over_1000: int | None = None  # RT only


def hours_in_day(day: date) -> int:
    """23, 24 or 25: the length of ERCOT's operating day in US Central time."""
    start = datetime.combine(day, time(0), CT)
    end = datetime.combine(day + timedelta(days=1), time(0), CT)
    return int((end.timestamp() - start.timestamp()) // 3600)


def _body(data: bytes, emil: str, wanted: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, int]]:
    try:
        body = json.loads(data)
        meta, fields, rows = body["_meta"], body["fields"], body["data"]
    except (ValueError, KeyError, TypeError):
        raise ParseError(f"ERCOT {emil} answer has no _meta / fields / data") from None
    got = str((body.get("report") or {}).get("reportEMIL") or "")
    if got.upper() != emil:
        raise ParseError(f"expected an ERCOT {emil} answer, got report {got!r}")
    index = {str(f.get("name")): i for i, f in enumerate(fields) if isinstance(f, dict)}
    missing = [f for f in wanted if f not in index]
    if missing or not isinstance(rows, list) or not isinstance(meta, dict):
        raise ParseError(f"ERCOT {emil} answer lacks fields {missing}")
    return body, index


def _price(value: Any, where: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        raise ParseError(f"{where}: price {value!r} is not a number") from None
    low, high = PRICE_RANGE
    if not out.is_finite() or not low <= out <= high:
        raise ParseError(f"{where}: price {value!r} is outside {low}..{high} USD/MWh")
    return out


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().upper()
    if text in ("Y", "TRUE"):
        return True
    if text in ("N", "FALSE", ""):
        return False
    raise ParseError(f"DSTFlag {value!r} is not a flag")


def _page(body: dict[str, Any], rows: list[Price]) -> Page:
    meta = body["_meta"]
    try:
        return Page(int(meta.get("totalRecords") or 0), int(meta.get("totalPages") or 0),
                    int(meta.get("currentPage") or 0), rows)
    except (TypeError, ValueError):
        raise ParseError("ERCOT answer has a malformed _meta block") from None


def dam_page(data: bytes) -> Page:
    body, ix = _body(data, DAM_EMIL, DAM_FIELDS)
    out = []
    for r in body["data"]:
        try:
            day = date.fromisoformat(str(r[ix["deliveryDate"]]))
            he = str(r[ix["hourEnding"]]).strip()
            hour = int(he.split(":")[0])
            point = str(r[ix["settlementPoint"]]).strip().upper()
        except (ValueError, IndexError, TypeError):
            raise ParseError(f"ERCOT DAM row {r!r} is malformed") from None
        if not 1 <= hour <= 24 or not he.endswith(":00"):
            raise ParseError(f"ERCOT DAM row {r!r}: hour ending {he!r}")
        out.append(Price(point, day, hour, 0, _flag(r[ix["DSTFlag"]]),
                         _price(r[ix["settlementPointPrice"]], f"{point} {day} HE{hour:02d}")))
    return _page(body, out)


def rt_page(data: bytes, types: frozenset[str] | None = None) -> Page:
    """Rows of the settlement point types asked for (`types`, e.g. {'LZ', 'HU'}); all rows when None."""
    body, ix = _body(data, RT_EMIL, RT_FIELDS)
    out = []
    for r in body["data"]:
        try:
            day = date.fromisoformat(str(r[ix["deliveryDate"]]))
            hour, interval = int(r[ix["deliveryHour"]]), int(r[ix["deliveryInterval"]])
            point = str(r[ix["settlementPoint"]]).strip().upper()
            kind = str(r[ix["settlementPointType"]]).strip().upper()
        except (ValueError, IndexError, TypeError):
            raise ParseError(f"ERCOT RT row {r!r} is malformed") from None
        if not (1 <= hour <= 24 and 1 <= interval <= 4):
            raise ParseError(f"ERCOT RT row {r!r}: hour {hour} interval {interval}")
        if types is not None and kind not in types:
            continue
        out.append(Price(point, day, hour, interval, _flag(r[ix["DSTFlag"]]),
                         _price(r[ix["settlementPointPrice"]], f"{point} {day} H{hour:02d}.{interval}")))
    return _page(body, out)


def _mean(values: list[Decimal]) -> Decimal:
    return (sum(values, Decimal(0)) / len(values)).quantize(AVG_PLACES)


def daily(rows: list[Price], *, per_hour: int) -> tuple[list[DayStats], list[dict[str, Any]]]:
    """Statistics for every complete point-day, and the incomplete point-days ({point, day, periods, expected,
    reason}). `per_hour` is 1 for DAM hourly prices and 4 for RT 15-minute prices."""
    groups: dict[tuple[str, date], dict[tuple[int, bool, int], Decimal]] = defaultdict(dict)
    clashes: set[tuple[str, date]] = set()
    for p in rows:
        slots = groups[(p.point, p.day)]
        key = (p.hour, p.dst, p.interval)
        if key in slots and slots[key] != p.price:
            clashes.add((p.point, p.day))
        slots[key] = p.price
    stats, incomplete = [], []
    for (point, day), slots in sorted(groups.items()):
        expected = hours_in_day(day) * per_hour
        if (point, day) in clashes or len(slots) != expected:
            reason = "two prices for one period" if (point, day) in clashes else "periods missing or extra"
            incomplete.append({"point": point, "day": day.isoformat(), "periods": len(slots), "expected": expected,
                               "reason": reason})
            continue
        ordered = [(key, slots[key]) for key in sorted(slots)]  # (hour, dst, interval): the repeat follows
        prices = [v for _, v in ordered]
        peak = [v for (hour, _, _), v in ordered if hour in ON_PEAK_HOURS]
        stat = DayStats(point, day, _mean(prices), _mean(peak), max(prices), len(prices))
        if per_hour == 1:
            stat = replace(stat, hourly=[[hour, v] for (hour, _, _), v in ordered])
        else:
            stat = replace(stat, intervals_over_1000=sum(v > SPIKE_USD_MWH for v in prices))
        stats.append(stat)
    return stats, incomplete
