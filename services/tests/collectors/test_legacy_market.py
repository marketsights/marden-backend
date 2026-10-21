"""Seed loaders legacy_yahoo and legacy_frankfurt on real archive files (copied into a temporary seed dir)."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from marden.collectors import yahoo_equity as Y
from marden.collectors.rates_common import RunStats
from marden.core import config, db
from marden.seed import common, legacy_frankfurt, legacy_yahoo

DATA = Path(__file__).parent / "data"
# The collector's CRWV answer of 2026-10-21 (bars 10/15..10/21): 10/15 and 10/16 overlap the archive's y_CRWV.json,
# and Yahoo had revised the 10/16 volume (94,876,600 in the archive, 95,394,200 later).
CRWV_LIVE = (DATA / "yahoo_equity/chart_CRWV_5d_session_open.json").read_bytes()
AFTER_CLOSE = datetime(2026, 10, 21, 20, 15, tzinfo=UTC)
SNAPSHOTS = {"watch/dbread/q3/bondq.json": "bondq_2026-10-18.json", "watch/out/db/bondq.json": "bondq_2026-10-20.json"}


@pytest.fixture
def seed_dir(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    (tmp_path / "aiwc/research/dl").mkdir(parents=True)
    shutil.copy(DATA / "legacy_yahoo/y_CRWV.json", tmp_path / "aiwc/research/dl/y_CRWV.json")
    for rel, name in SNAPSHOTS.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(DATA / "legacy_frankfurt" / name, tmp_path / rel)
    shutil.copy(DATA / "legacy_frankfurt/aidebt_excerpt.json", tmp_path / "aidebt.json")
    monkeypatch.setenv("MARDEN_SEED_DIR", str(tmp_path))
    config.reload_settings()
    common.seed_dir.cache_clear()
    yield tmp_path
    monkeypatch.undo()
    config.reload_settings()
    common.seed_dir.cache_clear()


def test_legacy_yahoo_writes_the_series_once(seed_dir, committed):
    first = legacy_yahoo.load()
    assert first["counts"] == {"files": 1, "bars": 44, "facts_seen": 264, "facts_new": 264, "held_by_collector": 0}
    assert first["files"]["aiwc/research/dl/y_CRWV.json"]["last"] == "2026-10-16"
    assert legacy_yahoo.load()["counts"]["facts_new"] == 0
    close = committed.execute(
        """SELECT f.*, d.url FROM fact_current f JOIN document d ON d.id = f.document_id
           WHERE subject_type = 'series' AND subject_id = 'equity:CRWV' AND field = 'close'""").fetchone()
    assert (close["value"], close["valid_from"], close["tag"]) == (61.39, date(2026, 10, 16), "F")
    assert (close["source_name"], close["producer"]) == ("Yahoo Finance", "seed:legacy_yahoo")
    assert close["url"] == "seed://marden_data_2026-10-20/aiwc/research/dl/y_CRWV.json"


def _collector_writes_crwv() -> RunStats:
    stats = RunStats()
    with db.transaction() as conn:
        Y.write_chart(conn, "CRWV", Y.parse_chart(CRWV_LIVE, symbol="CRWV", fetched_at=AFTER_CLOSE),
                      producer=Y.PRODUCER, document_id=None, stats=stats)
    return stats


def _per_date(conn) -> list[tuple]:
    return conn.execute(
        """SELECT field, valid_from, count(*) AS n, count(DISTINCT value) AS nv FROM fact
           WHERE subject_id = 'equity:CRWV' GROUP BY 1, 2 HAVING count(*) > 1 ORDER BY 1, 2""").fetchall()


def test_archive_then_collector_hold_each_date_once(seed_dir, committed):
    legacy_yahoo.load()
    stats = _collector_writes_crwv()
    assert stats.extra["held_by_other_writer"] == 11  # 10/15 and 10/16 already held, except the revised volume
    assert stats.facts_new == 5 * 6 - 11
    assert [(r["field"], r["valid_from"], r["n"], r["nv"]) for r in _per_date(committed)] == [
        ("volume", date(2026, 10, 16), 2, 2)]  # a revision is a new value, never a copy
    assert legacy_yahoo.load()["counts"]["facts_new"] == 0


def test_collector_then_archive_hold_each_date_once(seed_dir, committed):
    assert _collector_writes_crwv().facts_new == 30
    report = legacy_yahoo.load()
    assert report["counts"]["held_by_collector"] == 12 and report["counts"]["facts_new"] == 264 - 12
    assert _per_date(committed) == []
    assert _collector_writes_crwv().facts_new == 0


def _instruments(conn, skip: set[str]) -> None:
    snap = json.loads((DATA / "legacy_frankfurt/bondq_2026-10-20.json").read_text())
    for q in snap["q"]:
        if q["id"] not in skip:
            conn.execute("INSERT INTO instrument (id, name, kind, issuer_name, isin) VALUES (%s, %s, 'bond', %s, %s)",
                         (q["id"], q["n"], q["g"], q["isin"]))


def test_legacy_frankfurt_loads_fixings_and_recognises_copies(seed_dir, committed):
    # EX-CIFR-031 is missing from the instrument table: its quotes fall back to the ISIN, which no one carries.
    _instruments(committed, {"EX-CIFR-031"})
    first = legacy_frankfurt.load()
    c = first["counts"]
    assert (c["snapshots"], c["snapshot_quotes"]) == (2, 326)
    assert c["price_obs.created"] == 324 and first["unmatched"] == ["EX-CIFR-031 US17253JAB26", "aidebt EX-CIFR-031"]
    assert c["aidebt_px_frankfurt_copies"] == 3 and c["aidebt_copy.same_as_snapshot"] == 2  # CORZ-066, ORCL-255
    again = legacy_frankfurt.load()["counts"]
    assert again.get("price_obs.created", 0) == 0 and again["price_obs.existing"] == 324

    rows = committed.execute(
        "SELECT * FROM price_obs WHERE instrument_id = 'EX-ORCL-255' ORDER BY obs_at").fetchall()
    assert len(rows) == 2
    assert [(r["obs_date"], r["price"]) for r in rows] == [(date(2026, 10, 16), Decimal("55.78")),
                                                           (date(2026, 10, 20), Decimal("54.13"))]
    last = rows[-1]
    assert last["obs_at"] == datetime(2026, 10, 20, 15, 20, 6, tzinfo=UTC)  # 17:20:06+02:00
    assert (last["kind"], last["tag"], last["source_name"]) == ("exchange", "R", "Börse Frankfurt")
    assert last["source_url"] == "https://live.deutsche-boerse.com/anleihe/us68389xca19"
    assert committed.execute("SELECT count(*) AS n FROM fact WHERE field = 'exchange_quote'").fetchone()["n"] == 0


def test_legacy_frankfurt_maps_by_identifiers_not_by_the_archive_id(seed_dir, committed):
    # EX-CORZ-066 exists but carries another ISIN: the archive's id alone does not put a price on it.
    _instruments(committed, {"EX-CORZ-066"})
    committed.execute("INSERT INTO instrument (id, name, kind, issuer_name, isin) VALUES "
                      "('EX-CORZ-066', 'x', 'bond', 'x', 'US00000000A0')")
    snap = json.loads((DATA / "legacy_frankfurt/bondq_2026-10-20.json").read_text())
    isin = next(q["isin"] for q in snap["q"] if q["id"] == "EX-CORZ-066")
    out = legacy_frankfurt.load()
    assert f"EX-CORZ-066 {isin}" in out["unmatched"] and "aidebt EX-CORZ-066" in out["unmatched"]
    assert committed.execute(
        "SELECT count(*) AS n FROM price_obs WHERE instrument_id = 'EX-CORZ-066'").fetchone()["n"] == 0
