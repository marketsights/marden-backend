from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from marden.core.checks import ValidationError
from marden.core.records import EventWriter, HoldingWriter, PriceWriter

NPORT_URL = "https://www.sec.gov/Archives/edgar/data/1/000000000026000001/primary_doc.xml"


@pytest.fixture
def instrument(conn):
    conn.execute("INSERT INTO entity (id, name, kind) VALUES ('pgim-total-return-bond-fund', 'PGIM TRB', 'fund')")
    conn.execute(
        """INSERT INTO site (slug, name) VALUES ('abilene', 'Abilene');
           INSERT INTO instrument (id, name, kind, issuer_name, site_slug)
           VALUES ('EX-ABILENE-000', 'Abilene construction loan', 'construction_loan', 'Vantage', 'abilene')"""
    )
    return "EX-ABILENE-000"


def _dirty(conn):
    return {(r["view"], r["key"]) for r in conn.execute("SELECT view, key FROM read_model_dirty")}


def test_price_dedupe_on_instrument_date_kind_source_price(conn, instrument):
    w = PriceWriter(conn)
    a = w.add(instrument, date(2026, 9, 30), "fund_mark", 99.5, tag="F", source_name="N-PORT", source_url=NPORT_URL)
    b = w.add(instrument, "2026-09-30", "fund_mark", Decimal("99.50"), tag="F", source_name="N-PORT",
              source_url=NPORT_URL)
    c = w.add(instrument, date(2026, 9, 30), "fund_mark", 99.25, tag="F", source_name="N-PORT",
              source_url=NPORT_URL)
    assert a.created and not b.created and a.id == b.id and c.id != a.id
    assert {("instrument_detail", instrument), ("instruments", "all"), ("site_detail", "abilene")} <= _dirty(conn)


def test_intraday_prints_at_the_same_price_are_kept(conn, instrument):
    w = PriceWriter(conn)
    t1 = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
    a = w.add(instrument, date(2026, 10, 1), "trade", 98, tag="F", source_name="TRACE", obs_at=t1, volume=1e6)
    b = w.add(instrument, date(2026, 10, 1), "trade", 98, tag="F", source_name="TRACE", obs_at=t2, volume=2e6)
    assert a.id != b.id


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"kind": "marden", "tag": "F"}, "model values"),
        ({"kind": "marden", "tag": "M"}, "method_version"),
        ({"kind": "rumor"}, "price kind"),
        ({"price": -1}, "negative"),
        ({"price": None}, "price, yield or spread"),
        ({"obs_date": date(2099, 1, 1)}, "future"),
    ],
)
def test_price_validators(conn, instrument, kwargs, message):
    args = {"obs_date": date(2026, 9, 30), "kind": "quote", "price": 99.0, "tag": "R"}
    args.update(kwargs)
    with pytest.raises(ValidationError, match=message):
        PriceWriter(conn).add(instrument, args.pop("obs_date"), args.pop("kind"), args.pop("price"),
                              source_name="Dealer", **args)


def test_marden_price_with_method_version(conn, instrument):
    res = PriceWriter(conn).add(instrument, date(2026, 10, 20), "marden", 97.4, tag="M",
                                source_name="Marden price", method_version="price v0.3")
    assert res.created


def test_event_idempotent_and_future_dates_allowed(conn, instrument):
    w = EventWriter(conn)
    args = dict(tag="F", source_name="Taylor County", source_url="https://taylorcounty.example/agenda")
    a = w.add("site", "abilene", date(2027, 1, 12), "docket", "Abatement hearing", **args)
    b = w.add("site", "abilene", "2027-01-12", "docket", "Abatement hearing", **args)
    assert a.created and not b.created and a.id == b.id
    assert _dirty(conn) == set()  # candidate
    assert w.publish([a.id]) == [a.id]
    assert ("site_tab:events", "abilene") in _dirty(conn)


def test_material_event_needs_a_person(conn, instrument):
    w = EventWriter(conn)
    with pytest.raises(ValidationError, match="material"):
        w.add("site", "abilene", date(2026, 9, 24), "notice", "Force majeure notice", tag="F",
              source_name="SEC EDGAR", material=True, status="published")
    ev = w.add("site", "abilene", date(2026, 9, 24), "notice", "Force majeure notice", tag="F",
               source_name="SEC EDGAR", material=True, severity="red")
    with pytest.raises(ValidationError, match="need a person"):
        w.publish([ev.id])
    assert w.publish([ev.id], decided_by="user_cody") == [ev.id]
    assert w.reject([ev.id], "already published") == []


def test_holding_unique_per_fund_accession_line(conn, instrument):
    w = HoldingWriter(conn)
    args = dict(period_end=date(2026, 6, 30), filing_type="N-PORT", filing_url=NPORT_URL,
                filed_at=date(2026, 8, 29), par=25_000_000, value_usd=Decimal("24875000"), mark_price=99.5,
                cusip="09175RAE0")
    a = w.add("pgim-total-return-bond-fund", "0000000000-26-000001", "line-17", **args)
    b = w.add("pgim-total-return-bond-fund", "0000000000-26-000001", "line-17", **args)
    assert a.created and not b.created and a.id == b.id
    assert _dirty(conn) == set()  # not linked to an instrument yet: no screen shows it
    assert w.link_instrument(a.id, instrument)
    assert not w.link_instrument(a.id, instrument)
    # the instrument's screens, its site's pieces that list filed holders (debt tab, detail, project file), and
    # the Mispriced screen (fund marks)
    assert _dirty(conn) == {("instrument_detail", instrument), ("instruments", "all"), ("site_detail", "abilene"),
                            ("site_tab:debt", "abilene"), ("project_file", "abilene"), ("mispriced", "all")}


def test_holding_validators(conn, instrument):
    w = HoldingWriter(conn)
    with pytest.raises(ValidationError, match="CUSIP"):
        w.add("pgim-total-return-bond-fund", "acc", "1", period_end=date(2026, 6, 30), filing_type="N-PORT",
              filing_url=NPORT_URL, cusip="bad")
    with pytest.raises(ValidationError, match="filed_at"):
        w.add("pgim-total-return-bond-fund", "acc", "1", period_end=date(2026, 6, 30), filing_type="N-PORT",
              filing_url=NPORT_URL, filed_at=date(2026, 5, 1))
    with pytest.raises(ValidationError, match="filing_type"):
        w.add("pgim-total-return-bond-fund", "acc", "1", period_end=date(2026, 6, 30), filing_type="8-K",
              filing_url=NPORT_URL)
