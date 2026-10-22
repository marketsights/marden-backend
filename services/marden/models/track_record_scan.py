"""The backtest's signal side: what the status, price and cushion models said on each scan day, and the
first day each case's signal fired.

A scan day's state comes from the model's stored output when that output was written on the day itself
(a live run: `created_at`, US Eastern, equals `as_of`), which is a snapshot taken at the time (spec 12).
Otherwise it is reconstructed point in time by running the model code on the day with only what was
public then (marden.models.pit): the price engine's `run_day`, the status rules on `status_inputs.load`
with that day's Marden price outputs put in place of stored outputs (with their rate-neutral reading for
L2, `status_inputs.prices_from_outputs`), and the cushion simulation. Nothing is
written for the scan days: a reconstruction is not a snapshot, so it never enters the price history or
the status history.

Days run in parallel processes (spawned, one database pool each): the price engine takes 10-20 s a day.

Signals (track_record_cases.ASSUMPTIONS), each against the subject's own state on the first scan day:
- status: a case site's chain status is worse (Holding / No build < Strained < Broken);
- price: a case instrument's Marden price is below 95 when it was 95 or more (or not yet priced);
- cushion: a case site's cushion band moves into 0-3 or below zero from a better band (00-global alerts:
  "cushion under 3 months or under zero").

A diagnostic added after the first run (not a locked signal): the same scan without anything derived from
the Marden price (`price_free`): the chain status without its L1 / L2 price findings, and the cushion. For a
traded bond the Marden price follows its own prints, so a price-derived "lead" is partly the bond falling
through 98 before it falls through 95; the price-free flag shows what the other rules saw.
"""

from __future__ import annotations

import logging
import multiprocessing
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from marden.core import db
from marden.core.db import Conn
from marden.core.jsonutil import canonical_json, hash_parts
from marden.models.status_labels import chain_label
from marden.models.status_rules import PRICE_FINDINGS
from marden.models.track_record_cases import PRICE_TRIGGER, WINDOW_START

log = logging.getLogger(__name__)

STATUS_RANK = {"Holding": 0, "No build": 0, "Strained": 1, "Broken": 2}
BAND_RANK = {"12+": 0, "6-12": 1, "3-6": 2, "0-3": 3, "<0": 4}
ALERT_BANDS = ("0-3", "<0")
State = dict[str, Any]


def band_of(months: int | None) -> str | None:
    from marden.readmodels.views.sites import band_of as completion_band  # the Completion screen's bands

    return None if months is None else completion_band(months)


def scan_days(as_of: date, step: int = 7) -> list[date]:
    days, day = [], WINDOW_START
    while day < as_of:
        days.append(day)
        day += timedelta(days=step)
    return [*days, as_of]


def live_days(conn: Conn, model: str, start: date, end: date) -> set[date]:
    """Days with a live run of `model`: an output written on its own as-of day (US Eastern)."""
    rows = conn.execute(
        """SELECT DISTINCT as_of FROM model_output WHERE model = %s AND as_of BETWEEN %s AND %s
              AND (created_at AT TIME ZONE 'America/New_York')::date = as_of""",
        (model, start, end),
    ).fetchall()
    return {r["as_of"] for r in rows}


def _stored(conn: Conn, model: str, subject_type: str, ids: Sequence[str], day: date) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT DISTINCT ON (subject_id) subject_id, output FROM model_output
            WHERE model = %s AND subject_type = %s AND subject_id = ANY(%s) AND as_of = %s
              AND (created_at AT TIME ZONE 'America/New_York')::date = as_of
            ORDER BY subject_id, created_at DESC, id DESC""",
        (model, subject_type, list(ids), day),
    ).fetchall()
    return {r["subject_id"]: r["output"] for r in rows}


PRICE_LABELS = PRICE_FINDINGS  # status_rules' lenders findings on the Marden price
OTHER_LENDER_LABELS = ("Syndication stalled", "Default notice")


def _price_only(link: dict[str, Any]) -> bool:
    """A lenders link tripped only by the Marden price (no syndication or default finding)."""
    reason = link.get("reason") or ""
    return (link.get("role") == "lenders" and any(lbl in reason for lbl in PRICE_LABELS)
            and not any(lbl in reason for lbl in OTHER_LENDER_LABELS))


def _status_doc(output: dict[str, Any]) -> dict[str, Any]:
    """{status, statusExPrice (the chain without the Marden-price findings), codes}."""
    chain = output.get("chain") or []
    tripped = [link for link in chain if link.get("status") in ("amber", "red") and link.get("ruleCode")]
    codes = [f"{link['ruleCode']} {link['role']}" + (" (Marden price)" if _price_only(link) else "")
             for link in tripped]
    worst = max(({"red": 2, "amber": 1}[link["status"]] for link in tripped if not _price_only(link)), default=0)
    status = output.get("status")
    ex_price = status if status == "No build" else {2: "Broken", 1: "Strained"}.get(worst, "Holding")
    return {"status": status, "statusExPrice": ex_price, "codes": codes}


def _cushion_months(output: dict[str, Any] | None) -> int | None:
    value = (((output or {}).get("projection") or {}).get("cushionMonths") or {}).get("value")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _prices(conn: Conn, day: date, ids: list[str], live: bool,
            missing: list[str]) -> tuple[dict[str, Any], str, dict[str, Any], Any]:
    """(price per instrument, basis, the price outputs, the run's Treasury curves or None)."""
    from marden.models import price

    if live:
        stored = _stored(conn, "price", "instrument", ids, day)
        return {i: o.get("price") for i, o in stored.items()}, "snapshot", stored, None
    try:
        result = price.run_day(conn, day, ids)
    except ValueError as exc:  # e.g. no Treasury curve public yet
        missing.append(f"price {day.isoformat()}: {exc}")
        return {}, "missing", {}, None
    return {i: o.get("price") for i, o in result["prices"].items()}, "reconstructed", result["prices"], result["book"]


def _status(conn: Conn, day: date, slugs: list[str], outputs: dict[str, Any], book: Any,
            live: bool) -> tuple[dict, str]:
    from marden.models import status_inputs, status_rules

    if live:
        return {s: _status_doc(o) for s, o in _stored(conn, "status", "site", slugs, day).items()}, "snapshot"
    inputs = status_inputs.load(conn, day)  # every site: tenant ratings are pooled across sites
    pool = status_rules.rating_pool(inputs)
    priced = status_inputs.prices_from_outputs(conn, day, {i: (day, o, None) for i, o in outputs.items()}, book)
    out = {}
    for slug in slugs:
        site = inputs.sites.get(slug)
        if site is None:
            continue
        site.prices = [priced[i] for i in site.instruments if i in priced]
        out[slug] = _status_doc(status_rules.evaluate(site, inputs, pool))
    return out, "reconstructed"


def _cushion(conn: Conn, day: date, slugs: list[str], live: bool, missing: list[str]) -> tuple[dict, str]:
    from marden.models import cushion
    from marden.models.cushion_history import load_history
    from marden.models.cushion_inputs import load_sites

    if live:
        return {s: _cushion_months(o) for s, o in _stored(conn, "cushion", "site", slugs, day).items()}, "snapshot"
    history = load_history(conn, day)
    history_hash = hash_parts(canonical_json([r.doc() for r in history.records]))
    out: dict[str, int | None] = {}
    for site in load_sites(conn, day, slugs):
        try:
            output, _ = cushion.project_site(site, history, None, history_hash)
        except (ValueError, ZeroDivisionError) as exc:  # one site's bad dates never stop the scan
            missing.append(f"cushion {site.slug} {day.isoformat()}: {type(exc).__name__}: {exc}")
            continue
        out[site.slug] = _cushion_months(output)
    return out, "reconstructed"


def compute_day(args: tuple[str, list[str], list[str], list[str]]) -> State:
    """One scan day's state: {day, basis, price, status, cushion, missing} (a process-pool worker)."""
    day_iso, price_ids, status_slugs, cushion_slugs = args
    day = date.fromisoformat(day_iso)
    missing: list[str] = []
    with db.transaction() as conn:
        live = {m: day in live_days(conn, m, day, day) for m in ("price", "status", "cushion")}
        prices, price_basis, price_outputs, book = _prices(conn, day, price_ids, live["price"], missing)
        status, status_basis = _status(conn, day, status_slugs, price_outputs, book, live["status"])
        cush, cushion_basis = _cushion(conn, day, cushion_slugs, live["cushion"], missing)  # reads only
    return {"day": day_iso, "basis": {"price": price_basis, "status": status_basis, "cushion": cushion_basis},
            "price": {i: (round(float(v), 3) if isinstance(v, int | float) else None) for i, v in prices.items()},
            "status": status, "cushion": cush, "missing": missing}


def compute_days(days: Sequence[date], price_ids: list[str], status_slugs: list[str], cushion_slugs: list[str],
                 workers: int, memo: dict[tuple, State] | None = None) -> dict[date, State]:
    """Each day's state, in parallel processes. `memo` (optional, for several as-of runs in one sitting:
    a scan day's state does not depend on the run's as-of day) holds states already computed."""
    memo = {} if memo is None else memo
    key = (tuple(price_ids), tuple(status_slugs), tuple(cushion_slugs))
    args = [(d.isoformat(), price_ids, status_slugs, cushion_slugs) for d in days if (d, key) not in memo]
    if workers <= 1 or len(args) <= 1:
        states = [compute_day(a) for a in args]
    else:
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(workers, len(args)), mp_context=ctx) as pool:
            states = list(pool.map(compute_day, args))
    for s in states:
        memo[(date.fromisoformat(s["day"]), key)] = s
    return {d: memo[(d, key)] for d in days}


# ---------------------------------------------------------------- signals
@dataclass
class Fire:
    day: date
    signal: str  # 'status' | 'price' | 'cushion'
    subject: str
    detail: str
    basis: str  # 'snapshot' | 'reconstructed'

    def doc(self) -> dict[str, Any]:
        return {"date": self.day.isoformat(), "signal": self.signal, "subject": self.subject, "detail": self.detail,
                "basis": self.basis}


def fires(ids: Sequence[str], slugs: Sequence[str], base: State, state: State, *,
          price_free: bool = False) -> list[Fire]:
    """The case's signals that are on in `state` against the window-start state `base`. With `price_free`
    (a diagnostic): the chain status without its Marden-price findings, and the cushion; no price signal."""
    day, out = date.fromisoformat(state["day"]), []
    key = "statusExPrice" if price_free else "status"
    for slug in slugs:
        b, s = base["status"].get(slug), state["status"].get(slug)
        if not (b and s):
            continue
        before, after = b.get(key, b["status"]), s.get(key, s["status"])
        if STATUS_RANK.get(after, 0) > STATUS_RANK.get(before, 0):
            codes = [c for c in s["codes"] if not (price_free and c.endswith("(Marden price)"))]
            moved = f"{chain_label(before)} -> {chain_label(after)}"
            detail = f"{slug} site status {moved} ({', '.join(codes) or 'no rule code'})"
            out.append(Fire(day, "status", slug, detail, state["basis"]["status"]))
    for iid in ids if not price_free else []:
        b, p = base["price"].get(iid), state["price"].get(iid)
        if p is not None and p < PRICE_TRIGGER and (b is None or b >= PRICE_TRIGGER):
            start = "not priced" if b is None else f"{b:.1f}"
            out.append(Fire(day, "price", iid, f"Marden price {p:.1f} for {iid} (window start: {start})",
                            state["basis"]["price"]))
    for slug in slugs:
        b, c = band_of(base["cushion"].get(slug)), band_of(state["cushion"].get(slug))
        if c in ALERT_BANDS and (b is None or BAND_RANK[c] > BAND_RANK[b]):
            months = state["cushion"][slug]
            out.append(Fire(day, "cushion", slug, f"{slug} cushion {months} months (band {b or 'n/d'} -> {c})",
                            state["basis"]["cushion"]))
    return out


def first_fires(ids: Sequence[str], slugs: Sequence[str], states: dict[date, State], *,
                price_free: bool = False) -> dict[str, Fire]:
    """The first fire of each signal over the ordered states (the first state is the baseline)."""
    days = sorted(states)
    if not days:
        return {}
    base, found = states[days[0]], {}
    for day in days[1:]:
        for f in fires(ids, slugs, base, states[day], price_free=price_free):
            found.setdefault(f.signal, f)
    return found


def refine_days(states: dict[date, State], first: Fire | None) -> list[date]:
    """The days between the scan day before `first` and `first` (exclusive), to date it to the day."""
    if first is None:
        return []
    before = [d for d in sorted(states) if d < first.day]
    if not before:
        return []
    return [before[-1] + timedelta(days=n) for n in range(1, (first.day - before[-1]).days)]
