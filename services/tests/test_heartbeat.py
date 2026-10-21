from __future__ import annotations

import pytest

from marden.core import heartbeat, sources


@pytest.fixture
def source():
    sources.ensure_source(
        "test_ercot_feed", "ERCOT feed (test)", "feed", "https://www.ercot.com", 10080,
        "Public market information; no login",
    )
    return "test_ercot_feed"


def _source_row(committed, source_id):
    return committed.execute("SELECT * FROM source WHERE id = %s", (source_id,)).fetchone()


def _pipeline_events(committed):
    rows = committed.execute("SELECT payload FROM stream_event ORDER BY id").fetchall()
    return [(r["payload"]["source"], r["payload"]["status"]) for r in rows if r["payload"]["type"] == "pipeline"]


def test_success_records_counters_and_last_ok(committed, source):
    with heartbeat.run(source) as hb:
        hb.seen(3)
        hb.new()
        hb.meta["report"] = "2026-10"
    run = committed.execute("SELECT * FROM source_run WHERE id = %s", (hb.run_id,)).fetchone()
    assert run["ok"] is True and run["finished_at"] is not None
    assert (run["items_seen"], run["items_new"], run["meta"]) == (3, 1, {"report": "2026-10"})
    row = _source_row(committed, source)
    assert row["last_ok_at"] is not None and row["last_error_at"] is None
    assert _pipeline_events(committed) == []  # healthy -> healthy: nothing to announce


def test_failure_is_recorded_then_reraised(committed, source):
    with pytest.raises(RuntimeError, match="report moved"), heartbeat.run(source) as hb:
        hb.seen()
        raise RuntimeError("report moved")
    run = committed.execute("SELECT * FROM source_run WHERE id = %s", (hb.run_id,)).fetchone()
    assert run["ok"] is False and run["items_seen"] == 1
    assert run["error"] == "RuntimeError: report moved"
    row = _source_row(committed, source)
    assert row["last_error_at"] is not None and row["last_error"] == "RuntimeError: report moved"
    assert _pipeline_events(committed) == [(source, "down")]

    with heartbeat.run(source):
        pass
    assert _pipeline_events(committed) == [(source, "down"), (source, "ok")]
    assert len(heartbeat.latest_runs(source)) == 2


def test_failure_survives_rollback_of_the_run_transaction(committed, source):
    from marden.core import db

    with pytest.raises(ValueError), heartbeat.run(source), db.transaction() as conn:
        conn.execute("INSERT INTO entity (id, name, kind) VALUES ('rolled-back', 'x', 'other')")
        raise ValueError("parse failed")
    assert committed.execute("SELECT count(*) AS n FROM entity").fetchone()["n"] == 0
    assert committed.execute("SELECT ok FROM source_run").fetchone()["ok"] is False


def test_blocked_source_is_refused_and_recorded(committed):
    sources.ensure_source(
        "test_county_portal", "County portal", "portal", "https://county.example", 1440,
        "Terms of use forbid automated access", blocked=True, blocked_reason="ToS section 4 forbids scraping",
    )
    ran = False
    with pytest.raises(sources.SourceBlocked), heartbeat.run("test_county_portal"):
        ran = True
    assert not ran
    run = committed.execute("SELECT ok, error FROM source_run").fetchone()
    assert run["ok"] is False and "blocked" in run["error"]


def test_unknown_source_raises(committed):
    with pytest.raises(sources.UnknownSource), heartbeat.run("never_declared"):
        pass
