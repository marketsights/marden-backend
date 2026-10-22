"""The Debt tab's "Recovery if <tenant> walks" card (views/sites_recovery.py) on real seeded rows copied from
the local `marden` database (site_rows): the headline is the `recovery` model's own number for the site's
main instrument, the lines are the arithmetic that produces it (recomputed here from the model output's
basis), every line has a value or n/d with a plain reason (never bracketed text), and the price model's
recovery is only ever shown as an assumption ('55c assumed (no site collateral model yet)'), and only when the
price did not use the site waterfall.
"""

from __future__ import annotations

from typing import Any

import pytest
from real_rows import copied_rows
from site_rows import copy_sites

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import sites as _sites  # noqa: F401 (registers builders)
from marden.readmodels.views import sites_debt, sites_detail  # noqa: F401
from marden.readmodels.views.instrument_text import DASH
from marden.readmodels.views.sites_recovery import money_b

TIMES, MINUS, OVER = "\u00d7", "\u2212", "\u00f7"


@pytest.fixture
def sites() -> list[str]:
    return copy_sites()


def build(view: str, key: str) -> Any:
    b = get_builder(view)
    assert b is not None, view
    with building(business_today()), db.transaction() as conn:
        return b.build_one(conn, key)


def latest(model: str, instrument_id: str) -> dict[str, Any] | None:
    rows = copied_rows("SELECT version, output FROM model_output WHERE model = %s AND subject_id = %s "
                       "AND as_of <= %s ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1",
                       (model, instrument_id, business_today()))
    return rows[0] if rows else None


def lines(card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["label"]: i for i in card["inputs"]}


def test_headline_is_the_recovery_models_own_number_and_an_assumption_is_labelled(sites):
    seen = 0
    for slug in sites:
        tab = build("site_tab:debt", slug)
        main = (build("site_detail", slug) or {}).get("debtExposed")
        if tab is None or main is None:
            continue
        card = tab["recovery"]
        rec, price = latest("recovery", main["instrumentId"]), latest("price", main["instrumentId"])
        value = rec["output"]["recovery"]["value"] if rec else None
        if value is not None:
            seen += 1
            assert card["range"]["value"] == f"{value}c", slug
            assert card["range"]["sourceName"] == "Marden recovery model" and card["range"]["tag"] == "M"
            assert "reason" not in card and "assumed" not in card, slug
            continue
        assert card["range"]["value"] is None and card["reason"].startswith("Not derived"), slug
        cents = price and price["output"].get("recovery")
        if isinstance(cents, int | float) and price["output"].get("recoveryBasis") != "site_waterfall":
            assert card["assumed"]["value"].startswith(f"{round(cents * 100)}c assumed ("), slug
            assert "price model v0" not in card["assumed"]["value"], slug  # the version is in the source tag
            assert card["assumed"]["sourceName"] == "Marden price model", slug
        else:
            assert "assumed" not in card, slug
    assert seen, "no site's main instrument has a recovery-model number"


def test_every_line_has_a_value_or_nd_with_a_plain_reason(sites):
    for slug in sites:
        tab = build("site_tab:debt", slug)
        if tab is None:
            continue
        card = tab["recovery"]
        texts = [card.get("reason"), (card.get("assumed") or {}).get("value"), card["range"]["value"]]
        assert not any("[" in t or "]" in t for t in texts if t), (slug, texts)
        for item in card["inputs"]:
            assert item["value"]["tag"] == "M" and item["value"].get("sourceName"), (slug, item)
            if item["value"]["value"] is None:
                assert item.get("reason") and "[" not in item["reason"], (slug, item)
            else:
                assert "reason" not in item and "[" not in item["value"]["value"], (slug, item)


def test_jupiter_says_why_there_is_no_number_instead_of_showing_55c_as_derived(sites):
    if "jupiter" not in sites:
        pytest.skip("Jupiter is not in the source database")
    main = build("site_detail", "jupiter")["debtExposed"]
    rec = latest("recovery", main["instrumentId"])
    shell = rec["output"] if rec and rec["output"].get("method") == "powered_shell" else None
    if shell is None or shell["basis"].get("completion") is not None:
        pytest.skip("Jupiter's completion is known now: the card is derived")
    card = build("site_tab:debt", "jupiter")["recovery"]
    basis, by = rec["output"]["basis"], lines(card)
    assert card["range"]["value"] is None and "share of the works finished" in card["reason"]
    assert by["Share finished"]["value"]["value"] is None
    assert by["Share finished"]["reason"] == "nothing delivered yet and no completion share stated"
    assert by["Powered shell value"]["reason"] == "needs the share finished"
    assert by["Recovery"]["value"]["value"] is None
    assert by["Site capacity"]["value"]["value"] == f"{basis['grossMw']:,.0f} gross MW"
    assert by["Claims at the loan's rank"]["value"]["value"].startswith(money_b(basis["pariClaimsB"]))
    # the table and the site header carry the price model's recovery as an assumption, never as derived
    rows = build("site_tab:debt", "jupiter")["instruments"]
    loan = next(i for i in rows if i["instrumentId"] == main["instrumentId"])
    for shown in (loan["recovery"], main["recovery"]):
        if shown["value"] is not None and shown.get("sourceName") == "Marden price model":
            assert " assumed (" in shown["value"], shown


def test_jupiter_shows_an_estimated_share_and_a_derived_recovery_never_nd_next_to_55c(sites):
    """v0.2: with nothing delivered and no stated completion, the share finished is estimated (tag M) and
    explained, the card has a number, and the price uses that same per-site figure."""
    if "jupiter" not in sites:
        pytest.skip("Jupiter is not in the source database")
    main = build("site_detail", "jupiter")["debtExposed"]
    rec = latest("recovery", main["instrumentId"])
    basis = rec["output"]["basis"] if rec else {}
    if not basis.get("completionEstimated"):
        pytest.skip("no estimated completion for Jupiter in the source database")
    card = build("site_tab:debt", "jupiter")["recovery"]
    by = lines(card)
    assert card["range"]["value"] and "reason" not in card and "assumed" not in card
    assert by["Share finished"]["value"]["value"].startswith(f"{basis['completion']:.0%} (estimated: ")
    assert by["How the share is estimated"]["value"]["tag"] == "M"
    price = latest("price", main["instrumentId"])
    if price and price["output"].get("recoveryBasis") == "site_waterfall":
        assert round(price["output"]["recovery"] * 100) == basis["baseCents"]
        assert main["recovery"]["value"] == f"{basis['baseCents']}c (site collateral model)"


def test_abilene_lines_multiply_out_to_the_headline(sites):
    if "abilene" not in sites:
        pytest.skip("Abilene is not in the source database")
    main = build("site_detail", "abilene")["debtExposed"]
    rec = latest("recovery", main["instrumentId"])
    basis = rec["output"]["basis"] if rec else {}
    if not rec or basis.get("completion") is None or not basis.get("coverCents"):
        pytest.skip("no derived powered-shell recovery with its arithmetic for Abilene")
    card = build("site_tab:debt", "abilene")["recovery"]
    by = lines(card)
    q, gross, share = basis["compQuartilesUsdPerMw"], basis["grossMw"], basis["completion"]
    value, ahead, pari = rec["output"]["valueB"]["value"], basis["seniorClaimsB"], basis["pariClaimsB"]
    # v0.2: energized share at comp value, the unfinished rest less the haircut, plus unspent proceeds
    e, cut = basis.get("energizedShare", share), basis.get("unfinishedHaircut", 0.0)
    works = q[1] * gross * (e + (share - e) * (1 - cut))
    assert value == pytest.approx((works + (basis.get("unspentProceedsUsd") or 0)) / 1e9, rel=1e-3)
    parts = f"({e:.0%} energized + {share - e:.0%} unfinished {TIMES} (1 {MINUS} {cut:.0%}))" if share - e > 1e-4 \
        else f"{share:.0%}"
    assert by["Powered shell value"]["value"]["value"].startswith(
        f"${q[1] / 1e6:,.1f}mm {TIMES} {gross:,.0f} MW {TIMES} {parts} = {money_b(works / 1e9)}")
    cover = round((value - ahead) / pari * 100)
    assert cover == pytest.approx(basis["coverCents"][1], abs=1)
    text = by["Recovery"]["value"]["value"]
    assert text.startswith(
        f"({money_b(value)} {MINUS} {money_b(ahead)}) {OVER} {money_b(pari)} = {basis['coverCents'][1]}c")
    lo, hi = (max(0, min(100, c)) for c in (basis["coverCents"][0], basis["coverCents"][2]))
    assert card["range"]["value"] == (f"{lo}c" if lo == hi else f"{lo}{DASH}{hi}c")
    assert ("capped at 100c" in text) == (basis["coverCents"][1] > 100)
