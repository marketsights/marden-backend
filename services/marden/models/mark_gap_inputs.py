"""Inputs of the `mark_gap` model, read point in time (what was public on the run's as-of day).

- Holder lines: `holding` rows linked to the instrument from fund filings that name holders (N-PORT,
  BDC 10-Q / 10-K schedules; insurer NAIC lines are held for legal review, price-sources.md section 3).
  A filing is usable once public: an N-PORT report 60 days after its period end (pricing-v0.1 "Data";
  never before its filing date), a BDC schedule on its filing date (else 45 days after the period end,
  the 10-Q deadline). Each fund's latest usable filing that lists the instrument counts, and only while
  it is also the fund's newest usable filing (a newer filing without the line means the fund sold).
- Marks: the line's filed price (100 x value / par); a non-USD line carries its mark in its own
  currency (holding fact `mark_price`) and its par is converted to USD as value / mark.
- Fund net assets from the same filing: the line's `fund_net_assets` (BDC), else the fund's
  `net_assets_usd` fact for that period (N-PORT), else value / pctVal of the same line (N-PORT).
- The Marden price: the newest 'price' model output on or before the day (`mardenPrice` / `price`),
  else the newest Marden daily price (`price_obs` kind 'marden').
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit
from marden.models.sourced import num
from marden.models.status_inputs import price_value

HOLDER_FILINGS = ("N-PORT", "10-Q", "10-K")
BDC_LAG_DAYS = 45  # 10-Q due 40 (large accelerated) to 45 days after the quarter
PRICE_MODEL = "price"


def line_public_date(filing_type: str, period_end: date, filed_at: date | None) -> date:
    """The day a filed holding line became usable."""
    if filing_type == "N-PORT":
        lagged = period_end + timedelta(days=pit.NPORT_LAG_DAYS)
        return max(lagged, filed_at) if filed_at else lagged
    return filed_at or period_end + timedelta(days=BDC_LAG_DAYS)


@dataclass
class Holder:
    fund_id: str
    name: str
    filing_type: str
    filing_url: str
    period_end: date
    filed_at: date | None
    par_usd: float = 0.0
    marked_par_usd: float = 0.0
    marked_value_usd: float = 0.0
    net_assets: float | None = None
    net_assets_basis: str | None = None
    lines: list[int] = field(default_factory=list)

    @property
    def last_mark(self) -> float | None:
        return 100.0 * self.marked_value_usd / self.marked_par_usd if self.marked_par_usd > 0 else None


@dataclass
class Price:
    value: float
    as_of: date
    method: str | None
    source_url: str | None
    origin: str


@dataclass
class Inputs:
    as_of: date
    holders: dict[str, list[Holder]]  # instrument id -> holders (latest usable filing per fund)
    prices: dict[str, Price]
    unpriced_lines: dict[str, int]  # instrument id -> lines without a usable mark


def _lines(conn: Conn, ids: Sequence[str] | None, day: date) -> list[Row]:
    return conn.execute(
        """SELECT h.id, h.instrument_id, h.fund_entity_id, e.name AS fund_name, h.period_end, h.filing_type,
                  h.filing_url, h.filed_at, h.par, h.value_usd, h.mark_price, h.pct_nav, h.fund_net_assets
             FROM holding h JOIN entity e ON e.id = h.fund_entity_id
            WHERE h.instrument_id IS NOT NULL AND h.filing_type = ANY(%(filings)s) AND h.period_end <= %(day)s
              AND (%(ids)s::text[] IS NULL OR h.instrument_id = ANY(%(ids)s))
            ORDER BY h.instrument_id, h.fund_entity_id, h.period_end DESC, h.id""",
        {"filings": list(HOLDER_FILINGS), "day": day, "ids": None if ids is None else list(ids)},
    ).fetchall()


def _fund_newest(conn: Conn, funds: Sequence[str], day: date) -> dict[str, date]:
    """Each fund's newest usable filing period (over every line it filed, linked or not)."""
    rows = conn.execute(
        """SELECT fund_entity_id, period_end, filing_type, min(filed_at) AS filed_at FROM holding
            WHERE fund_entity_id = ANY(%s) AND filing_type = ANY(%s) AND period_end <= %s
            GROUP BY 1, 2, 3""",
        (list(funds), list(HOLDER_FILINGS), day),
    ).fetchall()
    newest: dict[str, date] = {}
    for r in rows:
        if line_public_date(r["filing_type"], r["period_end"], r["filed_at"]) <= day:
            newest[r["fund_entity_id"]] = max(newest.get(r["fund_entity_id"], r["period_end"]), r["period_end"])
    return newest


def _holding_facts(conn: Conn, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Per-line filed attributes the line row lacks: currency and the mark of a non-USD line."""
    if not ids:
        return {}
    rows = conn.execute(
        """SELECT DISTINCT ON (subject_id, field) subject_id, field, value FROM fact
            WHERE subject_type = 'holding' AND subject_id = ANY(%s) AND field IN ('cur_cd', 'mark_price')
              AND status IN ('published', 'superseded')
            ORDER BY subject_id, field, valid_from DESC, id DESC""",
        ([str(i) for i in ids],),
    ).fetchall()
    out: dict[int, dict[str, Any]] = defaultdict(dict)
    for r in rows:
        out[int(r["subject_id"])][r["field"]] = r["value"]
    return out


def _net_assets_facts(conn: Conn, day: date, funds: Sequence[str]) -> dict[tuple[str, date], float]:
    rows = pit.fact_rows(conn, day, "entity", list(funds), ["net_assets_usd"]) if funds else []
    out: dict[tuple[str, date], float] = {}
    for r in rows:
        value = num(r["value"])
        if value and value > 0:
            out.setdefault((r["subject_id"], r["valid_from"]), value)
    return out


def _add_line(h: Holder, line: Row, facts: dict[str, Any]) -> bool:
    """Add one line's par and mark to its holder. Returns False when the line has no usable mark."""
    h.lines.append(int(line["id"]))
    par, value = num(line["par"]), num(line["value_usd"])
    currency = facts.get("cur_cd")
    mark = num(line["mark_price"])
    if mark is None and currency not in (None, "USD"):
        mark = num(facts.get("mark_price"))
    if mark is None or mark <= 0 or value is None or value <= 0:
        if par:
            h.par_usd += par if currency in (None, "USD") else 0.0
        return False
    par_usd = par if currency in (None, "USD") and par else value * 100.0 / mark
    h.par_usd += par_usd
    h.marked_par_usd += par_usd
    h.marked_value_usd += value
    return True


def _net_assets(h: Holder, line: Row, nav_facts: dict[tuple[str, date], float]) -> None:
    if h.net_assets is not None:
        return
    filed = num(line["fund_net_assets"])
    if filed and filed > 0:
        h.net_assets, h.net_assets_basis = filed, "filed net assets (schedule of investments)"
        return
    fact = nav_facts.get((h.fund_id, h.period_end))
    if fact:
        h.net_assets, h.net_assets_basis = fact, "filed net assets (N-PORT)"
        return
    pct, value = num(line["pct_nav"]), num(line["value_usd"])
    if pct and pct > 0 and value and value > 0:
        h.net_assets, h.net_assets_basis = value * 100.0 / pct, "value / pctVal of the same filing"


def holders(conn: Conn, day: date, ids: Sequence[str] | None) -> tuple[dict[str, list[Holder]], dict[str, int]]:
    lines = [r for r in _lines(conn, ids, day)
             if line_public_date(r["filing_type"], r["period_end"], r["filed_at"]) <= day]
    funds = sorted({r["fund_entity_id"] for r in lines})
    newest = _fund_newest(conn, funds, day)
    facts = _holding_facts(conn, [int(r["id"]) for r in lines])
    nav_facts = _net_assets_facts(conn, day, funds)
    latest: dict[tuple[str, str], date] = {}
    for r in lines:
        key = (r["instrument_id"], r["fund_entity_id"])
        latest[key] = max(latest.get(key, r["period_end"]), r["period_end"])
    out: dict[str, dict[str, Holder]] = defaultdict(dict)
    unpriced: dict[str, int] = defaultdict(int)
    for r in lines:
        iid, fund = r["instrument_id"], r["fund_entity_id"]
        if r["period_end"] != latest[(iid, fund)] or newest.get(fund) != r["period_end"]:
            continue
        h = out[iid].get(fund)
        if h is None:
            h = out[iid][fund] = Holder(fund, r["fund_name"], r["filing_type"], r["filing_url"], r["period_end"],
                                        r["filed_at"])
        if not _add_line(h, r, facts.get(int(r["id"]), {})):
            unpriced[iid] += 1
        _net_assets(h, r, nav_facts)
    return {iid: list(by_fund.values()) for iid, by_fund in out.items()}, dict(unpriced)


def prices(conn: Conn, day: date, ids: Sequence[str] | None) -> dict[str, Price]:
    """The Marden price per instrument on or before `day`."""
    params = {"model": PRICE_MODEL, "day": day, "ids": None if ids is None else list(ids)}
    out: dict[str, Price] = {}
    for r in conn.execute(
        """SELECT DISTINCT ON (subject_id) subject_id, as_of, version, output FROM model_output
            WHERE model = %(model)s AND subject_type = 'instrument' AND as_of <= %(day)s
              AND (%(ids)s::text[] IS NULL OR subject_id = ANY(%(ids)s))
            ORDER BY subject_id, as_of DESC, created_at DESC, id DESC""",
        params,
    ).fetchall():
        value, method, url = price_value(r["output"])
        if value is not None:
            out[r["subject_id"]] = Price(value, r["as_of"], method or f"{PRICE_MODEL} {r['version']}", url,
                                         "model_output:price")
    for r in conn.execute(
        """SELECT DISTINCT ON (instrument_id) instrument_id, obs_date, price, method_version, source_url
             FROM price_obs WHERE kind = 'marden' AND obs_date <= %(day)s AND price IS NOT NULL
              AND (%(ids)s::text[] IS NULL OR instrument_id = ANY(%(ids)s))
            ORDER BY instrument_id, obs_date DESC, obs_at DESC NULLS LAST, id DESC""",
        params,
    ).fetchall():
        if r["instrument_id"] not in out:
            out[r["instrument_id"]] = Price(float(r["price"]), r["obs_date"], r["method_version"], r["source_url"],
                                            "price_obs:marden")
    return out


def load(conn: Conn, day: date, ids: Sequence[str] | None = None) -> Inputs:
    by_instrument, unpriced = holders(conn, day, ids)
    return Inputs(day, by_instrument, prices(conn, day, ids), unpriced)
