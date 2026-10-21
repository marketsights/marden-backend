"""Seed loaders legacy_yahoo and legacy_frankfurt on real archive files (copied into a temporary seed dir)."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from marden.core import config
from marden.seed import common, legacy_frankfurt, legacy_yahoo

DATA = Path(__file__).parent / "data"
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
    assert first["counts"] == {"files": 1, "bars": 44, "facts_seen": 264, "facts_new": 264}
    assert first["files"]["aiwc/research/dl/y_CRWV.json"]["last"] == "2026-10-16"
    assert legacy_yahoo.load()["counts"]["facts_new"] == 0
    close = committed.execute(
        """SELECT f.*, d.url FROM fact_current f JOIN document d ON d.id = f.document_id
           WHERE subject_type = 'series' AND subject_id = 'equity:CRWV' AND field = 'close'""").fetchone()
    assert (close["value"], close["valid_from"], close["tag"]) == (61.39, date(2026, 10, 16), "F")
    assert (close["source_name"], close["producer"]) == ("Yahoo Finance", "seed:legacy_yahoo")
    assert close["url"] == "seed://marden_data_2026-10-20/aiwc/research/dl/y_CRWV.json"


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
    assert [(r["obs_date"], r["price"]) for r in rows] == [(date(2026, 10, 16), Decimal("55.78")),
                                                           (date(2026, 10, 20), Decimal("54.13"))]
    last = rows[-1]
    assert last["obs_at"] == datetime(2026, 10, 20, 15, 20, 6, tzinfo=UTC)  # 17:20:06+02:00
    assert (last["kind"], last["tag"], last["source_name"]) == ("exchange", "R", "Börse Frankfurt")
    assert last["source_url"] == "https://live.deutsche-boerse.com/anleihe/us68389xca19"
    assert committed.execute("SELECT count(*) AS n FROM fact WHERE field = 'exchange_quote'").fetchone()["n"] == 0
