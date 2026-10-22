"""Tier curves' HY input (pricing-v0.1 "Inputs when FRED is unavailable"): the ICE BofA HY OAS wins whenever it
has a value; on a day it has none the USHY fund OAS stands in, recorded in the inputs as a proxy; without HY
history at the deals' issue dates the market term runs at the methodology's 33bp per point against its stated
2.89% sample mean."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from price_rows import copy_price_inputs

from marden.core.facts import FactWriter
from marden.models import price_tiers as T
from marden.models.price_curve import load_curves
from marden.models.price_terms import load_comps

DAY = date(2026, 10, 21)
ICE_NAME = "ICE Data Indices, LLC via FRED"
PROXY_NAME = "iShares USHY fund OAS (BlackRock)"
PROXY_URL = "https://www.ishares.com/us/products/291299/ishares-broad-usd-high-yield-corporate-bond-etf"


def _deals() -> list[T.Deal]:
    spec = [("IG project bond", 2.0, 240), ("IG project bond", 5.0, 250), ("Backstopped senior", 3.0, 270),
            ("Backstopped senior", 7.0, 260), ("Neocloud bond", 4.0, 500), ("Neocloud bond", 6.0, 520)]
    return [T.Deal(f"d{i}", tier, date(2025, 6, 1) + timedelta(days=40 * i), years, float(bp), None, "test")
            for i, (tier, years, bp) in enumerate(spec)]


def _with_hy(deals: list[T.Deal], hy: T.HyInputs) -> list[T.Deal]:
    """The deals with their HY at issue as `deals()` attaches it (ICE first, else the proxy)."""
    out = []
    for d in deals:
        p = hy.at(d.issue_date)
        out.append(replace(d, hy=None if p is None else p.value, hy_proxy=p is not None and p.proxy))
    return out


def _pt(day: date, value: float, proxy: bool) -> T.HyPoint:
    return T.HyPoint(day, value, proxy, PROXY_NAME if proxy else ICE_NAME, PROXY_URL if proxy else None)


def test_without_any_hy_the_market_term_is_dropped_and_said_missing():
    tf = T.fit_deals(DAY, _deals(), [], T.HyInputs())
    assert tf.hy_mode is None and tf.hy_coef is None and tf.hy_input is None
    assert any(T.HY_SERIES in m and T.HY_PROXY in m for m in tf.missing)
    assert T.fit_doc(tf)["inputs"] == []


def test_the_proxy_moves_the_curve_at_the_methodology_coefficient_and_stated_mean():
    deals = _deals()
    flat = T.fit_deals(DAY, deals, [], T.HyInputs())
    hy = T.HyInputs(proxy={date(2026, 10, 20): _pt(date(2026, 10, 20), 3.076988, True)})
    tf = T.fit_deals(DAY, deals, [], hy)
    assert tf.hy_mode == "fixed" and tf.missing == []  # the HY note is gone: the proxy fills the day
    assert (tf.hy_coef, tf.hy_mean, tf.hy_now) == (33.0, 2.89, 3.076988)
    assert tf.levels == pytest.approx(flat.levels)  # levels fitted without the market term, as before
    shift = 33.0 * (3.076988 - 2.89)
    assert tf.spread("Neocloud bond", 5.0) == pytest.approx(flat.spread("Neocloud bond", 5.0) + shift)
    inp = tf.hy_input
    assert inp["proxy"] is True and inp["proxyLabel"] == "proxy" and inp["proxyFor"] == T.HY_SERIES
    assert inp["sourceName"] == PROXY_NAME and inp["series"] == T.HY_PROXY and inp["asOf"] == "2026-10-20"
    assert inp["method"] == "fund OAS of USHY, which tracks the ICE BofA US High Yield Constrained Index"
    assert inp["tag"] == "R" and inp["displayAllowed"] is False and inp["label"].endswith("(proxy: USHY fund OAS)")
    doc = T.fit_doc(tf)
    assert doc["hyMode"] == "fixed" and "2.89%" in doc["hyBasis"] and doc["inputs"] == [inp]


def test_the_ice_series_wins_whenever_it_has_a_value():
    deals = _deals()
    ice = {d.issue_date: _pt(d.issue_date, 3.0 + 0.1 * i, False) for i, d in enumerate(deals)}
    ice[DAY] = _pt(DAY, 3.2, False)
    proxy = {DAY: _pt(DAY, 3.08, True)}
    hy = T.HyInputs(ice=ice, proxy=proxy)
    assert hy.at(DAY).proxy is False and hy.at(DAY).value == 3.2
    tf = T.fit_deals(DAY, _with_hy(deals, hy), [], hy)
    assert tf.hy_mode == "fitted" and tf.hy_now == 3.2 and tf.hy_input["proxy"] is False
    assert tf.hy_input["sourceName"] == ICE_NAME and "method" not in tf.hy_input
    assert tf.hy_mean == pytest.approx(sum(p.value for d, p in ice.items() if d != DAY) / len(deals))
    # FRED down today only: the ICE history still fits b; today's level comes from the proxy, labelled.
    del ice[DAY]
    today_proxy = T.fit_deals(DAY, _with_hy(deals, hy), [], hy)
    assert today_proxy.hy_mode == "fitted" and today_proxy.hy_now == 3.08 and today_proxy.hy_input["proxy"]


def test_a_stale_value_is_not_carried_more_than_a_week():
    hy = T.HyInputs(proxy={date(2026, 10, 1): _pt(date(2026, 10, 1), 3.0, True)})
    tf = T.fit_deals(DAY, _deals(), [], hy)
    assert tf.hy_mode is None and tf.hy_now is None and any("no value within 7 days" in m for m in tf.missing)


def _series_fact(conn, sid: str, day: date, value: float, name: str, producer: str) -> None:
    FactWriter(conn).add("series", sid, "value", value, tag="R", source_name=name, valid_from=day,
                         producer=producer, status="published", unit="pct")


def test_load_hy_reads_both_series_through_pit(conn):
    _series_fact(conn, T.HY_PROXY, date(2026, 10, 20), 3.076988, PROXY_NAME, "collector:etf_oas")
    hy = T.load_hy(conn, DAY)
    assert not hy.ice and hy.at(DAY).value == pytest.approx(3.076988) and hy.at(DAY).proxy
    assert T.hy_series(conn, DAY) == {}  # the ICE-only reader never returns the proxy
    _series_fact(conn, T.HY_SERIES, date(2026, 10, 19), 2.95, ICE_NAME, "collector:fred")
    hy = T.load_hy(conn, DAY)
    assert hy.at(DAY).proxy is False and hy.at(DAY).value == pytest.approx(2.95)
    assert T.hy_series(conn, DAY) == {date(2026, 10, 19): pytest.approx(2.95)}


def test_real_comps_with_only_the_proxy(conn):
    copy_price_inputs(["EX-JUPITER-190"])
    conn.execute("DELETE FROM fact WHERE subject_type = 'series' AND subject_id IN (%s, %s)",
                 (T.HY_SERIES, T.HY_PROXY))
    day = date(2026, 10, 14)
    book = load_curves(conn, day, since=date(2025, 1, 1))
    comps = load_comps(conn, day)
    flat = T.fit(conn, day, comps, book)
    assert flat.hy_mode is None and any(T.HY_SERIES in m for m in flat.missing)
    _series_fact(conn, T.HY_PROXY, date(2026, 10, 13), 3.10, PROXY_NAME, "collector:etf_oas")
    tf = T.fit(conn, day, comps, book)
    assert tf.hy_mode == "fixed" and not any(T.HY_SERIES in m for m in tf.missing)
    assert tf.hy_input["proxy"] and tf.hy_input["asOf"] == "2026-10-13"
    for tier in tf.levels:
        assert tf.spread(tier, 5.0) == pytest.approx(flat.spread(tier, 5.0) + 33.0 * (3.10 - 2.89))
