"""fact_current precedence and fact_asof point-in-time reads (the views in the core migration)."""

from __future__ import annotations

from datetime import date, datetime

from marden.core.facts import FactWriter, current_facts, facts_asof


def _pub(w: FactWriter, value, tag, valid_from, *, source="Source", producer="collector:test", **kw):
    return w.add(
        "instrument", "EX-ABILENE-000", "coupon", value,
        tag=tag, source_name=source, valid_from=valid_from, producer=producer, status="published",
        method_version="price v0.3" if tag == "M" else None, **kw,
    )


def _model(w: FactWriter, value, valid_from):
    return _pub(w, value, "M", valid_from, producer="model:price@0.3")


def _current(conn):
    return current_facts(conn, "instrument", "EX-ABILENE-000")["coupon"]


def test_filed_beats_newer_model(conn):
    w = FactWriter(conn)
    filed = _pub(w, 6.5, "F", date(2026, 1, 15))
    _model(w, 7.1, date(2026, 10, 1))
    assert _current(conn)["id"] == filed.id


def test_reported_beats_newer_model(conn):
    w = FactWriter(conn)
    reported = _pub(w, 6.6, "R", date(2026, 2, 1), source="Reuters")
    _model(w, 7.1, date(2026, 10, 1))
    assert _current(conn)["id"] == reported.id


def test_newer_valid_from_wins_among_filed_and_reported(conn):
    w = FactWriter(conn)
    _pub(w, 6.5, "F", date(2026, 1, 15))
    newer = _pub(w, 6.75, "R", date(2026, 6, 1), source="Bloomberg")
    assert _current(conn)["id"] == newer.id


def test_filed_beats_reported_on_the_same_date(conn):
    w = FactWriter(conn)
    filed = _pub(w, 6.5, "F", date(2026, 3, 1), source="SEC EDGAR")
    _pub(w, 6.55, "R", date(2026, 3, 1), source="Reuters")
    assert _current(conn)["id"] == filed.id


def test_latest_model_wins_when_only_models(conn):
    w = FactWriter(conn)
    _model(w, 7.0, date(2026, 9, 1))
    latest = _model(w, 7.2, date(2026, 10, 1))
    assert _current(conn)["id"] == latest.id


def test_candidates_and_expired_values_are_not_current(conn):
    w = FactWriter(conn)
    w.add("instrument", "EX-ABILENE-000", "coupon", 9.9, tag="F", source_name="S",
          valid_from=date(2026, 9, 1), producer="collector:test")
    expired = _pub(w, 8.0, "F", date(2026, 8, 1))
    conn.execute("UPDATE fact SET valid_to = '2026-08-31' WHERE id = %s", (expired.id,))
    kept = _pub(w, 6.5, "F", date(2026, 1, 1))
    assert _current(conn)["id"] == kept.id


def _learned(conn, fact_id: int, when: str) -> None:
    conn.execute("UPDATE fact SET learned_at = %s WHERE id = %s", (datetime.fromisoformat(when), fact_id))


def test_fact_asof_answers_what_was_known_on_a_date(conn):
    w = FactWriter(conn)
    early = _pub(w, 6.5, "F", date(2026, 1, 1))
    later = _pub(w, 6.75, "F", date(2026, 3, 1))
    _learned(conn, early.id, "2026-01-05T15:00:00+00:00")
    _learned(conn, later.id, "2026-03-10T15:00:00+00:00")

    def known(day: date):
        row = facts_asof(conn, day, "instrument", "EX-ABILENE-000").get("coupon")
        return row["id"] if row else None

    assert known(date(2026, 1, 3)) is None  # nothing learned yet
    assert known(date(2026, 2, 1)) == early.id
    assert known(date(2026, 3, 5)) == early.id  # true since Mar 1 but not learned until Mar 10
    assert known(date(2026, 3, 10)) == later.id
    assert _current(conn)["id"] == later.id


def test_fact_asof_keeps_superseded_and_skips_rejected(conn):
    w = FactWriter(conn)
    old = _pub(w, 6.5, "F", date(2026, 1, 1))
    _learned(conn, old.id, "2026-01-02T00:00:00+00:00")
    conn.execute("UPDATE fact SET status = 'superseded' WHERE id = %s", (old.id,))
    rejected = w.add("instrument", "EX-ABILENE-000", "coupon", 5.0, tag="F", source_name="S",
                     valid_from=date(2026, 2, 1), producer="collector:test")
    w.reject([rejected.id], "wrong tranche")
    _learned(conn, rejected.id, "2026-02-02T00:00:00+00:00")
    row = facts_asof(conn, date(2026, 6, 1), "instrument", "EX-ABILENE-000")["coupon"]
    assert row["id"] == old.id
    assert "coupon" not in current_facts(conn, "instrument", "EX-ABILENE-000")
