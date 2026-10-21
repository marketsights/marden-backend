"""Point-in-time backtests read nothing that was not public on their day (pricing-v0.1 "Backtesting").

The price and cushion models are re-run as of 2026-08-17 on the real rows copied from the local `marden`
database (tests/models/price_rows.py, cushion_rows.py) inside `marden.models.pit.audit()`, which lists
every input the run read with the day it became public: facts (record date + publication lag), events,
price observations (an N-PORT mark 60 days after its period), stored model outputs (their as-of day)
and static instrument terms (public at issue). The list must hold no input dated after the backtest day,
while the same run as of today does read later inputs (so the check is not vacuous).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from cushion_rows import copy_sites
from price_rows import copy_price_inputs

from marden.core import db
from marden.models import cushion, pit
from marden.models.price import run_day
from marden.models.price_terms import Instrument, note_terms
from marden.readmodels.context import business_today

BACKTEST = date(2026, 8, 17)
SITES = ["jupiter", "the-barn", "frontier", "childress"]
INSTRUMENTS = ["EX-JUPITER-190", "EX-SALINE-308", "EX-CRWV-103", "EX-ORCL-228"]


def test_late_inputs_flags_what_was_not_yet_public():
    with pit.audit() as used:
        pit.note("fact", "site:x", "outside_date", date(2026, 8, 1), date(2026, 8, 1), "Filing")
        pit.note("price_obs", "instrument:y", "nport", date(2026, 7, 31), date(2026, 9, 29), "N-PORT: f (2026-07-31)")
    assert [i.field for i in pit.late_inputs(used, BACKTEST)] == ["nport"]
    pit.note("fact", "site:z", "f", BACKTEST, BACKTEST, "outside an audit")  # ignored: no audit open
    assert len(used) == 2


def test_static_terms_are_public_at_issue_and_late_without_one():
    inst = Instrument("T-1", "Test note", "bond", None, "x", None)
    inst.facts["coupon"] = {"valid_from": date(2026, 10, 20), "source_name": "SEC EDGAR"}
    with pit.audit() as used:
        note_terms(inst, BACKTEST, None)  # no issue comp, no observation: only its record date
        inst.comp = {"id": "issue_comp:T", "issue_date": date(2026, 3, 1), "source_name": "SEC EDGAR"}
        note_terms(inst, BACKTEST, None)
    assert [(i.public, i.basis) for i in used[:1]] == [(date(2026, 10, 20), "")]
    assert all(i.public == date(2026, 3, 1) for i in used[1:]) and len(used) == 3
    assert len(pit.late_inputs(used, BACKTEST)) == 1


def test_cushion_backtest_reads_nothing_published_after_its_day(committed):
    copy_sites(SITES)
    with pit.audit() as used:
        result = cushion.run(BACKTEST, SITES, trigger="test", mark=False)
    assert len(result["sites"]) == len(SITES)
    assert {i.kind for i in used} >= {"fact"} and len(used) > 20
    assert pit.late_inputs(used, BACKTEST) == []
    assert all(i.record_date <= i.public for i in used if i.kind == "fact")
    with pit.audit() as today:
        cushion.run(business_today(), SITES, trigger="test", mark=False)
    assert pit.late_inputs(today, BACKTEST), "a run for today reads inputs a 2026-08-17 backtest must not"


def test_price_backtest_reads_nothing_published_after_its_day(committed):
    copy_price_inputs(INSTRUMENTS)
    with db.transaction() as conn, pit.audit() as used:
        result = run_day(conn, BACKTEST)
    priced = [i for i, o in result["prices"].items() if o["price"] is not None]
    assert "EX-JUPITER-190" in priced and result["cds"]["coreweave"]["spreadBp"] is not None
    kinds = {i.kind for i in used}
    assert {"fact", "price_obs", "terms"} <= kinds
    late = pit.late_inputs(used, BACKTEST)
    assert late == [], f"{len(late)} inputs not yet public on {BACKTEST}: {late[:5]}"
    nport = [i for i in used if i.kind == "price_obs" and i.field == "nport"]
    assert nport and all(i.public == i.record_date + timedelta(days=pit.NPORT_LAG_DAYS) for i in nport)
    terms = [i for i in used if i.kind == "terms" and i.subject == "instrument:EX-JUPITER-190"]
    assert terms and all(i.public <= BACKTEST < i.record_date for i in terms if i.field != "issue_comp")
    with db.transaction() as conn, pit.audit() as today:
        run_day(conn, business_today(), ["EX-JUPITER-190"])
    assert pit.late_inputs(today, BACKTEST), "a run for today reads inputs a 2026-08-17 backtest must not"


def test_licence_switch_keeps_unlicensed_sources_out_of_every_input(committed, monkeypatch):
    """MARDEN_SHOW_UNLICENSED_PRICES=false: no input of a price run comes from an unlicensed source
    (docs/methods/licensing.md), while with the default switch the same run does read them."""
    from marden.core import config, licensing

    copy_price_inputs(["EX-CRWV-103", "EX-SALINE-308"])
    day = business_today()
    with db.transaction() as conn, pit.audit() as shown:
        run_day(conn, day)
    hidden_names = set(licensing.unlicensed_source_names())
    if not {i.source for i in shown} & hidden_names:
        pytest.skip("no unlicensed rows (TRACE, Yahoo, Frankfurt, ICE) loaded in the source database")
    monkeypatch.setenv("MARDEN_SHOW_UNLICENSED_PRICES", "false")
    config.reload_settings()
    try:
        with db.transaction() as conn, pit.audit() as used:
            result = run_day(conn, day)
    finally:
        monkeypatch.delenv("MARDEN_SHOW_UNLICENSED_PRICES")
        config.reload_settings()
    assert used and not [i for i in used if i.source in hidden_names]
    assert result["prices"]["EX-CRWV-103"]["price"] is not None  # still priced, from the N-PORT marks


def test_xbrl_value_is_public_on_its_filing_date_when_later_than_the_lag():
    """A 10-K (or a restating 10-K/A) filed after the 40-day rule is public only once filed (quote_loc)."""
    base = {"subject_type": "entity", "producer": "collector:sec_xbrl", "source_name": "SEC EDGAR XBRL",
            "valid_from": date(2025, 12, 31)}
    ten_k = {**base, "quote_loc": "us-gaap:Revenues [USD]; 10-K 0001819989-26-000012 filed 2026-03-26"}
    quick = {**base, "quote_loc": "us-gaap:Revenues [USD]; 10-K 0000320193-26-000001 filed 2026-01-20"}
    assert pit.public_date(ten_k) == date(2026, 3, 26)  # not 2026-02-09 (period end + 40)
    assert pit.public_date(quick) == date(2026, 2, 9)  # filed early: the locked 40-day rule still applies
    assert pit.public_date({**base, "quote_loc": None}) == date(2026, 2, 9)
