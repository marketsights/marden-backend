"""The `instruments` seed loader against a slice of the real archive (MARDEN_SEED_DIR).

Parser tests read real aidebt.json rows; the database tests load a few real instruments into the throwaway
test database (committed, truncated after each test) and check identity rows, facts, tags, prices and
idempotency. Expected values are read from the archive or from docs/methods/price-sources.md.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from marden.core import config
from marden.seed import common

pytestmark = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")

SLICE = {
    "EX-JUPITER-190",  # press dealer quote (FT via Reuters on kfgo.com)
    "EX-APLD-010",  # legacy px copies the TRACE pull; N-PORT lines in the cited filings
    "EX-CRWV-093",  # issuer "approximates fair value": a fact, not a price
    "EX-CRWV-095",  # DDTL 4.0: drawn amount; floating-tranche CUSIP named in the research note
    "EX-VANTAGE-361",  # Vantage sub loan: BDC 10-Q marks
    "NP-EQIX-29390XAE4",  # legacy px is a Börse Frankfurt fixing
    "EX-ABILENE-000",  # no price anywhere
}


@pytest.fixture(scope="module")
def rows():
    config.reload_settings()
    common.seed_dir.cache_clear()
    aidebt = common.load_json("aidebt.json")
    cols = aidebt["cols"].split(",")
    return {r[0]: dict(zip(cols, r, strict=True)) for r in aidebt["rows"]}


# ---------------------------------------------------------------- pure rules on real rows


def test_kinds_from_real_rows(rows):
    from marden.seed.instruments_parse import instrument_kind

    gn = common.load_json("aidebt.json")["gn"]
    expect = {
        "EX-ABILENE-000": "construction_loan", "EX-CRWV-095": "ddtl", "EX-CRWV-107": "convert",
        "EX-ALIGNED-002": "abs", "EX-QTS-296": "cmbs", "EX-CRWV-110": "lease", "EX-APLD-014": "facility",
        "EX-ORCL-254": "bond", "EX-VANTAGE_ORCL-367": "construction_loan", "BDC-CRWV-CoreWeave": "other",
        "EX-CORZ-072": "muni", "NP-EQIX-29447JAB4": "bond",
    }
    got = {i: instrument_kind(rows[i], group_name=(gn.get(rows[i]["grp"]) or [""])[0]) for i in expect}
    assert got == expect


def test_coupon_and_maturity_parsing_keeps_only_what_the_text_states(rows):
    from marden.seed.instruments_parse import parse_coupon, parse_maturity

    assert parse_coupon(rows["EX-JUPITER-190"]["cpn"]) == {
        "coupon_type": "floating", "coupon_benchmark": "SOFR", "coupon_spread_bp": 250.0}
    assert parse_coupon(rows["EX-CRWV-103"]["cpn"]) == {"coupon_type": "fixed", "coupon_rate_pct": 9.75}
    assert parse_coupon("~SOFR+2.50%") == {"coupon_type": "floating", "coupon_benchmark": "SOFR"}
    assert parse_coupon("15% / 15% / 13%") == {}
    assert parse_maturity(rows["EX-CRWV-095"]["mat"]) == ("2032-03", None)  # a month is not padded to a day
    assert parse_maturity(rows["EX-JUPITER-190"]["mat"]) == ("2029-11-09", None)
    assert parse_maturity("2027-2066") == (None, "2027-2066")


def test_identifier_checks_on_real_ids():
    from marden.seed.instruments_parse import cusip_valid, isin_valid, nsin_like

    ids = common.load_json("aiwcredit.json")["ids"]
    assert cusip_valid(ids["EX-CRWV-103"]) and not cusip_valid("000000000")
    assert nsin_like(ids["NP-EQIX-29390XAD6"])  # the common code inside XS2941363637, not a CUSIP
    assert isin_valid("XS2941363637") and isin_valid("US21873SAG30") and not isin_valid("US21873SAG31")


def test_legacy_px_copies_of_blocked_sources_are_recognised(rows):
    from marden.seed.instruments_archive import read_blocked_in_memory
    from marden.seed.instruments_prices import FRANKFURT_COPY, TRACE_COPY, classify_legacy_px

    status = classify_legacy_px(rows, *read_blocked_in_memory())
    counts = {s: list(status.values()).count(s) for s in (TRACE_COPY, FRANKFURT_COPY)}
    assert counts == {TRACE_COPY: 163, FRANKFURT_COPY: 68}  # price-sources.md section 3
    assert status["EX-APLD-010"] == TRACE_COPY and status["NP-EQIX-29390XAE4"] == FRANKFURT_COPY
    assert "EX-JUPITER-190" not in status


def test_fund_mark_price_is_the_shared_dedupe_rule():
    from marden.seed.instruments_nport import fund_mark_price

    # Thrivent Core High Yield, 21873SAK4 at 7/31: 718,000 PA, $642,632.49 (price-sources.md section 2)
    assert fund_mark_price("642632.49", "718000") == Decimal("89.5031")
    assert fund_mark_price(1, 0) is None


# ---------------------------------------------------------------- database


@pytest.fixture
def loaded(rows, committed):
    from marden.seed.instruments import load

    return load(only=SLICE)


def _counts(c) -> dict[str, int]:
    one = lambda sql: c.execute(sql).fetchone()["n"]  # noqa: E731
    return {t: one(f"SELECT count(*) AS n FROM {t}") for t in
            ("instrument", "entity", "site", "fact", "price_obs", "document")}


def test_second_run_writes_nothing_new(loaded, committed):
    from marden.seed.instruments import load

    first = _counts(committed)
    again = load(only=SLICE)
    assert _counts(committed) == first
    assert again.counts["fact.created"] == 0 and again.counts["fact.existing"] == loaded.counts["fact.created"]
    runs = committed.execute("SELECT ok, items_seen, items_new FROM source_run ORDER BY id").fetchall()
    assert [(r["ok"], r["items_seen"], r["items_new"]) for r in runs] == [(True, 7, 7), (True, 7, 0)]


def test_identity_rows(loaded, committed):
    rows = {r["id"]: r for r in committed.execute("SELECT * FROM instrument").fetchall()}
    assert set(rows) == SLICE
    assert rows["EX-JUPITER-190"]["site_slug"] == "jupiter" and rows["EX-JUPITER-190"]["in_v1"]
    assert rows["NP-EQIX-29390XAE4"]["in_v1"] is False
    title = common.load_json("aiwc/memo/titles.json")["JUPITER"][0]
    assert committed.execute("SELECT name, legacy_group FROM site WHERE slug = 'jupiter'").fetchone() == {
        "name": title, "legacy_group": "JUPITER"}
    assert rows["EX-CRWV-095"]["cusip"] == "21881PAB0"  # named in the archive's research note
    coreweave = committed.execute("SELECT ticker FROM entity WHERE id = 'coreweave'").fetchone()
    assert coreweave == {"ticker": "CRWV"}
    assert committed.execute(
        "SELECT parent_id FROM entity WHERE id = %s", (rows["EX-CRWV-095"]["issuer_entity_id"],)
    ).fetchone() == {"parent_id": "coreweave"}


def test_only_allowed_prices_are_written(loaded, committed, rows):
    prices = committed.execute("SELECT * FROM price_obs ORDER BY instrument_id, obs_date").fetchall()
    by_id: dict[str, list] = {}
    for p in prices:
        by_id.setdefault(p["instrument_id"], []).append(p)
    jupiter = by_id["EX-JUPITER-190"]
    assert [(p["kind"], float(p["price"]), p["tag"]) for p in jupiter] == [("dealer", 90.0, "R")]
    assert jupiter[0]["source_url"] == rows["EX-JUPITER-190"]["src"][1]
    # TRACE / Frankfurt copies and the issuer fair-value figure are never prices
    for iid in ("EX-APLD-010", "NP-EQIX-29390XAE4", "EX-CRWV-093"):
        assert all(abs(float(p["price"]) - rows[iid]["px"]) > 0.005 for p in by_id.get(iid, []))
    assert "EX-CRWV-093" not in by_id and "NP-EQIX-29390XAE4" not in by_id and "EX-ABILENE-000" not in by_id
    marks = [p for ps in by_id.values() for p in ps if p["kind"] == "fund_mark"]
    assert marks and all(p["tag"] == "F" and p["source_url"].startswith("https://www.sec.gov/Archives/") for p in marks)
    assert all(p["source_name"].startswith(("N-PORT: ", "BDC 10-Q: ")) for p in marks)
    assert {p["source_name"] for p in by_id["EX-VANTAGE-361"]} >= {"BDC 10-Q: ARES CAPITAL CORP (2026-06-30)"}
    docs = [r["url"] for r in committed.execute("SELECT url FROM document").fetchall()]
    assert not [u for u in docs if "/trace/" in u or u.endswith("bondq.json") or "case_px" in u]


def test_facts_are_sourced_and_tagged(loaded, committed, rows):
    facts = committed.execute(
        "SELECT * FROM fact WHERE subject_type = 'instrument' AND producer = 'seed:instruments'").fetchall()
    assert facts and all(f["status"] == "published" and f["document_id"] for f in facts)
    assert all(f["method_version"] for f in facts if f["tag"] == "M")
    f = {(x["subject_id"], x["field"]): x for x in facts}
    coupon = f[("EX-JUPITER-190", "coupon")]
    assert coupon["value"] == rows["EX-JUPITER-190"]["cpn"] and coupon["tag"] == "F"
    assert coupon["source_url"] == rows["EX-JUPITER-190"]["src"][0] and coupon["source_name"] == "SEC EDGAR"
    assert f[("EX-JUPITER-190", "amount_usd")]["value"] == 18_000_000_000
    assert f[("EX-CRWV-095", "amount_basis")]["value"] == "drawn"
    assert f[("EX-CRWV-093", "issuer_fair_value_disclosure")]["tag"] == "F"
    assert f[("EX-CRWV-093", "legacy_price_status")]["value"] == "issuer_fair_value_not_a_price"
    assert f[("EX-APLD-010", "legacy_price_status")]["value"] == "trace_copy_blocked"
    # no fact summarises price_obs (other writers add prices, so it would depend on what they stored)
    assert not [k for k in f if k[1] == "price_basis"]
    # 'price < 90' came from blocked legacy prices: dropped, and the score built on it is not written
    assert "price < 90" not in f[("EX-JUPITER-190", "flags")]["value"]
    assert ("EX-JUPITER-190", "legacy_score") not in f
    assert ("EX-ABILENE-000", "maturity") not in f  # missing values are simply not written
