"""marden.core.licensing: the licence table, the exact source names collectors write, and the switch."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest

from marden.collectors import boerse_frankfurt, yahoo_equity
from marden.core import config, licensing


@pytest.fixture
def env(monkeypatch) -> Iterator[pytest.MonkeyPatch]:
    yield monkeypatch
    monkeypatch.undo()
    config.reload_settings()


def test_the_table_lists_the_unlicensed_and_public_sources():
    names = licensing.unlicensed_source_names()
    assert {"Börse Frankfurt", "FINRA TRACE (finra.org)", "Yahoo Finance"} <= set(names) and names == sorted(names)
    for name in ("SEC EDGAR", "U.S. Treasury", "Federal Reserve Bank of New York"):
        assert licensing.licence_for(name)["status"] == "public"
    for entry in licensing.SOURCE_LICENCE.values():
        assert entry["status"] in licensing.STATUSES and entry["note"]


def test_collectors_write_exactly_the_flagged_names():
    assert yahoo_equity.SOURCE_NAME == licensing.YAHOO_FINANCE == "Yahoo Finance"
    assert boerse_frankfurt.SOURCE_NAME == licensing.BOERSE_FRANKFURT == "Börse Frankfurt"
    assert licensing.FINRA_TRACE == "FINRA TRACE (finra.org)"
    for name in (yahoo_equity.SOURCE_NAME, boerse_frankfurt.SOURCE_NAME, licensing.FINRA_TRACE):
        assert licensing.is_unlicensed(name)
    # near misses are not silently treated as the flagged source
    assert licensing.licence_for("FINRA TRACE") is None and licensing.licence_for("Borse Frankfurt") is None


def test_per_filing_names_resolve_by_prefix():
    from marden.seed.instruments_nport import bdc_source_name, nport_source_name

    assert licensing.licence_for(nport_source_name("Vanguard Short-Term Investment-Grade", date(2026, 7, 31)))[
        "status"] == "public"
    assert licensing.licence_for(bdc_source_name("Palmer Square BDC", date(2026, 6, 30)))["status"] == "public"
    assert licensing.licence_for("N-PORT median of 3 funds (2026-07-31)")["status"] == "public"
    assert licensing.licence_for("Some press site") is None and not licensing.is_unlicensed(None)


def test_the_switch_defaults_to_showing_and_can_hide(env):
    env.delenv("MARDEN_SHOW_UNLICENSED_PRICES", raising=False)
    assert config.reload_settings().show_unlicensed_prices is True
    rows = [{"source_name": "Yahoo Finance"}, {"source_name": "SEC EDGAR"}, {"source_name": "Börse Frankfurt"}]
    assert licensing.filter_rows(rows) == rows and licensing.visible("Yahoo Finance")
    env.setenv("MARDEN_SHOW_UNLICENSED_PRICES", "false")
    config.reload_settings()
    assert licensing.show_unlicensed_prices() is False
    assert licensing.filter_rows(rows) == [{"source_name": "SEC EDGAR"}]
    assert not licensing.visible("FINRA TRACE (finra.org)") and licensing.visible("N-PORT: X (2026-07-31)")
    env.setenv("MARDEN_SHOW_UNLICENSED_PRICES", "maybe")
    with pytest.raises(config.ConfigError, match="MARDEN_SHOW_UNLICENSED_PRICES"):
        config.reload_settings()
