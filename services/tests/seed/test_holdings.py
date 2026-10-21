"""The `holdings` seed loader against a slice of the real archive (MARDEN_SEED_DIR).

Parser tests read real archive rows. The database tests load a few real instruments with the instruments
loader, then a slice of the real N-PORT, BDC and NAIC lines (selected by file and borrower key) into the
throwaway test database, and check holdings, links, entities, prices, facts and idempotency. Expected values
are read from the archive files themselves or from docs/methods/price-sources.md.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from marden.core import config
from marden.seed import common

pytestmark = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")

INSTRUMENTS = {
    "EX-CRWV-096",  # DDTL V: BDC lines (cited filing), N-PORT lines by issuer + coupon + maturity, Palmer Square
    "EX-VANTAGE-361",  # Retained Vantage sub loan: Ares BDC lines
    "EX-XAI_VCI-390",  # VCI Asset Holdings 3: N-PORT LoanX lines and BDC 'vci' lines
    "EX-GLXY-163",  # Galaxy Helios II 9.875% 2031: agg_36322DAA4.json by CUSIP
    "EX-BEIGNET-022",  # Beignet 6.581% 2049: NAIC Schedule D line by CUSIP
    "EX-CRWV-106", "EX-CRWV-107",  # the 2031 / 2032 converts (one filer puts the stock ISIN on both)
}
FILES = {"aiwc/pxsrc/w2/res/rows_vci.json", "aiwc/pxsrc/w2/res/rows_crwv.json", "aiwc/q3x/ins.json"}
CUSIPS = {"36322DAA4"}  # its agg_ file repeats w2 lines: the w2 copy (more fields) is the one kept
BKEYS = {"coreweave financing", "retained vantage", "vci"}


def select(line) -> bool:
    return line.relpath in FILES or line.cusip in CUSIPS or (line.source == "bdc" and line.bkey in BKEYS)


@pytest.fixture(scope="module")
def seed():
    config.reload_settings()
    common.seed_dir.cache_clear()
    return common.load_json


# ---------------------------------------------------------------- pure rules on real rows


def test_line_keys_and_codes_from_real_rows(seed):
    from marden.seed.holdings_archive import HoldingsArchive, _nport_line

    arc = HoldingsArchive(base=None)  # type: ignore[arg-type]
    rows = seed("aiwc/pxsrc/w2/res/rows_crwv.json")
    bad = next(r for r in rows if r.get("isin") == "US21873S1087" and r.get("cusip") == "21873SAH1")
    line = _nport_line("aiwc/pxsrc/w2/res/rows_crwv.json", bad, None, arc)
    assert line is not None and line.cusip == "21873SAH1" and line.isin is None  # stock ISIN on a convert dropped
    assert line.codes == ("21873SAH1",) and line.accession == bad["adsh"]
    loan = next(r for r in rows if (r.get("other") or "").startswith("BL5672813 "))
    parsed = _nport_line("aiwc/pxsrc/w2/res/rows_crwv.json", loan, None, arc)
    assert parsed is not None and "BL5672813" in parsed.codes  # Bloomberg loan id without its lot suffix
    agg = seed("aiwc/pxsrc/agg_36322DAA4.json")[0]
    a = _nport_line("aiwc/pxsrc/agg_36322DAA4.json", agg, "36322DAA4", arc)
    assert a is not None and a.cusip == "36322DAA4" and a.accession.replace("-", "") in agg["url"]
    assert a.line_key.startswith("36322DAA4|PA|")


def test_rates_qualifiers_and_insurer_sources(seed):
    from marden.seed.holdings_archive import _rate, naic_url
    from marden.seed.holdings_link import qualifiers, qualifiers_agree

    assert _rate("0.06875000") == 6.875 and _rate("7.37500000") == 7.375 and _rate("0.5") == 0.5
    assert not qualifiers_agree(qualifiers("CoreWeave Compute Acquisition Co. VIII, LLC"),
                                qualifiers("Coreweave Compute Acquisitio", "Coreweave Compute Acquisition Co IV LLC"))
    assert qualifiers("VCI Asset Holdings 3 LLC") == {"3"} and qualifiers("COREWEAVE INC", "CRWV 8.5 07/15/32") == set()
    ins = seed("aiwc/q3x/ins.json")
    src = ins["src"]
    assert naic_url("Corebridge (AIG)", "TX", src) == src[1][1]
    assert naic_url("Nationwide", "OH", src) == src[0][1]
    assert all(naic_url(r["fam"], r["st"], src) for r in ins["rows"])


# ---------------------------------------------------------------- database


@pytest.fixture
def loaded(seed, committed):
    from marden.seed.holdings import load
    from marden.seed.instruments import load as load_instruments

    load_instruments(only=INSTRUMENTS)
    return load(select=select)


def _counts(c) -> dict[str, int]:
    one = lambda sql: c.execute(sql).fetchone()["n"]  # noqa: E731
    return {
        "holding": one("SELECT count(*) AS n FROM holding"),
        "linked": one("SELECT count(*) AS n FROM holding WHERE instrument_id IS NOT NULL"),
        "entity": one("SELECT count(*) AS n FROM entity"),
        "price_obs": one("SELECT count(*) AS n FROM price_obs"),
        "fact": one("SELECT count(*) AS n FROM fact WHERE producer = 'seed:holdings'"),
        "document": one("SELECT count(*) AS n FROM document"),
    }


def test_second_run_writes_nothing_new(loaded, committed):
    from marden.seed.holdings import load

    first = _counts(committed)
    again = load(select=select)
    assert _counts(committed) == first and first["holding"] > 0 and first["linked"] > 0
    assert not [k for k, v in again.counts.items() if k.endswith(".created") and v]
    runs = committed.execute("SELECT ok, items_new FROM source_run ORDER BY id").fetchall()
    assert [r["ok"] for r in runs][-2:] == [True, True] and runs[-1]["items_new"] == 0


def test_holdings_entities_and_links(loaded, committed, seed):
    from marden.seed.holdings_archive import HTML, html_blocks

    blocks = html_blocks((common.seed_dir() / HTML).read_text(encoding="utf-8"), ("data", "realnav"))
    meta = seed("watch/ref_meta.json")
    fi = meta["fund_idx"]["1287750"]  # Ares Capital
    ares = committed.execute(
        """SELECT h.*, e.kind, e.cik FROM holding h JOIN entity e ON e.id = h.fund_entity_id
            WHERE e.cik = '1287750' AND h.issuer_name LIKE 'Retained Vantage%%'""").fetchone()
    assert ares["instrument_id"] == "EX-VANTAGE-361" and ares["kind"] == "bdc"
    assert ares["filing_url"] == blocks["data"]["funds"][fi][3] and ares["filing_type"] == "10-Q"
    assert ares["period_end"].isoformat() == "2026-06-30" and ares["non_accrual"] is False
    assert ares["fund_net_assets"] == Decimal(str(blocks["realnav"]["nav"][str(fi)][0]))
    assert ares["accession"].replace("-", "") in ares["filing_url"]
    vci = committed.execute(
        "SELECT instrument_id, count(*) AS n FROM holding WHERE title ILIKE '%%VCI ASSET HOLDINGS 3%%' GROUP BY 1"
    ).fetchall()
    assert {r["instrument_id"] for r in vci} >= {"EX-XAI_VCI-390"}
    galaxy = committed.execute("SELECT count(*) AS n, count(instrument_id) AS l FROM holding WHERE cusip = '36322DAA4'"
                               ).fetchone()
    assert galaxy["n"] == galaxy["l"] > 0
    linked = {r["instrument_id"] for r in committed.execute(
        "SELECT instrument_id FROM holding WHERE cusip = '21873SAH1'").fetchall()}
    assert linked == {"EX-CRWV-107"}  # the stock ISIN filed on a 2032-convert line does not merge the converts
    naic = committed.execute("SELECT * FROM holding WHERE filing_type = 'NAIC Schedule D' AND cusip = '076912AA2'"
                             ).fetchall()
    beignet = [r for r in seed("aiwc/q3x/ins.json")["rows"] if r["cusip"] == "076912-AA-2"]
    assert len(naic) == len(beignet) and all(r["instrument_id"] == "EX-BEIGNET-022" for r in naic)
    assert all(r["title"].startswith("NAIC Schedule D: ") for r in naic)
    series = committed.execute(
        """SELECT s.kind, s.meta, p.cik FROM holding h JOIN entity s ON s.id = h.fund_entity_id
             JOIN entity p ON p.id = s.parent_id WHERE h.filing_type = 'N-PORT' LIMIT 1""").fetchone()
    assert series["kind"] == "fund" and series["cik"] == series["meta"]["registrant_cik"]
    assert not committed.execute("SELECT 1 FROM holding WHERE issuer_name ILIKE 'Vantage Bidco%%'").fetchall()


def test_prices_and_facts(loaded, committed, seed):
    from marden.seed.holdings_prices import ADJUSTED_METHOD

    prices = committed.execute("SELECT * FROM price_obs WHERE kind = 'fund_mark'").fetchall()
    holder = [p for p in prices if p["source_name"].startswith(("N-PORT: ", "BDC 10-Q: ")) and p["tag"] == "F"]
    assert holder and all(p["source_url"].startswith("https://www.sec.gov/Archives/edgar/data/") for p in holder)
    medians = [p for p in prices if " median of " in p["source_name"]]
    assert medians and all(p["tag"] == "F" and p["source_url"] is None for p in medians)
    assert any(p["source_name"].startswith("BDC 10-Q median of ") and p["instrument_id"] == "EX-CRWV-096"
               for p in medians)
    # Palmer Square DDTL V: raw F mark plus 100 x (FV + U) / (F + U) as M (price-sources.md section 2)
    fp = seed("fundpos.json")
    cols = fp["cols"]
    row = next(dict(zip(cols, x, strict=True)) for x in fp["f"]["1794776"] if x[0] == "coreweave financing")
    unfunded = Decimal(3_224_194)
    expect = (100 * (Decimal(row["fv_k"]) * 1000 + unfunded) / (Decimal(row["par_k"]) * 1000 + unfunded)).quantize(
        Decimal("0.0001"))
    adjusted = [p for p in prices if p["tag"] == "M"]
    assert [(p["instrument_id"], p["price"], p["method_version"]) for p in adjusted] == [
        ("EX-CRWV-096", expect, ADJUSTED_METHOD)]
    naic_prices = committed.execute(
        """SELECT count(*) AS n FROM price_obs p JOIN holding h ON h.instrument_id = p.instrument_id
            AND h.filing_url = p.source_url WHERE h.filing_type = 'NAIC Schedule D'""").fetchone()
    assert naic_prices["n"] == 0  # insurer statements are never price anchors
    facts = committed.execute("SELECT * FROM fact WHERE producer = 'seed:holdings'").fetchall()
    assert facts and all(f["status"] == "published" and f["document_id"] and f["tag"] == "F" for f in facts)
    assert {f["field"] for f in facts} <= {"net_assets_usd", "nav_per_share_usd"}
    assert all(f["source_name"] == "SEC EDGAR" and f["valid_from"].isoformat() == "2026-06-30" for f in facts)
    docs = [r["url"] for r in committed.execute("SELECT url FROM document").fetchall()]
    assert not [u for u in docs if "/trace/" in u or u.endswith("bondq.json") or "case_px" in u]


def test_holder_cik_on_another_party_is_not_merged(seed, committed):
    """An earlier loader can attach a filer's CIK to a party the filing mentions: library.json lists Apollo
    Origination II (UL) Capital Trust's Form 10 (CIK 2052153) under issuer 'Valor Compute (xAI)'. The BDC's
    holdings must not land on that party."""
    from marden.core import db
    from marden.seed.holdings import load

    rec = next(d for d in seed("aiwc/q3x/docs/library.json")["docs"] if str(d.get("cik")) == "2052153")
    with db.transaction() as conn:
        party = common.ensure_entity(conn, rec["iss"], "company", cik=str(rec["cik"]))
    report = load(select=lambda ln: ln.fund_key == "bdc:2052153" and ln.bkey == "vci")
    name = dict(seed("watch/ref_meta.json")["universe"])["2052153"]
    holders = committed.execute(
        "SELECT DISTINCT e.id, e.name, e.kind, e.cik, e.meta FROM holding h JOIN entity e ON e.id = h.fund_entity_id"
    ).fetchall()
    assert len(holders) == 1 and holders[0]["id"] != party
    assert holders[0]["name"] == name and holders[0]["kind"] == "bdc" and holders[0]["cik"] is None
    assert holders[0]["meta"]["cik"] == "2052153"
    assert committed.execute("SELECT cik FROM entity WHERE id = %s", (party,)).fetchone() == {"cik": "2052153"}
    assert report.skipped["holder CIK already on another entity (left untouched)"]
