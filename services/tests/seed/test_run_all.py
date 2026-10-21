"""`seed all` and its gaps report, against a slice of the real archive (MARDEN_SEED_DIR).

The full five-loader run is checked on a fresh database by hand (twice, same row counts); here the action's
order and the gaps report are checked on the real JUPITER and VANTAGE_TX cases.
"""

from __future__ import annotations

import os

import pytest

from marden.core import config
from marden.seed import common

pytestmark = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")


def test_seed_all_runs_every_loader_in_dependency_order():
    from marden import cli
    from marden.seed.run_all import ORDER, seed_all  # importing it registers the five loaders' actions

    actions = cli._groups["seed"]
    assert [name for name, _ in ORDER] == ["sites", "instruments", "holdings", "facilities", "cases"]
    assert all(actions[name].handler is handler for name, handler in ORDER)
    assert actions["all"].handler is seed_all
    assert not {"legacy_trace", "legacy_frankfurt", "legacy_yahoo"} & {name for name, _ in ORDER}


def test_gaps_report_lists_missing_critical_fields(committed):
    from marden.seed.run_all_gaps import render_gaps, site_gaps
    from marden.seed.sites import load

    config.reload_settings()
    common.seed_dir.cache_clear()
    load(["JUPITER", "VANTAGE_TX"])
    rows = {r["slug"]: r for r in site_gaps(committed)}
    assert set(rows) == {"jupiter", "frontier"}
    ctr = {r["id"]: r for r in common.load_json("aiwc/q3x/ctr.json")["rows"]}
    assert ctr["JUPITER"].get("outside") is None and "outside_date" in rows["jupiter"]["gaps"]
    assert "debt" in rows["jupiter"]["gaps"]  # no instrument loaded in this slice
    assert "power_source" not in rows["jupiter"]["gaps"]  # site_power.json: on-site gas
    text = render_gaps(committed)
    assert "| jupiter | JUPITER |" in text and "## Summary" in text
