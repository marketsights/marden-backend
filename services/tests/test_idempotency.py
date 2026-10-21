"""Safe reruns (ARCHITECTURE rule 5): every writer, run twice with the same input, leaves one row.

`events.emit` is the one deliberate exception: each call is a new announcement (a stream row), so
callers emit only when a doc actually changed (readmodels.rebuild) or a state changed (heartbeat).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from marden.core import db, documents, sources
from marden.core.checks import ValidationError
from marden.core.facts import FactWriter
from marden.core.records import EventWriter, HoldingWriter, PriceWriter
from marden.readmodels import mark_dirty, upsert_doc

URL = "https://www.sec.gov/Archives/edgar/data/1/000000000026000001/primary_doc.xml"


def _count(conn, table: str, where: str = "TRUE", params: tuple = ()) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}", params).fetchone()["n"]


@pytest.fixture
def instrument(conn):
    conn.execute(
        """INSERT INTO entity (id, name, kind) VALUES ('test-fund', 'Test fund', 'fund');
           INSERT INTO site (slug, name) VALUES ('abilene', 'Abilene');
           INSERT INTO instrument (id, name, kind, issuer_name, site_slug)
           VALUES ('EX-ABILENE-000', 'Abilene construction loan', 'construction_loan', 'Vantage', 'abilene')"""
    )
    return "EX-ABILENE-000"


def _fact(w: FactWriter, **kw):
    args = dict(tag="F", source_name="SEC EDGAR", source_url=URL, valid_from=date(2026, 9, 30),
                producer="collector:test")
    args.update(kw)
    return w.add("site", "abilene", "facility_mw", 1200, **args)


def test_fact_publish_reject_review_and_verifier_twice(conn):
    w = FactWriter(conn)
    a = _fact(w)
    b = _fact(w, source_url=URL + "#2")
    assert w.publish([a.id]) == [a.id]
    marked = conn.execute("SELECT view, key, marked_at FROM read_model_dirty ORDER BY view, key").fetchall()
    assert w.publish([a.id]) == []  # already published: nothing changes, nothing re-marked
    assert conn.execute("SELECT view, key, marked_at FROM read_model_dirty ORDER BY view, key").fetchall() == marked

    review = w.request_review(b.id, title="Check MW", reason="conflict")
    assert w.request_review(b.id, title="Check MW", reason="conflict") == review
    assert _count(conn, "review_item") == 1
    w.set_verifier(b.id, "agree")
    w.set_verifier(b.id, "agree")
    assert w.reject([b.id], "wrong campus") == [b.id]
    assert w.reject([b.id], "wrong campus") == []
    assert _count(conn, "audit_log", "action = 'fact.reject'") == 1
    assert _count(conn, "fact") == 2


def test_price_same_instant_in_any_timezone_is_one_row(conn, instrument):
    w = PriceWriter(conn)
    utc = datetime(2026, 10, 1, 18, 30, tzinfo=UTC)
    eastern = utc.astimezone(timezone(timedelta(hours=-4)))
    a = w.add(instrument, date(2026, 10, 1), "trade", 98.25, tag="F", source_name="TRACE", obs_at=utc)
    b = w.add(instrument, date(2026, 10, 1), "trade", 98.25, tag="F", source_name="TRACE", obs_at=eastern)
    assert a.created and not b.created and a.id == b.id
    assert conn.execute("SELECT obs_at FROM price_obs WHERE id = %s", (a.id,)).fetchone()["obs_at"] == utc
    with pytest.raises(ValidationError, match="timezone-aware"):
        w.add(instrument, date(2026, 10, 1), "trade", 98.25, tag="F", source_name="TRACE",
              obs_at=datetime(2026, 10, 1, 18, 30))


def test_yield_only_quotes_are_kept_apart_and_deduped(conn, instrument):
    w = PriceWriter(conn)
    args = dict(tag="R", source_name="Dealer run")
    a = w.add(instrument, date(2026, 10, 2), "dealer", None, yield_pct=7.25, **args)
    b = w.add(instrument, date(2026, 10, 2), "dealer", None, yield_pct=7.5, **args)
    again = w.add(instrument, date(2026, 10, 2), "dealer", None, yield_pct=7.250, **args)
    assert a.id != b.id and again.id == a.id and not again.created
    assert _count(conn, "price_obs") == 2


def test_event_publish_and_reject_twice(conn, instrument):
    w = EventWriter(conn)
    args = dict(tag="F", source_name="Taylor County", source_url="https://www.taylorcountytexas.org/agendas")
    a = w.add("site", "abilene", date(2026, 10, 14), "docket", "Abatement hearing", **args)
    b = w.add("site", "abilene", date(2026, 10, 14), "docket", "Rezoning hearing", **args)
    assert w.publish([a.id]) == [a.id] and w.publish([a.id]) == []
    assert w.reject([b.id], "duplicate notice") == [b.id] and w.reject([b.id], "duplicate notice") == []
    assert _count(conn, "audit_log", "action = 'event.reject'") == 1
    assert _count(conn, "event") == 2


def test_holding_and_link_twice(conn, instrument):
    w = HoldingWriter(conn)
    args = dict(period_end=date(2026, 6, 30), filing_type="N-PORT", filing_url=URL, par=10_000_000)
    a = w.add("test-fund", "0000000000-26-000001", "line-1", **args)
    b = w.add("test-fund", "0000000000-26-000001", "line-1", **args)
    assert a.id == b.id and not b.created
    assert w.link_instrument(a.id, instrument) and not w.link_instrument(a.id, instrument)
    assert _count(conn, "holding") == 1


def test_document_twice_is_one_row_and_one_raw_object(conn, raw_dir):
    sources.ensure_source("test_docs", "Test docs", "api", "https://www.sec.gov", None, "test source", conn=conn)
    data = b"<edgarSubmission>holdings</edgarSubmission>"
    first = documents.record_document("test_docs", URL, data, content_type="text/xml", doc_type="N-PORT", conn=conn)
    second = documents.record_document("test_docs", URL, data, content_type="text/xml", doc_type="N-PORT", conn=conn)
    assert first.was_new and not second.was_new and first.id == second.id
    assert _count(conn, "document") == 1
    assert len([p for p in (raw_dir / "raw" / "test_docs").rglob("*") if p.is_file()]) == 1
    changed = documents.record_document("test_docs", URL, data + b" ", content_type="text/xml", conn=conn)
    assert changed.was_new and changed.id != first.id  # a new version of the URL is a new row


def test_source_upsert_twice_keeps_heartbeat_columns(conn):
    args = ("test_src", "Test source", "api", "https://example.gov", 60, "public API, no login")
    sources.ensure_source(*args, conn=conn)
    conn.execute("UPDATE source SET last_ok_at = '2026-10-20T12:00:00Z' WHERE id = 'test_src'")
    sources.ensure_source(*args, conn=conn)
    sources.sync_registered(conn=conn)
    row = conn.execute("SELECT count(*) OVER () AS n, last_ok_at FROM source WHERE id = 'test_src'").fetchone()
    assert row["n"] == 1 and row["last_ok_at"] == datetime(2026, 10, 20, 12, tzinfo=UTC)


def test_dirty_marks_and_snapshots_twice(conn):
    mark_dirty("site_detail", "abilene", conn=conn)
    mark_dirty("site_detail", "abilene", conn=conn)
    assert _count(conn, "read_model_dirty") == 1
    with db.transaction(conn) as c:
        assert upsert_doc(c, "sites", "all", [{"slug": "abilene"}]) is True
        assert upsert_doc(c, "sites", "all", [{"slug": "abilene"}]) is False
    assert _count(conn, "read_model") == 1
