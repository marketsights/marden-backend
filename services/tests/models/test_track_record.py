"""The `track_record` backtest on real rows copied from the local `marden` database (price_rows.copy_price_inputs
plus the status model's site, entity and instrument facts, their archive documents and the site events), and
tiny hand-made cases for the move and signal formulas only.

Real-row expectations are read from the copied rows themselves: Jupiter's first public price is the 90 dealer
quote of 2026-09-18 (FT via Kfgo), The Barn's bonds first print below 95 on TRACE on 2026-09-01.
"""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from price_rows import copy_price_inputs
from psycopg import sql

from marden.core import config, db
from marden.models import status as status_model
from marden.models import status_inputs, triggers
from marden.models import track_record as tr
from marden.models import track_record_cases as tc
from marden.models import track_record_scan as ts
from marden.readmodels.context import business_today

JUPITER, BARN = "EX-JUPITER-190", "EX-SALINE-308"
STATUS_SITE_FIELDS = list(status_inputs.SITE_FIELDS)


def _insert_missing(src: psycopg.Connection, dst: psycopg.Connection, table: str, where: str,
                    params: dict, nulls: tuple[str, ...] = ()) -> None:
    """Copy rows through a temp table, skipping the ones the price copy already made."""
    cols = [r[0] for r in dst.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position", (table,))]
    names = sql.SQL(", ").join(map(sql.Identifier, cols))
    select = sql.SQL(", ").join(sql.SQL("NULL") if c in nulls else sql.Identifier(c) for c in cols)
    temp = sql.Identifier(f"tmp_{table}")
    dst.execute(sql.SQL("CREATE TEMP TABLE {} (LIKE {}) ON COMMIT DROP").format(temp, sql.Identifier(table)))
    query = sql.SQL("COPY (SELECT {} FROM {} WHERE {}) TO STDOUT").format(select, sql.Identifier(table), sql.SQL(where))
    with src.cursor().copy(query, params) as out, dst.cursor().copy(
            sql.SQL("COPY {} ({}) FROM STDIN").format(temp, names)) as inp:
        for chunk in out:
            inp.write(chunk)
    dst.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} ON CONFLICT DO NOTHING").format(
        sql.Identifier(table), names, names, temp))


def copy_backtest_inputs(instruments: list[str]) -> list[str]:
    """The price engine's rows plus what the status rules read for the instruments' sites."""
    copy_price_inputs(instruments)
    with psycopg.connect(BASE_URL) as src, psycopg.connect(TEST_URL) as dst:
        slugs = [r[0] for r in dst.execute("SELECT DISTINCT site_slug FROM instrument WHERE site_slug IS NOT NULL")]
        ents = [r[0] for r in dst.execute("SELECT DISTINCT entity_id FROM chain_party WHERE entity_id IS NOT NULL")]
        p = {"slugs": slugs, "ents": ents, "sf": STATUS_SITE_FIELDS, "ef": list(status_inputs.ENTITY_FIELDS)}
        facts = """status IN ('published', 'superseded') AND ((subject_type = 'site' AND subject_id = ANY(%(slugs)s)
                   AND field = ANY(%(sf)s)) OR (subject_type = 'entity' AND subject_id = ANY(%(ents)s)
                   AND field = ANY(%(ef)s)))"""
        _insert_missing(src, dst, "source", "true", p)
        _insert_missing(src, dst, "document", f"id IN (SELECT document_id FROM fact WHERE {facts})", p)
        _insert_missing(src, dst, "fact", facts, p)
        pairs = src.execute(f"SELECT id, document_id FROM fact WHERE {facts} AND document_id IS NOT NULL", p).fetchall()
        with dst.cursor() as cur:  # rows the price copy made without their archive document
            cur.executemany("UPDATE fact SET document_id = %s WHERE id = %s AND document_id IS NULL",
                            [(d, i) for i, d in pairs])
        _insert_missing(src, dst, "event", "subject_type = 'entity' AND subject_id = ANY(%(ents)s)", p,
                        nulls=("document_id", "fact_id"))
        for table in ("fact", "event", "document"):
            dst.execute(sql.SQL("SELECT setval(pg_get_serial_sequence({t}, 'id'), coalesce(max(id), 0) + 1, false) "
                                "FROM {i}").format(t=sql.Literal(table), i=sql.Identifier(table)))
    return slugs


# ---------------------------------------------------------------- formulas (tiny hand-made cases)
def _obs(day: str, price: float | None, spread: float | None = None, kind: str = "trade") -> tc.Obs:
    d = date.fromisoformat(day)
    return tc.Obs(d, price, spread, kind, "test", d)


def test_a_trace_print_below_95_needs_the_next_print():
    series = [_obs("2026-04-01", 99), _obs("2026-04-02", 99), _obs("2026-04-03", 99),
              _obs("2026-05-01", 94), _obs("2026-05-02", 97), _obs("2026-06-01", 94.5), _obs("2026-06-02", 93)]
    move, base = tc.detect_move("X", "bond", series)
    assert base["price"] == 99 and move is not None and move.day == date(2026, 6, 1) and move.rule == "price<95"
    unconfirmed, _ = tc.detect_move("X", "bond", series[:6])
    assert unconfirmed is None  # the last print alone is not a move yet


def test_spread_rule_baseline_below_95_and_loans_at_par():
    discount = [_obs("2026-04-01", 80, 150), _obs("2026-04-02", 80, 150), _obs("2026-04-03", 80, 150),
                _obs("2026-07-01", 70, 360), _obs("2026-07-02", 69, 365)]
    move, _ = tc.detect_move("X", "bond", discount)
    assert move is not None and move.rule == "spread+200" and move.day == date(2026, 7, 1) and move.driver == "credit"
    no_spread = [_obs("2026-04-01", 80), _obs("2026-04-02", 80), _obs("2026-04-03", 80), _obs("2026-07-01", 70),
                 _obs("2026-07-02", 69)]
    assert tc.detect_move("X", "bond", no_spread)[0] is None  # already below 95 at the start: no price rule
    quote = [_obs("2026-09-18", 90, kind="dealer")]
    move, base = tc.detect_move("L", "construction_loan", quote)
    assert base["price"] == 100.0 and "par" in base["basis"] and move is not None and move.day == date(2026, 9, 18)


def test_signals_fire_against_the_window_start_state():
    base = {"day": "2026-04-01", "basis": {"price": "r", "status": "r", "cushion": "r"},
            "price": {"A": 99.0, "B": 90.0}, "status": {"s": {"status": "Holding", "codes": []}}, "cushion": {"s": 8}}
    later = {"day": "2026-07-09", "basis": {"price": "r", "status": "r", "cushion": "r"},
             "price": {"A": 96.0, "B": 89.0}, "status": {"s": {"status": "Strained", "codes": ["T1 tenant"]}},
             "cushion": {"s": 2}}
    fired = {f.signal: f for f in ts.fires(["A", "B"], ["s"], base, later)}
    assert set(fired) == {"status", "cushion"}  # B was below 95 at the start; A is not below 95
    assert "T1 tenant" in fired["status"].detail and "0-3" in fired["cushion"].detail
    later["price"]["A"] = 94.0
    assert {f.subject for f in ts.fires(["A", "B"], ["s"], base, later) if f.signal == "price"} == {"A"}


def test_refine_days_cover_the_week_before_the_first_signal():
    states = {date(2026, 4, 1): {}, date(2026, 4, 8): {}}
    fire = ts.Fire(date(2026, 4, 8), "status", "s", "", "reconstructed")
    assert ts.refine_days(states, fire) == [date(2026, 4, 1) + timedelta(days=n) for n in range(1, 7)]
    assert ts.scan_days(date(2026, 4, 20)) == [date(2026, 4, 1), date(2026, 4, 8), date(2026, 4, 15),
                                               date(2026, 4, 20)]


# ---------------------------------------------------------------- real rows
@pytest.fixture
def jupiter_rows(test_database):
    return copy_backtest_inputs([JUPITER, BARN])


def test_market_moves_from_the_copied_prices(jupiter_rows, monkeypatch):
    with db.transaction() as conn:
        insts = tc.case_instruments(conn, tc.CASES)
        series = tc.market_series(conn, list(insts), date(2026, 10, 21))
        kinds = {i: v["kind"] for i, v in insts.items()}
        quote = conn.execute("SELECT obs_date, price FROM price_obs WHERE instrument_id = %s AND kind = 'dealer' "
                             "ORDER BY obs_date LIMIT 1", (JUPITER,)).fetchone()
        first_low = conn.execute(
            """SELECT obs_date FROM price_obs WHERE instrument_id = %s AND kind = 'trade' AND price < 95
                ORDER BY obs_date LIMIT 1""", (BARN,)).fetchone()
    jupiter = tc.case_market(next(c for c in tc.CASES if c.key == "jupiter"), kinds, series)
    assert jupiter.move is not None and jupiter.move.day == quote["obs_date"]
    assert jupiter.move.price == float(quote["price"]) and jupiter.move.kind == "dealer"
    barn = tc.case_market(next(c for c in tc.CASES if c.key == "the-barn"), kinds, series)
    assert barn.move is not None and barn.move.day >= first_low["obs_date"] and barn.move.kind == "trade"
    with db.transaction() as conn:  # a backtest as of the day before the quote does not see it
        early = tc.case_market(next(c for c in tc.CASES if c.key == "jupiter"), kinds,
                               tc.market_series(conn, [JUPITER], quote["obs_date"] - timedelta(days=1)))
    assert early.move is None
    monkeypatch.setenv("MARDEN_SHOW_UNLICENSED_PRICES", "false")
    config.reload_settings()
    try:
        with db.transaction() as conn:
            hidden = tc.market_series(conn, [BARN], date(2026, 10, 21))
    finally:
        monkeypatch.delenv("MARDEN_SHOW_UNLICENSED_PRICES")
        config.reload_settings()
    assert all(o.kind != "trade" for o in hidden.get(BARN, []))  # TRACE is unlicensed: dropped from inputs


def test_reconstructed_status_matches_the_status_models_own_backtest(jupiter_rows):
    day = date(2026, 9, 1)
    state = ts.compute_day((day.isoformat(), [JUPITER], ["jupiter"], ["jupiter"]))
    assert state["basis"] == {"price": "reconstructed", "status": "reconstructed", "cushion": "reconstructed"}
    price = state["price"][JUPITER]
    assert isinstance(price, float)
    # The status model's own backtest of the day, with no stored price output: the harness's only difference
    # is the day's Marden price in the lenders link, which can add an L1 / L2 but never remove a finding.
    own = status_model.run(day, ["jupiter"], trigger="test")["outputs"]["jupiter"]
    mine = state["status"]["jupiter"]
    assert ts.STATUS_RANK[mine["status"]] >= ts.STATUS_RANK[own["status"]]
    if price > 98:
        assert mine["status"] == own["status"]
    else:
        assert any(code.startswith(("L1", "L2")) for code in mine["codes"]) or mine["status"] == own["status"]
    with db.transaction() as conn:
        n = conn.execute("SELECT count(*) n FROM price_obs WHERE kind = 'marden'").fetchone()["n"]
    assert n == 0  # a reconstruction writes no price history


def test_run_shapes_the_output_for_the_read_models_and_marks_the_view(jupiter_rows, committed):
    result = tr.run(cases=["jupiter", "the-barn"], workers=1, step=56, trigger="test")
    out = result["output"]
    assert out["methodVersion"] == "track_record v0.1" and out["window"]["start"] == "2026-04-01"
    by_key = {c["key"]: c for c in out["cases"]}
    for c in by_key.values():
        assert {"name", "instrumentId", "flagged", "moved", "leadDays", "backtestRunning", "evidence"} <= set(c)
        if c["flagged"] and c["moved"]:
            assert c["leadDays"] == (date.fromisoformat(c["moved"]) - date.fromisoformat(c["flagged"])).days
        assert c["outcome"] in ("early", "same day", "late", "missed")
    assert by_key["jupiter"]["moved"] == "2026-09-18"
    assert all(e["leadDays"] > 0 for e in out["earlierCalls"])
    assert [r["link"] for r in out["rules"]] == ["End user", "Tenant", "Developer", "Lenders"]
    assert {m["name"] for m in out["models"]} == {"status", "price", "cushion", "track_record"}
    assert out["scan"]["weekly"][0] == "2026-04-01" and out["scan"]["weekly"][-1] == business_today().isoformat()
    rows = committed.execute("SELECT model, subject_type, subject_id FROM model_output WHERE run_id = %s",
                             (result["run_id"],)).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("track_record", "series", "all")]
    assert committed.execute("SELECT count(*) FROM price_obs WHERE kind = 'marden'").fetchone()[0] == 0
    # a run on a subset of cases is not the published backtest: it does not mark the screen
    assert committed.execute("SELECT count(*) FROM read_model_dirty WHERE view = 'track_record'").fetchone()[0] == 0

    from marden.readmodels.views.track_record import build_track_record

    with db.transaction() as conn:
        (key, doc), = list(build_track_record(conn))
    assert key == "all" and doc["total"] == out["total"] and doc["published"] == out["published"]
    assert {c["name"] for c in doc["cases"]} == {c["name"] for c in out["cases"]}


def test_a_live_run_on_the_day_is_used_as_the_snapshot(jupiter_rows):
    status_model.run(trigger="test")  # a live run for today
    today = business_today()
    state = ts.compute_day((today.isoformat(), [JUPITER], ["jupiter"], ["jupiter"]))
    assert state["basis"]["status"] == "snapshot"
    with db.transaction() as conn:
        stored = conn.execute("SELECT output FROM model_output WHERE model = 'status' AND subject_id = 'jupiter' "
                              "AND as_of = %s", (today,)).fetchone()["output"]
    assert state["status"]["jupiter"]["status"] == stored["status"]


def test_a_new_price_marks_the_backtest_and_a_refresh_waits_for_a_full_run(jupiter_rows):
    from marden.core.records import PriceWriter

    with db.transaction() as conn:
        PriceWriter(conn).add(JUPITER, business_today(), "dealer", 84.0, tag="R", source_name="Test desk")
        marks = triggers.dirty_marks(conn, "track_record")
    assert [k for k, _ in marks] == ["all"]
    assert "skipped" in tr.refresh(trigger="test")
