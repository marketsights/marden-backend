"""Model `yield` (docs/methods/yield-v0.1.md): yield to maturity, yield to worst and, for floating-rate debt,
the discount margin, worked from every Marden daily price and from the latest market price.

    cd services && uv run python -m marden model yield [--as-of YYYY-MM-DD] [--instrument ID ...] [--json]

Terms are the price engine's (price_terms) and the schedule its (price_bonds): a fixed coupon pays twice a
year on dates counted back from maturity, accrued 30/360; a floating coupon pays quarterly, Act/360, at the
current coupon held flat: the latest SOFR fixing on the price date (NY Fed) plus the margin, with a stated
SOFR floor applied (the street convention for a loan's yield and discount margin; the price engine itself
projects SOFR along the forward curve). Principal is bullet, as in price v0.1. Every yield is the internal
rate of those flows against the dirty price, compounded twice a year (bond-equivalent, so fixed and floating
yields compare), settling on the price date:

- YTM: to the stated maturity, redeemed at par.
- YTW: the lowest yield over the stated maturity and the dates the issuer or borrower may choose: each
  extension option (the coupon text or research terms: 'two 1-yr extensions'), a call stated in the
  research terms ('Callable at par from 15 May 2042'), and for a bank loan (construction loan, term loan,
  DDTL) prepayment at par on any interest date; and a holder put stated on a date ('Holders may require
  repurchase at par on 1 Jun 2029'). A put on a fundamental change has no date and is noted, not worked.
  A call the archive only presumes ('First call presumed about Aug 2028 at 104.94 (model assumption)') is
  worked and shown but left out of YTW; a make-whole call never sets YTW. With no call or extension in the
  sources, YTW = YTM and the output says so.
- DM (floating): the spread over SOFR (held at the current fixing) that discounts the flows to the dirty
  price.

One `model_output` per instrument (model 'yield', version VERSION, subject ('instrument', id)): the
headline yields at the latest Marden price (Sourced, tag M), else at the latest market price (a TRACE
trade, a BondTerminal or Börse Frankfurt quote, a dealer quote: price_obs, worked on that observation's
date and tagged with its tag, F or R, and source), the dates worked, the assumptions in plain words, the
inputs used (each sourced), one point per Marden daily price and the yields at the latest market price.
No price or no terms: the yields are null (n/d) and `why` says what is missing, in plain words. A
convertible is worked as a straight bond: the yield on the trade price, ignoring the conversion option
(`note` says so). Non-USD notes and securitizations are not worked.

Runs daily 09:44 UTC (after the 09:40 price run) and within a minute of a new price output, a price
observation, an instrument's terms or a SOFR fixing (`models.run_dirty`).
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from dateutil.relativedelta import relativedelta
from scipy.optimize import brentq

from marden import cli
from marden.core.db import Conn, Row
from marden.core.licensing import show_unlicensed_prices, unlicensed_source_names
from marden.core.queue import app
from marden.models import pit, triggers
from marden.models.price import HISTORY_START, targets
from marden.models.price_bonds import Flows, Terms, days_30_360, previous_coupon, schedule, yield_to_maturity
from marden.models.price_curve import CurveBook, load_curves, year_frac
from marden.models.price_terms import Instrument, attach, load_comps, load_instruments, note_terms
from marden.models.sourced import Batch, m_value, method_version, run_outputs
from marden.readmodels.context import business_today

log = logging.getLogger(__name__)

MODEL = "yield"
VERSION = "0.1"
METHOD = method_version(MODEL, VERSION)
METHOD_URL = "docs/methods/yield-v0.1.md"
LABEL = "Marden yield model"
CRON_UTC = "44 9 * * *"

LOAN_KINDS = ("construction_loan", "term_loan", "ddtl")  # prepayable at par on an interest date (assumed)
SECURITIZED = ("abs", "cmbs")
OBSERVED_KINDS = ("trade", "exchange", "dealer", "quote")
MARKET_RANK = {"quote": 0, "dealer": 1, "exchange": 2, "trade": 3}  # same day: a trade over a quote
EXTRA_FIELDS = ("research_terms", "maturity_text", "amortization", "sofr_floor_pct", "covenants")
PERPETUAL_YEAR = 2100  # a maturity this far out is the loaders' perpetual placeholder (3999-12-31)
_GAP_WORDS = {"coupon terms": "coupon", "coupon rate": "coupon rate", "floating margin": "floating margin",
              "maturity": "maturity", "terms": "coupon and maturity"}

_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "six": 6, "twelve": 12}
_EXTENSION = re.compile(r"\b(a|an|one|two|three|four|\d)\s+(one|two|three|six|twelve|\d+)[- ]?(yr|year|month|mo)s?"
                        r"\b[- ]?extensions?", re.I)
_DAY_MONTH = r"\d{1,2} [A-Z][a-z]{2,8} \d{4}|[A-Z][a-z]{2,8} \d{1,2},? \d{4}"
_CALL = re.compile(rf"callable at (par|\d{{2,3}}(?:\.\d+)?) (?:from|on or after|after) ({_DAY_MONTH})", re.I)
_PRESUMED = re.compile(r"first call presumed (?:in )?(?:about )?([A-Z][a-z]{2,8} \d{4}) at (\d{2,3}(?:\.\d+)?)", re.I)
_ASSUMED = re.compile(r"assumes a first call at (\d{2,3}(?:\.\d+)?)", re.I)
_MAKE_WHOLE = re.compile(rf"make[- ]whole[^.;]*?(?:before|until|through) ({_DAY_MONTH})", re.I)
_NO_CALL = re.compile(r"call terms not in the sources", re.I)
_AMORTIZES = re.compile(r"\bamortiz", re.I)
_NOT_DISCLOSED = re.compile(r"amortization not disclosed", re.I)
_FLOOR = re.compile(r"(\d+(?:\.\d+)?)% floor", re.I)  # 'Term SOFR with a 2.50% floor'
_PUT = re.compile(r"\b(?:put(?:t?able)?|holders?\W{0,2} (?:may|can|have the right to|option to) (?:require|put)|"
                  r"repurchase (?:right|option)|holder put)\b", re.I)
_PUT_DATE = re.compile(rf"\b(?:on|in) ({_DAY_MONTH}|[A-Z][a-z]{{2,8}} \d{{4}})")
_PUT_PRICE = re.compile(r"\bat (par|\d{2,3}(?:\.\d+)?)(?:%|\b)(?! [A-Z][a-z]{2,8}\b)", re.I)
_CONTINGENT = re.compile(r"fundamental change|change of control|delisting", re.I)
_SENTENCE = re.compile(r"(?<!\d)[.;](?!\d)")
_FIXING = re.compile(r"\((?:~)?(\d+(?:\.\d+)?)% at (\d{1,2} [A-Z][a-z]{2,8}(?: \d{4})?)")  # '(6.14% at 30 Jun)'


def when(d: date) -> str:
    """'Apr 15, 2030' (spec 00 Formats, long date)."""
    return f"{d:%b} {d.day}, {d.year}"


def _parse_day(text: str) -> date | None:
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_month(text: str) -> date | None:
    for fmt in ("%b %Y", "%B %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _count(word: str) -> int | None:
    w = word.lower()
    return int(w) if w.isdigit() else _WORDS.get(w)


# ---------------------------------------------------------------- terms beyond the coupon and maturity
@dataclass(frozen=True)
class Workout:
    """A date the debt may be redeemed on, and at what price per 100."""

    kind: str  # 'maturity' | 'extended' | 'call' | 'prepay' | 'put'
    day: date
    price: float
    label: str
    counted: bool = True  # False: worked and shown, left out of YTW (a call the archive only presumes)


@dataclass
class Options:
    """What can move the redemption date: extensions, calls, prepayment (and what the sources say about it)."""

    extensions: int = 0
    extension_months: int = 0
    calls: list[Workout] = field(default_factory=list)
    puts: list[Workout] = field(default_factory=list)  # holder puts stated on a date (counted in YTW)
    prepay_at_par: bool = False
    notes: list[str] = field(default_factory=list)
    amortizing: bool = False
    floor_pct: float | None = None
    reported: str | None = None  # a coupon the sources state at an earlier date: '6.14% at 30 Jun'
    rows: dict[str, Row] = field(default_factory=dict)  # input key -> the fact row it was read from


def _texts(inst: Instrument, fields: Sequence[str] = ("coupon", "maturity_text", "research_terms")
           ) -> list[tuple[str, Row, str]]:
    """(field, fact row, text) of the term texts the options are read from."""
    out = []
    for fld in fields:
        row = inst.facts.get(fld)
        if row is None:
            continue
        value = row["value"]
        text = value.get("terms") if isinstance(value, dict) else value
        if isinstance(text, str) and text:
            out.append((fld, row, text))
    return out


def _puts(inst: Instrument, opts: Options) -> None:
    """Holder puts stated on a date (coupon, maturity or research terms, covenants): 'Holders may require the
    company to repurchase the notes at par on 1 Jun 2029'. A put on a fundamental change (a takeover, a
    delisting) has no date: noted, not worked."""
    contingent = False
    for _fld, row, text in _texts(inst, ("coupon", "maturity_text", "research_terms", "covenants")):
        for sentence in _SENTENCE.split(text):
            if not _PUT.search(sentence):
                continue
            if _CONTINGENT.search(sentence):
                contingent = True
                continue
            pm = _PUT_PRICE.search(sentence)
            price = 100.0 if pm is None or pm.group(1).lower() == "par" else float(pm.group(1))
            for dm in _PUT_DATE.finditer(sentence):
                day = _parse_day(dm.group(1)) or _parse_month(dm.group(1))
                if day is None or any(p.day == day for p in opts.puts):
                    continue
                what = "par" if price == 100.0 else f"{price:g}"
                opts.puts.append(Workout("put", day, price, f"Put by holders at {what} on {when(day)}"))
                opts.rows.setdefault("put", row)
    if contingent:
        opts.notes.append("Holders may require repurchase at par on a fundamental change (a takeover or delisting). "
                          "That right has no date, so it is not worked.")


def options(inst: Instrument) -> Options:
    """Extensions, calls, puts and prepayment from the instrument's terms (coupon text, maturity text, research
    terms; puts also from the covenants)."""
    opts = Options(prepay_at_par=inst.kind in LOAN_KINDS)
    for fld, row, text in _texts(inst):
        m = _EXTENSION.search(text)
        if m and not opts.extensions:
            n, length = _count(m.group(1)), _count(m.group(2))
            if n and length:
                opts.extensions = n
                opts.extension_months = length * (12 if m.group(3).lower() in ("yr", "year") else 1)
                opts.rows["extensions"] = row
        for c in _CALL.finditer(text):
            day = _parse_day(c.group(2))
            if day is None:
                continue
            price = 100.0 if c.group(1).lower() == "par" else float(c.group(1))
            what = "par" if price == 100.0 else f"{price:g}"
            opts.calls.append(Workout("call", day, price, f"Called at {what} from {when(day)}"))
            opts.rows.setdefault("call", row)
        for c in _PRESUMED.finditer(text):
            month = _parse_month(c.group(1))
            if month is None:
                continue
            price = float(c.group(2))
            opts.calls.append(Workout("call", month, price, f"Presumed first call, {c.group(1)} at {price:g}",
                                      counted=False))
            opts.notes.append(f"The research archive presumes a first call about {c.group(1)} at {price:g}. That is "
                              "the archive's assumption, not a term from the indenture, so it is shown but not "
                              "counted in yield to worst.")
            opts.rows.setdefault("presumedCall", row)
        if (assumed := _ASSUMED.search(text)) and not _PRESUMED.search(text):
            opts.notes.append(f"The research archive assumes a first call at {float(assumed.group(1)):g} with no date; "
                              "with no date it cannot be worked, so it is not counted in yield to worst.")
        if (mw := _MAKE_WHOLE.search(text)) is not None:
            day = _parse_day(mw.group(1))
            opts.notes.append("A make-whole call applies before " + (when(day) if day else mw.group(1)) + ". Its price "
                              "moves with Treasury yields, so it never sets yield to worst; the call price after "
                              "that date is not in the sources.")
        if _NO_CALL.search(text) and not opts.calls:
            opts.notes.append("The call terms are not in the sources, so yield to worst equals yield to maturity.")
        if fld == "coupon" and (fx := _FIXING.search(text)) is not None:
            day_month = fx.group(2).split(" ")
            on = f"{day_month[1]} {day_month[0]}" + (f", {day_month[2]}" if len(day_month) > 2 else "")
            opts.reported = f"{float(fx.group(1)):g}% on {on}"
        if opts.floor_pct is None and (fl := _FLOOR.search(text)) is not None:
            opts.floor_pct = float(fl.group(1))
            opts.rows["floor"] = row
        if fld in ("research_terms", "maturity_text") and _AMORTIZES.search(text) and not _NOT_DISCLOSED.search(text):
            opts.amortizing = True
    if inst.facts.get("amortization") is not None:
        opts.amortizing = True
    floor = inst.facts.get("sofr_floor_pct")
    if floor is not None and isinstance(floor["value"], int | float) and not isinstance(floor["value"], bool):
        opts.floor_pct = float(floor["value"])  # a stated floor field wins over the text
        opts.rows["floor"] = floor
    _puts(inst, opts)
    return opts


# ---------------------------------------------------------------- the yield of one price
def snap(terms: Terms, start: date, settle: date) -> date | None:
    """The first coupon date on or after `start` (and after the settle day): a call works on an interest date."""
    dates = schedule(terms, settle)
    i = bisect.bisect_left(dates, max(start, settle + timedelta(days=1)))
    return dates[i] if i < len(dates) else None


def workouts(terms: Terms, opts: Options, settle: date) -> list[Workout]:
    """Every redemption date worked: stated maturity, each extension, the calls, the holder puts, prepayment at
    par."""
    out = [Workout("maturity", terms.maturity, 100.0, f"Stated maturity, {when(terms.maturity)}")]
    for k in range(1, opts.extensions + 1):
        day = terms.maturity + relativedelta(months=opts.extension_months * k)
        name = "Fully extended" if k == opts.extensions else ("Extended once" if k == 1 else f"Extended {k} times")
        out.append(Workout("extended", day, 100.0, f"{name}, {when(day)}"))
    last = max(w.day for w in out)
    for c in opts.calls:
        day = snap(terms, c.day, settle)
        if day is not None and day < last:
            out.append(replace(c, day=day))
    for p in opts.puts:
        day = snap(terms, p.day, settle)
        if day is not None and day < last:
            out.append(replace(p, day=day))
    if opts.prepay_at_par:
        for day in schedule(replace(terms, maturity=last), settle)[:-1]:
            out.append(Workout("prepay", day, 100.0, f"Prepaid at par, {when(day)}"))
    return [w for w in out if w.day > settle]


def current_rate(terms: Terms, sofr_pct: float, floor_pct: float | None) -> float:
    """The floating coupon (decimal) held flat: the SOFR fixing, floored when a floor is stated, plus the margin."""
    index = max(sofr_pct, floor_pct) if floor_pct is not None else sofr_pct
    return index / 100.0 + (terms.margin_bp or 0.0) / 1e4


def cash_flows(terms: Terms, settle: date, sofr_pct: float | None, floor_pct: float | None = None,
               redemption: float = 100.0) -> Flows:
    """Cash flows per 100 par after `settle`, redeemed at `redemption` on `terms.maturity`: a fixed coupon as in
    price_bonds (30/360 accrual), a floating one at the current coupon held flat (Act/360)."""
    dates = schedule(terms, settle)
    if not dates:
        raise ValueError(f"{terms.instrument_id}: matured on {terms.maturity}")
    prev = previous_coupon(terms, settle)
    times = np.array([year_frac(settle, d) for d in dates])
    if terms.floating:
        if sofr_pct is None or terms.margin_bp is None:
            raise ValueError(f"{terms.instrument_id}: floating coupon needs SOFR and the margin")
        rate = current_rate(terms, sofr_pct, floor_pct)
        starts = [prev, *dates[:-1]]
        amounts = np.array([100.0 * rate * (b - a).days / 360.0 for a, b in zip(starts, dates, strict=True)])
        accrued = 100.0 * rate * (settle - prev).days / 360.0
    else:
        if terms.coupon_pct is None:
            raise ValueError(f"{terms.instrument_id}: fixed coupon missing")
        amounts = np.full(len(dates), terms.coupon_pct / terms.frequency)
        accrued = terms.coupon_pct / terms.frequency * days_30_360(prev, settle) / (days_30_360(prev, dates[0]) or 1)
    amounts[-1] += redemption
    return Flows(times, amounts, accrued, float(times[-1]))


def discount_margin(terms: Terms, settle: date, sofr_pct: float, floor_pct: float | None, f: Flows,
                    clean: float) -> float | None:
    """The spread over SOFR (decimal, SOFR held at the current fixing, floored) that discounts the flows to the
    dirty price, each period discounted at (SOFR + DM) x Act/360 (the first from the settle day)."""
    dates = schedule(terms, settle)
    starts = [settle, *dates[:-1]]
    tau = np.array([(b - a).days / 360.0 for a, b in zip(starts, dates, strict=True)])
    index = current_rate(terms, sofr_pct, floor_pct) - (terms.margin_bp or 0.0) / 1e4
    target = clean + f.accrued

    def gap(dm: float) -> float:
        return float(np.sum(f.amounts * np.cumprod(1.0 / (1.0 + (index + dm) * tau)))) - target

    try:
        return float(brentq(gap, -0.5, 2.0, xtol=1e-10))
    except ValueError:
        return None


@dataclass(frozen=True)
class Worked:
    workout: Workout
    yield_pct: float | None
    dm: float | None  # decimal; floating only


def work(terms: Terms, w: Workout, settle: date, sofr_pct: float | None, floor_pct: float | None,
         clean: float) -> Worked:
    """The yield (and, floating, the discount margin) of `clean` if the debt is redeemed at `w`."""
    t = replace(terms, maturity=w.day)
    f = cash_flows(t, settle, sofr_pct, floor_pct, w.price)
    y = yield_to_maturity(f, clean)
    dm = discount_margin(t, settle, sofr_pct, floor_pct, f, clean) if t.floating and sofr_pct is not None else None
    return Worked(w, y, dm)


@dataclass(frozen=True)
class Solved:
    ytm: Worked
    ytw: Worked | None
    worked: list[Worked]
    coupon: float | None  # the floating coupon held flat (decimal)


def solve(terms: Terms, opts: Options, settle: date, sofr_pct: float | None, clean: float) -> Solved:
    cands = workouts(terms, opts, settle)
    worked = [work(terms, w, settle, sofr_pct, opts.floor_pct, clean) for w in cands]
    ytm = next(x for x in worked if x.workout.kind == "maturity")
    counted = [x for x in worked if x.workout.counted and x.yield_pct is not None]
    ytw = min(counted, key=lambda x: (x.yield_pct, x.workout.day)) if ytm.yield_pct is not None and counted else None
    coupon = current_rate(terms, sofr_pct, opts.floor_pct) if terms.floating and sofr_pct is not None else None
    return Solved(ytm, ytw, worked, coupon)


# ---------------------------------------------------------------- one instrument
def _why_not(inst: Instrument) -> str | None:
    """Plain reason an instrument is not worked at all (before any price)."""
    if inst.kind in SECURITIZED:
        return ("No yield: securitized notes repay principal early on a schedule the sources do not give, so a "
                "yield to the legal maturity would mislead.")
    if not inst.usd:
        return "No yield: non-USD note (no foreign-currency discount curve in v0.1)."
    if inst.terms is None:
        gaps = sorted({_GAP_WORDS.get(m.split(": ", 1)[-1], m.split(": ", 1)[-1]) for m in inst.terms_missing})
        gaps = gaps or ["coupon and maturity"]
        return f"No yield: the {' and '.join(gaps)} {'is' if len(gaps) == 1 else 'are'} not in the sources."
    if inst.terms.maturity.year >= PERPETUAL_YEAR:
        return (f"No yield: a perpetual note (maturity recorded as {inst.terms.maturity.isoformat()}); it has no "
                "maturity to work a yield to, and its call schedule is not in the sources.")
    return None


def _round(v: float | None, n: int) -> float | None:
    return None if v is None else round(v, n)


def _workout_doc(x: Worked) -> dict[str, Any]:
    return {"kind": x.workout.kind, "date": x.workout.day.isoformat(), "price": round(x.workout.price, 4),
            "label": x.workout.label, "yieldPct": _round(x.yield_pct, 3),
            "dmBp": None if x.dm is None else round(x.dm * 1e4, 1), "counted": x.workout.counted}


def _shown(s: Solved) -> list[dict[str, Any]]:
    """The dates worked, in date order: every maturity, extension and call; of the prepayment dates only the
    lowest-yielding one (the others only raise the yield)."""
    keep = [x for x in s.worked if x.workout.kind != "prepay"]
    prepay = [x for x in s.worked if x.workout.kind == "prepay" and x.yield_pct is not None]
    if prepay:
        keep.append(min(prepay, key=lambda x: (x.yield_pct, x.workout.day)))
    return [_workout_doc(x) for x in sorted(keep, key=lambda x: (x.workout.day, x.workout.kind))]


def _sofr_day(book: CurveBook, day: date) -> date | None:
    days = sorted(d for d in book.sofr if d <= day)
    return days[-1] if days and (day - days[-1]).days <= 7 else None


def _fact_input(key: str, label: str, row: Row, value: Any = None) -> dict[str, Any]:
    raw = row["value"]
    shown = value if value is not None else (raw.get("terms") if isinstance(raw, dict) else raw)
    out = {"key": key, "label": label, "value": shown, "tag": str(row["tag"]).strip(),
           "asOf": row["valid_from"].isoformat(), "sourceName": row.get("source_name") or "n/d"}
    if row.get("source_url"):
        out["sourceUrl"] = row["source_url"]
    if row.get("method_version"):
        out["methodVersion"] = row["method_version"]
    return out


@dataclass(frozen=True)
class PricePoint:
    day: date
    price: float
    kind: str
    tag: str
    source_name: str
    source_url: str | None
    method_version: str | None
    reported_yield: float | None = None

    def doc(self) -> dict[str, Any]:
        out: dict[str, Any] = {"price": round(self.price, 4), "date": self.day.isoformat(), "kind": self.kind,
                               "tag": self.tag, "sourceName": self.source_name}
        if self.source_url:
            out["sourceUrl"] = self.source_url
        if self.method_version:
            out["methodVersion"] = self.method_version
        return out


def _price_words(basis: PricePoint | None) -> str:
    """'trade price', 'quoted price', 'Marden price': what a yield is worked on."""
    if basis is None or basis.kind == "marden":
        return "Marden price"
    return "trade price" if basis.kind == "trade" else "quoted price"


def convert_note(basis: PricePoint | None) -> str:
    """The YTW tile's note on a convertible: 'Yield on the trade price, ignoring the conversion option.'"""
    return f"Yield on the {_price_words(basis)}, ignoring the conversion option."


def assumptions(inst: Instrument, opts: Options, s: Solved | None, price: float | None,
                sofr: tuple[date, float] | None = None, basis: PricePoint | None = None) -> list[str]:
    """The method's assumptions for this instrument, in plain words (the hover on the Yield tile)."""
    t = inst.terms
    assert t is not None
    out: list[str] = []
    if inst.kind == "convert":
        out.append(f"A convertible note, worked as a straight bond: the yield on the {_price_words(basis)}, ignoring "
                   "the conversion option. The flows are the coupons and par at maturity, as if the note is never "
                   "converted; the share price and the conversion terms are not modelled, so when the option is "
                   "worth a lot the yield is low or negative.")
    if basis is not None and basis.kind != "marden":
        out.append(f"No Marden price: worked from the latest market price, {basis.source_name} {basis.price:.2f} on "
                   f"{when(basis.day)}, settling that day.")
    if t.floating:
        margin = f"SOFR + {(t.margin_bp or 0.0) / 100:.2f}%"
        if s is not None and s.coupon is not None and sofr is not None:
            floored = opts.floor_pct is not None and opts.floor_pct > sofr[1]
            index = f"the {opts.floor_pct:g}% SOFR floor" if floored else f"SOFR {sofr[1]:.2f}% on {when(sofr[0])}"
            out.append(f"Cash flows at the current coupon, {s.coupon * 100:.2f}% ({index} plus "
                       f"{(t.margin_bp or 0.0) / 100:.2f}%), held for every quarter to the redemption date "
                       "(Act/360). Coupon terms: " + margin + ".")
        else:
            out.append(f"Cash flows at the current coupon ({margin}) held flat, paid quarterly (Act/360).")
        if opts.reported:
            out.append(f"The sources state the coupon was {opts.reported}; the yield uses the latest SOFR fixing "
                       "instead.")
        out.append("Yield: the internal rate of those payments, compounded twice a year so it compares with "
                   "fixed-rate yields. Discount margin: the spread over SOFR, held at the current fixing, that "
                   "discounts the payments to the price.")
    else:
        out.append(f"Fixed {t.coupon_pct:g}% coupon paid twice a year, accrued 30/360; yield compounded twice a "
                   "year (bond-equivalent).")
    out.append("Settles on the price date; the price is clean and the accrued interest is added.")
    if opts.extensions:
        ext = f"{opts.extension_months // 12}-year" if opts.extension_months % 12 == 0 else \
            f"{opts.extension_months}-month"
        count = {1: "One", 2: "Two", 3: "Three", 4: "Four"}.get(opts.extensions, str(opts.extensions))
        out.append(f"{count} {ext} extension option{'s' if opts.extensions > 1 else ''} (the borrower's "
                   f"choice): worked to the stated maturity, {when(t.maturity)}, and to each extended maturity at "
                   "the same margin. Extension fees are not in the sources and are left out.")
    elif t.floating:
        out.append("No extension options in the sources: worked to the stated maturity.")
    if opts.prepay_at_par:
        tail = ("Below par, prepaying early usually raises the yield; every interest date is worked and the "
                "lowest yield counts." if price is not None and price < 100 else
                "Above par, the earliest prepayment usually gives the lowest yield; every interest date is worked.")
        out.append("A bank loan is assumed prepayable at par on any interest date (the credit agreement's "
                   f"prepayment terms are not public). {tail}")
    calls = [c for c in opts.calls if c.counted]
    if calls:
        out.append("Call terms from the research terms: " + "; ".join(c.label[:1].lower() + c.label[1:] for c in calls)
                   + ". Worked to the first call date and to maturity.")
    if opts.puts:
        out.append("Holder put from the terms: " + "; ".join(p.label[:1].lower() + p.label[1:] for p in opts.puts)
                   + ". Worked to the put date and to maturity; yield to worst is the lower of the yields worked.")
    out.extend(opts.notes)
    if not t.floating and not calls and not any("call" in n for n in opts.notes) and not opts.prepay_at_par:
        out.append("No call schedule in the sources, so " + (
            "yield to worst is the lower of the yields to the put and to maturity." if opts.puts else
            "yield to worst equals yield to maturity."))
    if opts.floor_pct is not None and sofr is not None and opts.floor_pct <= sofr[1]:
        out.append(f"The {opts.floor_pct:g}% SOFR floor is below the current fixing, so it has no effect.")
    if opts.amortizing:
        out.append("The research terms describe scheduled amortization; like the price model, the yield assumes "
                   "all principal is repaid at the redemption date.")
    else:
        out.append("Principal repaid in full at the redemption date (no amortization in the sources).")
    out.extend(f"Terms: {a}." for a in t.assumptions)
    return out


def yield_value(value: float | None, basis: PricePoint) -> dict[str, Any]:
    """Sourced<T> of a yield worked from `basis`: tag M (this model) on a Marden price; on a market price the
    price's own tag (F for a TRACE trade, R for a quote) and source, dated the observation, with the method that
    worked it."""
    if basis.kind == "marden":
        return {**m_value(value, basis.day, METHOD, source_name=LABEL), "sourceUrl": METHOD_URL}
    return {"value": value, "tag": basis.tag or "R", "sourceName": basis.source_name, "asOf": basis.day.isoformat(),
            "methodVersion": METHOD, "sourceUrl": basis.source_url or METHOD_URL}


def instrument_output(inst: Instrument, book: CurveBook, day: date, series: list[PricePoint],
                      observed: PricePoint | None) -> dict[str, Any]:
    """The 'yield' output of one instrument as of `day`: worked from the latest Marden price, else from the
    latest market price (`observed`)."""
    t = inst.terms
    out: dict[str, Any] = {
        "label": LABEL, "methodVersion": METHOD, "sourceUrl": METHOD_URL,
        "couponType": t.coupon_type if t else None,
        "ytm": m_value(None, day, METHOD, source_name=LABEL), "ytw": m_value(None, day, METHOD, source_name=LABEL),
        "dmBp": None, "ytmTo": None, "ytwTo": None, "basis": None, "workouts": [], "points": [], "observed": None,
        "assumptions": [], "inputs": [], "why": None, "missing": [], "note": None,
    }
    for k in ("ytm", "ytw"):
        out[k]["sourceUrl"] = METHOD_URL
    why = _why_not(inst)
    if why is not None:
        out["why"] = why
        out["missing"] = list(inst.terms_missing) or [why]
        return out
    assert t is not None
    opts = options(inst)

    def at(p: PricePoint) -> tuple[Solved | None, str | None]:
        if t.maturity <= p.day:
            return None, f"No yield: matured on {when(t.maturity)}."
        sofr = book.sofr_on(p.day)
        if t.floating and sofr is None:
            return None, f"No yield on {when(p.day)}: no SOFR fixing within a week of the price date."
        try:
            return solve(t, opts, p.day, sofr, p.price), None
        except (ValueError, ZeroDivisionError) as exc:
            return None, f"No yield on {when(p.day)}: {exc}."

    for p in series:
        s, miss = at(p)
        if s is None:
            out["missing"].append(miss)
            out["points"].append({"date": p.day.isoformat(), "price": round(p.price, 4), "ytm": None, "ytw": None,
                                  "ytwTo": None, "dmBp": None})
            continue
        out["points"].append({
            "date": p.day.isoformat(), "price": round(p.price, 4), "ytm": _round(s.ytm.yield_pct, 3),
            "ytw": _round(s.ytw.yield_pct if s.ytw else None, 3),
            "ytwTo": s.ytw.workout.day.isoformat() if s.ytw else None,
            "dmBp": None if not s.ytw or s.ytw.dm is None else round(s.ytw.dm * 1e4, 1),
        })
    obs_solved = None
    if observed is not None:
        obs_solved, miss = at(observed)
        out["observed"] = {**observed.doc(), "ytm": _round(obs_solved.ytm.yield_pct, 3) if obs_solved else None,
                           "ytw": _round(obs_solved.ytw.yield_pct, 3) if obs_solved and obs_solved.ytw else None,
                           "reportedYieldPct": _round(observed.reported_yield, 3)}
        if miss:
            out["missing"].append(miss)
    basis = series[-1] if series else observed
    if basis is None:
        out["why"] = "No yield: no Marden or observed price yet."
        out["missing"].append(f"instrument {inst.id}: no Marden or observed price on or before {day}")
        out["assumptions"] = assumptions(inst, opts, None, None)
        return out
    s = at(basis)[0] if basis is not observed else obs_solved
    out["basis"] = basis.doc()
    if inst.kind == "convert":
        out["note"] = convert_note(basis)
    sday = _sofr_day(book, basis.day)
    out["assumptions"] = assumptions(inst, opts, s, basis.price, (sday, book.sofr[sday]) if sday else None, basis)
    out["inputs"] = _inputs(inst, opts, book, basis)
    if s is None or s.ytm.yield_pct is None or s.ytw is None:
        out["why"] = (out["missing"][-1] if out["missing"] else
                      f"No yield: the price {basis.price:.1f} gives no yield the solver can reach.")
        return out
    assert s.ytw.yield_pct is not None
    out["ytm"] = yield_value(round(s.ytm.yield_pct, 3), basis)
    out["ytw"] = yield_value(round(s.ytw.yield_pct, 3), basis)
    if t.floating and s.ytw.dm is not None:
        out["dmBp"] = yield_value(round(s.ytw.dm * 1e4, 1), basis)
    out["ytmTo"] = _workout_doc(s.ytm)
    out["ytwTo"] = _workout_doc(s.ytw)
    out["workouts"] = _shown(s)
    return out


def _inputs(inst: Instrument, opts: Options, book: CurveBook, basis: PricePoint) -> list[dict[str, Any]]:
    """The inputs of the headline yields, each sourced (rule 4: an M value names its method)."""
    t = inst.terms
    assert t is not None
    items: list[dict[str, Any]] = [{"key": "price", "label": "Price the yields are worked from",
                                    "value": round(basis.price, 4), "unit": "per 100", **{
                                        k: v for k, v in basis.doc().items() if k not in ("price", "date", "kind")},
                                    "asOf": basis.day.isoformat()}]
    for key, label in (("coupon", "Coupon"), ("maturity", "Maturity")):
        row = inst.facts.get(key)
        if row is not None:
            items.append(_fact_input(key, label, row))
    if inst.comp is not None and any("issue comp" in a for a in t.assumptions):
        items.append({"key": "issueComp", "label": "Issue terms (comp)", "value": inst.comp.get("rate") or None,
                      "tag": str(inst.comp.get("issue_spread_tag") or "R").strip() or "R",
                      "asOf": inst.comp["issue_date"].isoformat(),
                      "sourceName": inst.comp.get("source_name") or "n/d",
                      **({"sourceUrl": inst.comp["source_url"]} if inst.comp.get("source_url") else {})})
    for key, label in (("extensions", "Extension options"), ("call", "Call terms"), ("presumedCall", "Presumed call"),
                       ("put", "Holder put"), ("floor", "SOFR floor")):
        row = opts.rows.get(key)
        if row is not None:
            items.append(_fact_input(key, label, row))
    if t.floating:
        sday = _sofr_day(book, basis.day)
        if sday is not None:
            items.append({"key": "sofrPct", "label": "SOFR", "value": book.sofr[sday], "unit": "%", "tag": "F",
                          "asOf": sday.isoformat(), "sourceName": "Federal Reserve Bank of New York"})
    return items


# ---------------------------------------------------------------- inputs and the run
def _price_rows(conn: Conn, ids: Sequence[str], day: date, kinds: Sequence[str]) -> list[Row]:
    params: dict[str, Any] = {"ids": list(ids), "day": day, "kinds": list(kinds)}
    sql = """SELECT instrument_id, obs_date, kind, price, yield_pct, tag, source_name, source_url, method_version
               FROM price_obs WHERE instrument_id = ANY(%(ids)s) AND kind = ANY(%(kinds)s) AND obs_date <= %(day)s
                AND price IS NOT NULL"""
    if not show_unlicensed_prices():
        sql += " AND source_name <> ALL(%(hidden)s)"
        params["hidden"] = unlicensed_source_names()
    return conn.execute(sql + " ORDER BY instrument_id, obs_date, obs_at NULLS FIRST, id", params).fetchall()


def _point(r: Row) -> PricePoint:
    y = r["yield_pct"]
    return PricePoint(r["obs_date"], float(r["price"]), r["kind"], str(r["tag"]).strip(), r["source_name"],
                      r["source_url"], r["method_version"], None if y is None else float(y))


def load_prices(conn: Conn, ids: Sequence[str], day: date) -> tuple[dict[str, list[PricePoint]], dict[str, PricePoint]]:
    """The Marden daily price series (one per day, the last row) and the latest market price (TRACE trade,
    BondTerminal or Börse Frankfurt quote, dealer quote; the latest day, on it a trade over a quote, then the
    latest row), by instrument."""
    series: dict[str, dict[date, PricePoint]] = {}
    for r in _price_rows(conn, ids, day, ("marden",)):
        series.setdefault(r["instrument_id"], {})[r["obs_date"]] = _point(r)
    best: dict[str, tuple[tuple[date, int, int], Row]] = {}
    for n, r in enumerate(_price_rows(conn, ids, day, OBSERVED_KINDS)):
        key = (r["obs_date"], MARKET_RANK.get(r["kind"], 0), n)
        if r["instrument_id"] not in best or key > best[r["instrument_id"]][0]:
            best[r["instrument_id"]] = (key, r)
    observed = {iid: _point(r) for iid, (_, r) in best.items()}
    return {k: [v[d] for d in sorted(v)] for k, v in series.items()}, observed


def _extra_facts(conn: Conn, insts: dict[str, Instrument]) -> None:
    """Research terms, maturity text, amortization and SOFR floor (static terms, read like price_terms)."""
    if not insts:
        return
    with pit.unaudited():
        rows = pit.fact_rows(conn, business_today(), "instrument", list(insts), EXTRA_FIELDS)
    grouped: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        grouped.setdefault((r["subject_id"], r["field"]), []).append(r)
    for (iid, fld), rs in grouped.items():
        best = pit.current_of(rs)
        if best is not None:
            insts[iid].facts.setdefault(fld, best)


def universe(conn: Conn, day: date) -> list[str]:
    """V1 instruments and every instrument with a Marden or observed price on or before `day`."""
    rows = conn.execute("""SELECT id FROM instrument WHERE in_v1 UNION
                           SELECT DISTINCT instrument_id FROM price_obs
                            WHERE obs_date <= %s AND kind = ANY(%s) AND price IS NOT NULL ORDER BY 1""",
                        (day, ["marden", *OBSERVED_KINDS])).fetchall()
    return [r["id"] for r in rows]


def load_terms(conn: Conn, ids: Sequence[str]) -> dict[str, Instrument]:
    """Terms exactly as the price engine builds them: its universe is linked to the issue comps together (a comp
    goes to one instrument), the instruments outside it on their own."""
    priced = targets(conn)
    comps = load_comps(conn, business_today())
    insts = load_instruments(conn, priced)
    others = load_instruments(conn, [i for i in ids if i not in insts])
    with pit.unaudited():  # issue comps are dated their issue day; note_terms records the ones used
        attach(insts, comps)
        attach(others, comps)
    found = {**insts, **others}
    out = {i: found[i] for i in ids if i in found}
    _extra_facts(conn, out)
    return out


def run_day(conn: Conn, day: date, ids: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    """Every output of one day (no writes)."""
    wanted = list(ids) if ids else universe(conn, day)
    insts = load_terms(conn, wanted)
    book = load_curves(conn, day, since=HISTORY_START)
    series, observed = load_prices(conn, list(insts), day)
    outs: dict[str, dict[str, Any]] = {}
    for iid, inst in sorted(insts.items()):
        outs[iid] = instrument_output(inst, book, day, series.get(iid, []), observed.get(iid))
        if outs[iid]["basis"] is not None:  # the static terms behind a yield (input audit)
            seen = series.get(iid) or ([observed[iid]] if iid in observed else [])
            note_terms(inst, day, min((p.day for p in seen), default=None))
    return outs


def run(as_of: date | None = None, instruments: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    """Yields of every V1 or priced instrument (or `instruments`) as of `as_of` (default today, US Eastern)."""

    def compute(conn: Conn, day: date) -> Batch:
        batch = Batch()
        outs = run_day(conn, day, instruments)
        for iid, out in outs.items():
            batch.add("instrument", iid, out)
        batch.inputs = [[iid, (o["basis"] or {}).get("price"), (o["basis"] or {}).get("date"), o["ytw"]["value"]]
                        for iid, o in sorted(outs.items())]
        return batch

    return run_outputs(MODEL, VERSION, as_of, compute, trigger=trigger, mark=mark)


def run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    result = run(instruments=None if "*" in keys else keys, trigger=trigger)
    return {"instruments": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


# ---------------------------------------------------------------- triggers and schedule
def _rate_series(conn: Conn, subject_id: str) -> list[str]:
    return ["*"] if subject_id.startswith(("ust_par_", "sofr", "issue_comp:")) else []


def _install_triggers() -> None:
    triggers.model_depends(MODEL, "instrument", "@model:price")
    triggers.model_depends(MODEL, "instrument", "@price")  # Marden daily prices and observed prices
    for fld in ("coupon", "coupon_type", "coupon_rate_pct", "coupon_spread_bp", "maturity", *EXTRA_FIELDS):
        triggers.model_depends(MODEL, "instrument", fld)
    for fld in ("value", "deal_terms"):
        triggers.model_depends(MODEL, "series", fld, _rate_series)
    triggers.register_runner(MODEL, run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="yield_daily")
@app.task(name="models.yield", queue="models", queueing_lock="models.yield", lock="models.yield")
def yield_task(timestamp: int | None = None) -> dict[str, Any]:
    result = run(trigger="schedule")
    return {"instruments": len(result["outputs"]), "changed": len(result["changed"]), "run_id": result["run_id"]}


# ---------------------------------------------------------------- CLI
def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern); a past day "
                   "works the prices and curves public that day")
    p.add_argument("--instrument", action="append", dest="instruments", help="instrument id (repeatable)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true", help="print every output as JSON")


def _pct(v: Any) -> str:
    return "n/d" if v is None else f"{v:.2f}%"


@cli.action("model", MODEL, help="Yield to maturity, yield to worst and discount margin from each price",
            configure=_args)
def run_cli(args: argparse.Namespace) -> int:
    result = run(args.as_of, args.instruments, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(result["outputs"], indent=2, ensure_ascii=False))
        return 0
    outs = result["outputs"]
    worked = sum(1 for o in outs.values() if o["ytw"]["value"] is not None)
    print(f"yield {VERSION} as of {result['as_of']} (run {result['run_id']}): {len(outs)} instruments, {worked} with "
          f"yields, changed {len(result['changed'])}")
    for iid, o in outs.items():
        b, to = o["basis"] or {}, o["ytwTo"] or {}
        dm = f"DM {o['dmBp']['value']:.0f}bp" if o["dmBp"] else ""
        line = (f"  {iid:36} {b.get('kind', '-'):8} {b.get('price', 0) or 0:8.3f} YTM {_pct(o['ytm']['value']):>8} "
                f"YTW {_pct(o['ytw']['value']):>8} to {to.get('date', '-'):10} {dm:10} pts {len(o['points'])}")
        print(line if o["why"] is None else f"  {iid:36} {o['why']}")
    return 0
