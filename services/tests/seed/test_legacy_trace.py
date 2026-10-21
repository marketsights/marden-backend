"""The `legacy_trace` seed loader against the real archive (MARDEN_SEED_DIR).

The collapsing of copies is checked on the real files; the database test loads three real instruments'
TRACE history into the throwaway test database and checks rows, mapping by identifiers, spreads, the
aidebt copies (no new rows), facts written only when missing, and that a rerun writes nothing.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest

from marden.collectors.finra_trace_parse import bond_page_url
from marden.core import config, db
from marden.core.facts import FactWriter
from marden.seed import common

pytestmark = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")


@pytest.fixture(scope="module")
def book():
    config.reload_settings()
    common.seed_dir.cache_clear()
    from marden.seed.legacy_trace import _read_books

    return _read_books()


def test_copies_of_one_print_collapse_to_the_unrounded_observation(book):
    b, secs = book
    assert len(b.obs) == 26329 and b.outcome["eod.new"] == 25951
    apld = b.obs[("00202DAA5", date(2026, 10, 20))]  # the 10/20 10:55 ET refresh: not in eod.json
    assert (apld.price, apld.yield_pct, apld.spread_bp) == (Decimal("102.375"), Decimal("8.369364"), Decimal(347))
    assert apld.origins == {"trace_rows.last", "spreads"}
    pf1b4 = b.obs[("03790NAA5", date(2026, 10, 16))]  # case_px says 92.812: the eod price is 92.8125
    assert pf1b4.price == Decimal("92.8125") and "case_px" in pf1b4.origins
    iren = b.obs[("46270CAE9", date(2026, 10, 16))]  # trace_rows rounds it to 57.943
    assert iren.price == Decimal("57.9426") and {"eod", "trace_rows.last"} <= iren.origins
    assert b.conflicts == ["36317GAC0 2026-10-16 security_search 62.391900 vs 61.9283 (eod/trace_rows.ser)"]
    sec = secs["21873SAL2"][0]
    assert (sec.coupon_rate_pct, sec.maturity, sec.is_144a) == (Decimal("2.875"), date(2033, 4, 1), True)


def _instrument(conn, iid: str, cusip: str) -> None:
    conn.execute("INSERT INTO instrument (id, name, kind, issuer_name, cusip) VALUES (%s, %s, 'bond', 'x', %s)",
                 (iid, iid, cusip))


def test_load_writes_each_mapped_print_once(committed, book):
    from marden.seed import legacy_trace

    b, _ = book
    with db.transaction() as conn:
        for iid, cusip in (("EX-APLD-010", "00202DAA5"), ("EX-CRWV-101", "21873SAB4"), ("EX-IREN-180", "46270CAE9")):
            _instrument(conn, iid, cusip)
        facts = FactWriter(conn)
        facts.add("instrument", "EX-CRWV-101", "identifiers",
                  [{"type": "cusip", "value": "21873SAB4"}, {"type": "cusip", "value": "U2069EAA8"}],
                  tag="F", source_name="SEC EDGAR", valid_from="2026-10-20", producer="seed:instruments",
                  status="published")
        facts.add("instrument", "EX-IREN-180", "maturity", "2031-07", tag="F", source_name="SEC EDGAR",
                  valid_from="2026-10-20", producer="seed:instruments", status="published")

    first = legacy_trace.load()
    expected = sum(1 for (c, _d) in b.obs if c in {"00202DAA5", "21873SAB4", "46270CAE9"})
    regs = sum(1 for (c, _d) in b.obs if c == "U2069EAA8")
    assert first.trace.prices_new == expected + regs and regs > 100  # the RegS line maps through identifiers
    assert first.counts["aidebt_px.trace_copy"] == 3 and first.counts["aidebt_px.rows_added"] == 0

    rows = committed.execute(
        """SELECT instrument_id, obs_date, price, yield_pct, spread_bp, kind, tag, source_name, source_url, obs_at
           FROM price_obs ORDER BY instrument_id, obs_date""").fetchall()
    assert {(r["kind"], r["tag"], r["source_name"]) for r in rows} == {("trade", "F", "FINRA TRACE (finra.org)")}
    assert all(r["obs_at"] is None for r in rows)
    assert {r["source_url"] for r in rows if r["instrument_id"] == "EX-CRWV-101"} == {
        bond_page_url("21873SAB4"), bond_page_url("U2069EAA8")}
    last = [r for r in rows if r["instrument_id"] == "EX-APLD-010"][-1]
    assert (last["obs_date"], float(last["price"]), float(last["spread_bp"])) == (date(2026, 10, 20), 102.375, 347)
    got = {(r["subject_id"], r["field"]): r["value"] for r in committed.execute(
        "SELECT subject_id, field, value FROM fact_current WHERE source_name = 'FINRA TRACE (finra.org)'").fetchall()}
    assert got[("EX-IREN-180", "is_144a")] is True and got[("EX-IREN-180", "issuer_name")] == "IREN LTD"
    assert ("EX-IREN-180", "maturity") not in got  # the instrument already had one
    assert got[("EX-APLD-010", "maturity")] == "2030-12-15"

    again = legacy_trace.load()
    assert again.trace.prices_new == 0 and again.trace.facts_new == 0
    assert committed.execute("SELECT count(*) AS n FROM price_obs").fetchone()["n"] == len(rows)
