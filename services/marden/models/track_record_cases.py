"""The backtest's locked cases and the market side: when each case's market price moved.

Written and locked on 2026-10-21, before the first run (pricing-v0.1 "Backtesting": assumptions are
locked in writing before a backtest runs). Changing anything here is a new method version.

- Window: 2026-04-01 (WINDOW_START) to the run's as-of day. Each instrument's baseline is its state at
  the start of the window, so the backtest covers the Q2-Q4 2026 repricings; the Q1 2026 neocloud dip
  is before it.
- Cases (CASES): every V1 instrument except convertibles (their price follows the stock), grouped where
  one name has several notes (CoreWeave unsecured, Oracle notes, the CoreWeave DDTLs, xAI VCI), plus
  the repricings spec 00a names (Jupiter, CoreWeave unsecured, Anthropic junior, Nscale) even without
  an instrument or a price on file. A case whose market moved in the window is a past repricing; one
  that did not is a control (a flag on it is a false alarm).
- Market move (spec 12: "price below 95 or spread +200bp"): the first observation in the window,
  public by the as-of day, where
  * the price is below 95 and the instrument's baseline price was 95 or more, or
  * the spread is 200bp or more above the baseline spread (TRACE prints carry the spread);
  a single TRACE print must be confirmed by the instrument's next print (a fund mark, BDC mark, dealer
  quote or exchange quote counts on its own: it is already an aggregate or a reported level).
- Observations: price_obs kinds trade, dealer, quote, exchange and fund_mark (never 'marden' or
  'legacy'); one value per instrument and day: the TRACE median if there are prints, else dealer and
  exchange quotes, else the median fund mark (a BDC mark adjusted for the unfunded commitment replaces
  the same fund's raw fair-value-over-commitment). Public dates: an N-PORT mark 60 days after its period
  end, a BDC 10-Q 45 days and a 10-K 90 days after the period end, others on their own date.
  MARDEN_SHOW_UNLICENSED_PRICES=false drops the unlicensed sources (TRACE, Börse Frankfurt).
- Baseline: the median of the instrument's first three observations in the window. A loan (construction
  loan, term loan, DDTL) with no observation in the window's first 30 days is taken at par (loans are
  syndicated at par: Jupiter priced at SOFR+250 at par on 2025-11-07, pricing-v0.1).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from marden.core.db import Conn
from marden.core.licensing import show_unlicensed_prices, unlicensed_source_names

WINDOW_START = date(2026, 4, 1)
PRICE_TRIGGER = 95.0
SPREAD_TRIGGER_BP = 200.0
CREDIT_DRIVEN_BP = 100.0  # a price move with less spread widening than this is mostly the Treasury move
BASELINE_OBS = 3
LOAN_PAR_DAYS = 30
CONFIRM_DAYS = 10  # the confirming TRACE print must come within this many days
LOAN_KINDS = frozenset({"construction_loan", "term_loan", "ddtl"})
MARKET_KINDS = ("trade", "dealer", "quote", "exchange", "fund_mark")
NPORT_LAG, BDC_10Q_LAG, BDC_10K_LAG = 60, 45, 90


@dataclass(frozen=True)
class Case:
    key: str
    name: str
    instruments: tuple[str, ...]
    named: bool = False  # spec 00a names it as a past repricing
    entities: tuple[str, ...] = ()  # chain-party entities whose sites carry the signal (no instrument site)
    note: str | None = None


CASES: tuple[Case, ...] = (
    Case("jupiter", "Jupiter construction loans", ("EX-JUPITER-190",), named=True),
    Case("coreweave-unsecured", "CoreWeave unsecured notes",
         ("EX-CRWV-101", "EX-CRWV-102", "EX-CRWV-103", "EX-CRWV-104", "EX-CRWV-105"), named=True),
    Case("anthropic-junior", "Anthropic junior chip paper", (), named=True, entities=("anthropic",),
         note="the ~$35bn Anthropic TPU financing (issue_comp:ANTH_COMPUTE_SPV_35B) has no instrument or "
              "dated price on file"),
    Case("nscale", "Nscale GPU facilities", ("EX-NSCALE-205", "EX-NSCALE-210", "EX-NSCALE-211", "EX-NSCALE-212"),
         named=True),
    Case("oracle-notes", "Oracle senior notes",
         ("EX-ORCL-228", "EX-ORCL-239", "EX-ORCL-240", "EX-ORCL-261", "EX-ORCL-262")),
    Case("abilene", "Abilene construction loans", ("EX-ABILENE-000", "EX-ABILENE-001")),
    Case("frontier", "Frontier and Lighthouse construction debt", ("EX-VANTAGE_ORCL-366",)),
    Case("vantage-holdco", "Vantage HoldCo senior sub loan", ("EX-VANTAGE-361",)),
    Case("coreweave-ddtl", "CoreWeave DDTLs 1.0-5.5",
         ("EX-CRWV-091", "EX-CRWV-092", "EX-CRWV-093", "EX-CRWV-094", "EX-CRWV-095", "EX-CRWV-096",
          "EX-CRWV-097")),
    Case("apld-2030", "Applied Digital 9.25% 2030", ("EX-APLD-010",)),
    Case("apld-pf2", "Applied Digital Polaris Forge 2 6.75% 2031", ("EX-APLD-011",)),
    Case("apld-pf1-b4", "Applied Digital Polaris Forge 1 Bldg 4 7.00% 2031", ("EX-APLD-012",)),
    Case("hyperion", "Meta Hyperion (Beignet) 6.581% 2049", ("EX-BEIGNET-022",)),
    Case("meta-el-paso", "Meta El Paso (Sopaipilla) 7.534% 2048", ("EX-SOPAIPILLA-309",)),
    Case("barber-lake", "Cipher Barber Lake 7.125% 2030", ("EX-CIFR-027",)),
    Case("black-pearl", "Cipher Black Pearl 6.125% 2031", ("EX-CIFR-028",)),
    Case("core-scientific", "Core Scientific 7.75% 2031", ("EX-CORZ-065",)),
    Case("abernathy", "Fluidstack Abernathy (Flash Compute) 7.25% 2030", ("EX-FLUIDSTACK-156",)),
    Case("helios-1", "Galaxy Helios I term loan", ("EX-GLXY-162",)),
    Case("helios-2", "Galaxy Helios II 9.875% 2031", ("EX-GLXY-163",)),
    Case("river-bend", "Hut 8 River Bend 6.192% 2042", ("EX-HUT-171",)),
    Case("beacon-point", "Hut 8 Beacon Point 6.129% 2042", ("EX-HUT-172",)),
    Case("childress", "IREN Childress DDTL and 5.96% USPP", ("EX-IREN-184", "EX-IREN-185")),
    Case("nebius", "Nebius secured term loan", ("EX-NBIS-204",)),
    Case("the-barn", "The Barn project bonds", ("EX-SALINE-308",)),
    Case("lake-mariner", "TeraWulf Lake Mariner 7.75% 2030", ("EX-WULF-369",)),
    Case("colossus-2", "xAI Colossus 2 VCI loans and leases",
         ("EX-XAI_VCI-383", "EX-XAI_VCI-388", "EX-XAI_VCI-389", "EX-XAI_VCI-390")),
    Case("edged", "Edged 7.5% green notes 2031", ("MAN-EDGED-75SeniorSecuredGreenNote",)),
)

ASSUMPTIONS: tuple[str, ...] = (
    f"window {WINDOW_START.isoformat()} to the as-of day; baselines are each instrument's state at the window start",
    "market move: price below 95 from a baseline of 95 or more, or spread 200bp or more over the baseline "
    "(spec 12); a TRACE print needs the next print to confirm it",
    "N-PORT marks count 60 days after the period end, BDC 10-Q marks 45 days and 10-K marks 90 days after",
    "loans with no observation in the window's first 30 days start at par",
    "Marden signals, each against its own state at the window start: chain status worse (any site of the "
    "case), Marden price below 95 (any instrument that was 95 or more), cushion band into 0-3 or below zero",
    "signals are computed point in time (marden.models.pit) weekly from the window start, then daily in the "
    "week before the first one; a day with a live model run uses that run's stored output (a snapshot)",
    "lead days = move date - first signal date; a signal after the move is a late call, none is a miss",
    "convertible notes are left out: their price follows the stock",
    "a move is labelled credit-driven when the spread at the move is 100bp or more over its baseline, "
    "rates-driven when less, n/d without a spread",
)


@dataclass
class Obs:
    day: date
    price: float | None
    spread: float | None
    kind: str
    source: str
    public: date


@dataclass
class Move:
    day: date
    instrument_id: str
    rule: str  # 'price<95' | 'spread+200'
    price: float | None
    spread: float | None
    baseline_price: float | None
    baseline_spread: float | None
    baseline_basis: str
    source: str
    kind: str
    public: date

    @property
    def driver(self) -> str:
        if self.spread is None or self.baseline_spread is None:
            return "n/d"
        return "credit" if self.spread - self.baseline_spread >= CREDIT_DRIVEN_BP else "rates"

    def doc(self) -> dict[str, Any]:
        return {"date": self.day.isoformat(), "instrumentId": self.instrument_id, "rule": self.rule,
                "driver": self.driver,
                "price": _r(self.price, 3), "spreadBp": _r(self.spread, 0), "baselinePrice": _r(self.baseline_price, 3),
                "baselineSpreadBp": _r(self.baseline_spread, 0), "baselineBasis": self.baseline_basis,
                "source": self.source, "kind": self.kind, "public": self.public.isoformat()}


@dataclass
class Market:
    """One case's market side: the move (or None), what each instrument started from, what is missing."""

    move: Move | None
    baselines: dict[str, dict[str, Any]] = field(default_factory=dict)
    observed: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def graded(self) -> bool:
        """A market price was observed in the window (so 'no move' is a finding, not a gap)."""
        return any(self.observed.values())


def _r(value: float | None, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def public_day(row: dict[str, Any]) -> date:
    day: date = row["obs_date"]
    source = row["source_name"] or ""
    if source.startswith("N-PORT"):
        return day + timedelta(days=NPORT_LAG)
    if source.startswith("BDC 10-K"):
        return day + timedelta(days=BDC_10K_LAG)
    if source.startswith("BDC"):
        return day + timedelta(days=BDC_10Q_LAG)
    return day


def _fund(source: str) -> str:
    return source.replace(" unfunded-adjusted", "").split(", adjusted for")[0].strip()


def _daily(rows: list[dict[str, Any]]) -> Obs:
    """One observation per instrument and day from that day's rows."""
    trades = [r for r in rows if r["kind"] == "trade"]
    quotes = [r for r in rows if r["kind"] in ("dealer", "quote", "exchange")]
    pick = trades or quotes
    if not pick:  # fund marks: an unfunded-adjusted mark replaces the same fund's raw one
        funds = [r for r in rows if " median of " not in r["source_name"]] or rows  # not the loader's medians too
        adjusted = {_fund(r["source_name"]) for r in funds if _fund(r["source_name"]) != r["source_name"]}
        pick = [r for r in funds if _fund(r["source_name"]) != r["source_name"] or r["source_name"] not in adjusted]
    prices = [float(r["price"]) for r in pick if r["price"] is not None]
    spreads = [float(r["spread_bp"]) for r in pick if r["spread_bp"] is not None]
    kinds = sorted({r["kind"] for r in pick})
    sources = sorted({r["source_name"] for r in pick})
    label = sources[0] if len(sources) == 1 else f"median of {len(sources)} {kinds[0]} sources"
    return Obs(rows[0]["obs_date"], statistics.median(prices) if prices else None,
               statistics.median(spreads) if spreads else None, kinds[0], label, max(public_day(r) for r in pick))


def market_series(conn: Conn, ids: Sequence[str], as_of: date) -> dict[str, list[Obs]]:
    """Each instrument's daily observations in the window that were public by `as_of`, oldest first."""
    if not ids:
        return {}
    params: dict[str, Any] = {"ids": list(ids), "start": WINDOW_START, "day": as_of, "kinds": list(MARKET_KINDS)}
    sql = """SELECT instrument_id, obs_date, kind, price, spread_bp, source_name FROM price_obs
              WHERE instrument_id = ANY(%(ids)s) AND kind = ANY(%(kinds)s) AND obs_date BETWEEN %(start)s AND %(day)s"""
    if not show_unlicensed_prices():
        sql += " AND source_name <> ALL(%(hidden)s)"
        params["hidden"] = unlicensed_source_names()
    grouped: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute(sql + " ORDER BY instrument_id, obs_date, id", params).fetchall():
        if public_day(r) <= as_of:
            grouped[(r["instrument_id"], r["obs_date"])].append(r)
    out: dict[str, list[Obs]] = defaultdict(list)
    for (iid, _), rows in sorted(grouped.items()):
        out[iid].append(_daily(rows))
    return dict(out)


def baseline(series: list[Obs], kind: str) -> tuple[float | None, float | None, str, int]:
    """(price, spread, basis, number of observations used) at the window start."""
    if kind in LOAN_KINDS and not any(o.day <= WINDOW_START + timedelta(days=LOAN_PAR_DAYS) for o in series):
        return 100.0, None, "par (a loan with no observation in the window's first 30 days)", 0
    first = series[:BASELINE_OBS]
    prices = [o.price for o in first if o.price is not None]
    spreads = [o.spread for o in series if o.spread is not None][:BASELINE_OBS]  # prints carry it a few days in
    if not first:
        return None, None, "no observation in the window", 0
    basis = f"median of the first {len(first)} observations from {first[0].day.isoformat()}"
    return (statistics.median(prices) if prices else None, statistics.median(spreads) if spreads else None,
            basis, len(first))


def _confirmed(series: list[Obs], i: int, test: Any) -> bool:
    obs = series[i]
    if obs.kind != "trade":
        return True
    nxt = next((o for o in series[i + 1:] if o.kind == "trade"), None)
    return nxt is not None and (nxt.day - obs.day).days <= CONFIRM_DAYS and test(nxt)


def detect_move(iid: str, kind: str, series: list[Obs]) -> tuple[Move | None, dict[str, Any]]:
    """The instrument's first market move in the window, and its baseline."""
    base_px, base_sp, basis, used = baseline(series, kind)
    base = {"price": _r(base_px, 3), "spreadBp": _r(base_sp, 0), "basis": basis}
    tests = []
    if base_px is not None and base_px >= PRICE_TRIGGER:
        tests.append(("price<95", lambda o: o.price is not None and o.price < PRICE_TRIGGER))
    if base_sp is not None:
        tests.append(("spread+200", lambda o: o.spread is not None and o.spread >= base_sp + SPREAD_TRIGGER_BP))
    for i in range(used, len(series)):
        for rule, test in tests:
            if test(series[i]) and _confirmed(series, i, test):
                o = series[i]
                return Move(o.day, iid, rule, o.price, o.spread, base_px, base_sp, basis, o.source, o.kind,
                            o.public), base
    return None, base


def case_market(case: Case, kinds: dict[str, str], series: dict[str, list[Obs]]) -> Market:
    market = Market(None)
    if not case.instruments:
        market.missing.append(f"{case.name}: {case.note or 'no instrument on file'}")
        return market
    for iid in case.instruments:
        obs = series.get(iid, [])
        market.observed[iid] = len(obs)
        move, base = detect_move(iid, kinds.get(iid, ""), obs)
        market.baselines[iid] = base
        if not obs and kinds.get(iid) not in LOAN_KINDS:
            market.missing.append(f"{iid}: no market observation in the window")
        if move is not None and (market.move is None or move.day < market.move.day):
            market.move = move
    if market.move is None and not any(market.observed.values()):
        market.missing.append(f"{case.name}: no dated market price on file")
    return market


def case_instruments(conn: Conn, cases: Sequence[Case]) -> dict[str, dict[str, Any]]:
    """instrument id -> {kind, site_slug, issuer} for every case instrument that exists."""
    ids = sorted({i for c in cases for i in c.instruments})
    rows = conn.execute(
        "SELECT id, kind, site_slug, issuer_entity_id FROM instrument WHERE id = ANY(%s)", (ids,)
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def case_sites(conn: Conn, case: Case, insts: dict[str, dict[str, Any]]) -> list[str]:
    """The sites whose status and cushion carry the case's signal: the instruments' own sites; for an
    instrument with no site, the sites where its issuer (or the case's entities) is a chain party."""
    slugs = {insts[i]["site_slug"] for i in case.instruments if i in insts and insts[i]["site_slug"]}
    entities = set(case.entities) | {insts[i]["issuer_entity_id"] for i in case.instruments
                                     if i in insts and not insts[i]["site_slug"] and insts[i]["issuer_entity_id"]}
    if entities:
        rows = conn.execute("SELECT DISTINCT site_slug FROM chain_party WHERE entity_id = ANY(%s)",
                            (sorted(entities),)).fetchall()
        slugs |= {r["site_slug"] for r in rows}
    return sorted(slugs)
