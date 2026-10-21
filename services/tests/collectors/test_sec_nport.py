"""sec_nport: parsers on real captured N-PORT responses, a full run on the test database (twice), the
failure path, and one live check against sec.gov.

Captured 2026-10-21 with SEC_USER_AGENT (trimmed to the records the tests need, structure kept):
- form.20261015.idx: header, the four NPORT-P lines of 2026-10-15 used here and six 10-Q lines.
- 0001410368-26-105017.xml: American Beacon DoubleLine Floating Rate Fund at 2026-08-31, 9 of 386
  positions (APLD, Cipher, Stingray notes, a LoanX-only loan, a non-USD share line).
- 0001410368-26-105012 / -105013 / -104912.xml: three other filings of that day, 6 positions each.
- 0000940400-26-037909 / -037899.xml: two Polen series (FundVantage Trust) at 2026-07-31.
- 0001410368-26-099845.xml (iShares, EUR rate 0.86719 per dollar) and 0001532203-26-000117.xml
  (Vanguard, EUR rate 1.15315 dollars per euro): Digital Dutch Finco EUR notes at 2026-07-31.
- 2026q3_nport_subset.zip: SUBMISSION, REGISTRANT, FUND_REPORTED_HOLDING and IDENTIFIERS rows of
  the 2026q3 DERA data set for those four accessions plus five others (EUR rates and no match).
"""

from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import sec_nport
from marden.collectors import sec_nport_fx as fx
from marden.collectors import sec_nport_parse as p
from marden.collectors import sec_nport_store as store
from marden.collectors.rates_common import due_et, today_et
from marden.collectors.sec_index_parse import daily_index_url, parse_form_index, previous_business_day
from marden.collectors.sec_nport_dataset import dataset_candidates
from marden.core import db
from marden.core.config import get_settings
from marden.core.facts import FactWriter
from marden.core.http import Fetcher, FetchError
from marden.core.queue import app
from marden.seed.holdings_lines import nport_line_key

DATA = Path(__file__).parent / "data" / "sec_nport"
ARCH = "https://www.sec.gov/Archives/edgar"
ZIP_URL = "https://www.sec.gov/files/dera/data/form-n-port-data-sets/2026q3_nport.zip"
JUL, AUG = date(2026, 7, 31), date(2026, 8, 31)


def _read(name: str) -> bytes:
    return (DATA / name).read_bytes()


def _doc(name: str) -> p.NportDoc:
    return p.parse_nport(_read(name))


# ---------------------------------------------------------------- parsers
def test_parse_primary_doc():
    doc = _doc("0001410368-26-105017.xml")
    h = doc.header
    assert (h.submission_type, h.reg_name, h.reg_cik, h.series_id, h.period) == (
        "NPORT-P", "AMERICAN BEACON FUNDS", "809593", "S000052009", AUG)
    assert h.series_name == "American Beacon DoubleLine Floating Rate Fund" and h.net_assets == Decimal("119562613.63")
    assert len(doc.positions) == 9
    apld = next(x for x in doc.positions if x.cusip == "00202DAA5")
    assert (apld.ordinal, apld.isin, apld.balance, apld.units, apld.cur_cd, apld.val_usd) == (
        5, "US00202DAA54", Decimal("45000.00000000"), "PA", "USD", Decimal("47464.50000000"))
    assert (apld.asset_cat, apld.fair_value_level, apld.payoff) == ("DBT", "2", "Long")
    assert {"00202DAA5", "US00202DAA54"} <= apld.keys and p.mark_price(apld) == Decimal("105.4767")
    kaman = next(x for x in doc.positions if x.name == "KAMAN CORPORATION")
    assert kaman.others == (("LoanX ID", "LX292249"),) and kaman.asset_cat == "LON"
    shares = next(x for x in doc.positions if x.units == "NS")
    assert (shares.cur_cd, shares.exchange_rt, shares.cusip) == ("EUR", Decimal("0.86088200"), None)
    assert p.mark_price(shares, fx.PER_USD) is None  # share counts are not par
    with pytest.raises(p.NportParseError):
        p.parse_nport(b"<Error><Code>AccessDenied</Code></Error>")


def test_prescan_and_identifier_keys():
    tokens = p.prescan(_read("0001410368-26-105017.xml"))
    assert {"00202DAA5", "US00202DAA54", "LX292249", "990ACJES0", "86083AAA4"} <= tokens
    assert "000000000" not in tokens
    assert p.norm_id(" n/a ") is None and p.norm_id("000000000") is None and p.norm_id("lx292249") == "LX292249"
    assert p.isin_cusip("US21873SAC26") == "21873SAC2" and p.isin_cusip("XS2232115423") is None
    assert p.isin_cusip("US21873SAC27") is None  # bad check digit
    assert p.id_keys("US21873SAC26", None, "") == {"US21873SAC26", "21873SAC2"}


def test_mark_price_rules_and_fx_direction():
    ishares = next(x for x in _doc("0001410368-26-099845.xml").positions if x.isin == "XS2232115423")
    vanguard = next(x for x in _doc("0001532203-26-000117.xml").positions if x.isin == "XS2232115423")
    assert (ishares.exchange_rt, vanguard.exchange_rt) == (Decimal("0.86719000"), Decimal("1.15315000"))
    assert p.mark_price(ishares) is None and p.mark_price(ishares, fx.UNVERIFIED) is None
    assert p.mark_price(ishares, fx.PER_USD) == Decimal("85.2294")
    assert p.mark_price(vanguard, fx.USD_PER) == Decimal("85.1262")
    assert p.mark_price(vanguard, fx.PER_USD) == Decimal("113.1970")  # what reading it the wrong way gives

    votes = fx.FxVotes()
    for acc in ("a1", "a2"):
        votes.add(acc, JUL, [("EUR", Decimal("0.86719"))])
    assert votes.convention("EUR", Decimal("0.86719"), JUL) == fx.UNVERIFIED  # two votes are not enough
    votes.add("a3", JUL, [("EUR", Decimal("0.865914"))])
    votes.add("a4", JUL, [("EUR", Decimal("1.15315"))])
    assert votes.reference("EUR", JUL) == Decimal("0.86719")
    assert votes.convention("EUR", Decimal("0.86915"), JUL) == fx.PER_USD
    assert votes.convention("EUR", Decimal("1.15315"), JUL) == fx.USD_PER
    assert votes.convention("EUR", Decimal("1.0"), JUL) == fx.UNVERIFIED  # a placeholder rate
    assert votes.convention("EUR", Decimal("0.86719"), AUG) == fx.UNVERIFIED  # no votes for that date
    period, pairs = fx.document_rates(_read("0001410368-26-099845.xml"))
    assert period == JUL and ("EUR", Decimal("0.86719000")) in pairs and all(c != "USD" for c, _ in pairs)


def test_line_key_matches_the_holdings_seed_loader():
    doc = _doc("0001410368-26-105017.xml")
    by_name = {x.name: x for x in doc.positions}
    apld, kaman = by_name["APLD COMPUTECO LLC"], by_name["KAMAN CORPORATION"]
    assert store.line_code(apld) == "00202DAA5" and store.line_code(kaman) == "48354VAP7"
    assert nport_line_key(store.line_code(apld), apld.units, apld.balance, apld.val_usd) == "00202DAA5|PA|45000|47464.5"
    ishares = next(x for x in _doc("0001410368-26-099845.xml").positions if x.isin == "XS2232115423")
    assert store.line_code(ishares) == "XS2232115423"
    unfunded = p.Position(ordinal=1, name="CoreWeave DDTL", title="CoreWeave DDTL 4.0 Unfunded", lei=None,
                          cusip=None, isin=None, balance=Decimal(1000), units="PA", cur_cd="USD", val_usd=Decimal(-5))
    assert store.unfunded(unfunded) and not store.unfunded(apld)


def test_daily_index_and_dataset_parsers():
    lines, unparsed = parse_form_index(_read("form.20261015.idx").decode("latin-1"))
    assert unparsed == 0 and len(lines) == 10
    nport = p.nport_lines(lines)
    assert [x.accession for x in nport] == [
        "0001410368-26-105012", "0001410368-26-105013", "0001410368-26-105017", "0001410368-26-104912"]
    assert p.primary_doc_url("809593", "0001410368-26-105017") == (
        f"{ARCH}/data/809593/000141036826105017/primary_doc.xml")
    keys = {"00202DAA5", "21873SAC2", "XS2232115423"}
    votes = fx.FxVotes()
    with zipfile.ZipFile(DATA / "2026q3_nport_subset.zip") as zf:
        found, stats = dataset_candidates(zf, keys, None, votes)
        only_aug, _ = dataset_candidates(zf, keys, {AUG})
    assert [(c.accession, c.cik, c.period, c.filing_date) for c in found] == [
        ("0000940400-26-037899", "1388485", JUL, date(2026, 9, 22)),
        ("0000940400-26-037909", "1388485", JUL, date(2026, 9, 22)),
        ("0001410368-26-099845", "1100663", JUL, date(2026, 9, 25)),
        ("0001532203-26-000117", "1532203", JUL, date(2026, 9, 28)),
    ]
    assert found[0].matched_keys == ("00202DAA5", "21873SAC2")
    assert (stats.matched_accessions, stats.matched_positions, stats.holdings_rows) == (4, 6, 20)
    assert only_aug == []
    assert votes.reference("EUR", JUL) == Decimal("0.86719")
    assert votes.convention("EUR", Decimal("1.15315"), JUL) == fx.USD_PER
    assert p.dataset_url("2026q3") == ZIP_URL and p.quarter_end(2026, 3) == date(2026, 9, 30)
    assert sec_nport.due_datasets(date(2026, 10, 21)) == ["2026q3"]
    assert sec_nport.due_datasets(date(2026, 10, 6)) == [] and sec_nport.due_datasets(date(2027, 1, 7)) == ["2026q4"]


def test_task_schedule_and_cli_are_registered():
    assert app.tasks["collect.sec_nport"].queue == "collect"
    assert app.tasks["collect.sec_nport_backfill"].queue == "collect"
    crons = {pid: t.cron for (name, pid), t in app.periodic_registry.periodic_tasks.items()
             if name == "collect.sec_nport"}
    assert crons == {"daily_0700_et": sec_nport.CRON_UTC}
    assert "sec_nport" in cli._groups["collect"]
    assert due_et(int(datetime(2026, 10, 21, 11, 0, tzinfo=UTC).timestamp()), *sec_nport.RUN_AT_ET)  # 07:00 EDT
    assert not due_et(int(datetime(2026, 10, 21, 12, 0, tzinfo=UTC).timestamp()), *sec_nport.RUN_AT_ET)


# ---------------------------------------------------------------- a run on the test database
PRIMARY = {
    f"{ARCH}/data/809593/000141036826105017/primary_doc.xml": "0001410368-26-105017.xml",
    f"{ARCH}/data/809593/000141036826105012/primary_doc.xml": "0001410368-26-105012.xml",
    f"{ARCH}/data/809593/000141036826105013/primary_doc.xml": "0001410368-26-105013.xml",
    f"{ARCH}/data/804123/000141036826104912/primary_doc.xml": "0001410368-26-104912.xml",
    f"{ARCH}/data/1388485/000094040026037909/primary_doc.xml": "0000940400-26-037909.xml",
    f"{ARCH}/data/1388485/000094040026037899/primary_doc.xml": "0000940400-26-037899.xml",
    f"{ARCH}/data/1100663/000141036826099845/primary_doc.xml": "0001410368-26-099845.xml",
    f"{ARCH}/data/1532203/000153220326000117/primary_doc.xml": "0001532203-26-000117.xml",
}
INDEX_URL = daily_index_url(date(2026, 10, 15))
INDEX_ETAG = '"e35573a7d85e38608b0f6cee6e4c5c84-gzip"'
ZIP_MODIFIED = "Wed, 07 Oct 2026 16:13:21 GMT"


class CapturedSec:
    """Serves the captured responses at their real URLs (with the validators sec.gov sends)."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.requests: list[str] = []
        self.fail = fail or set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        assert "/cgi-bin" not in url and "/search" not in url
        assert request.headers["user-agent"] == get_settings().sec_user_agent
        if url in self.fail:
            return httpx.Response(503, content=b"unavailable")
        if url == INDEX_URL:
            if request.headers.get("if-none-match") == INDEX_ETAG:
                return httpx.Response(304, headers={"etag": INDEX_ETAG})
            return httpx.Response(200, content=_read("form.20261015.idx"), headers={"etag": INDEX_ETAG})
        if url == ZIP_URL:
            if request.headers.get("if-modified-since") == ZIP_MODIFIED:
                return httpx.Response(304, headers={"last-modified": ZIP_MODIFIED})
            return httpx.Response(200, content=_read("2026q3_nport_subset.zip"),
                                  headers={"last-modified": ZIP_MODIFIED, "content-type": "application/octet-stream"})
        return httpx.Response(200, content=_read(PRIMARY[url]), headers={"content-type": "text/xml"})


@pytest.fixture
def instruments(committed):
    committed.execute(
        """INSERT INTO instrument (id, name, kind, issuer_name, cusip, isin) VALUES
             ('EX-APLD-010', '9.25% Sr Secured Notes 2030', 'bond', 'APLD ComputeCo', '00202DAA5', 'US00202DAA54'),
             ('EX-CIFR-027', '7.125% Sr Secured Notes 2030', 'bond', 'Cipher Compute', '17253NAA5', NULL),
             ('EX-CIFR-029', 'Stingray notes', 'bond', 'Stingray Compute', NULL, NULL),
             ('EX-CRWV-102', '9.00% Sr Notes 2031', 'bond', 'CoreWeave', NULL, 'US21873SAC26'),
             ('EX-TEST-LOAN', 'Kaman term loan', 'term_loan', 'Kaman', NULL, NULL),
             ('NP-DLR-DUTCH', 'Digital Dutch Finco EUR notes', 'bond', 'Digital Dutch Finco', NULL, 'XS2232115423');
           INSERT INTO entity (id, name, kind, meta) VALUES
             ('polen-high-income-etf', 'Polen High Income ETF', 'fund',
              '{"role": "nport_series", "registrant_cik": "1388485"}');
           -- one line of that filing as the holdings seed loader writes it (same fund, accession, line_key)
           INSERT INTO holding (fund_entity_id, accession, line_key, period_end, filing_type, filing_url, cusip,
                                par, value_usd, mark_price)
           VALUES ('polen-high-income-etf', '0000940400-26-037899', '00202DAA5|PA|275000|292439.4', '2026-07-31',
                   'N-PORT', 'https://www.sec.gov/Archives/edgar/data/1388485/000094040026037899/primary_doc.xml',
                   '00202DAA5', 275000, 292439.4, 106.3416)"""
    )
    with db.transaction() as conn:
        facts = FactWriter(conn)
        for iid, kind, value in (("EX-CIFR-029", "cusip", "86083AAA4"), ("EX-TEST-LOAN", "loanx", "LX292249")):
            facts.add("instrument", iid, "identifiers", [{"type": kind, "value": value, "from": ["test"]}], tag="F",
                      source_name="SEC EDGAR", valid_from=date(2026, 6, 30), producer="seed:test", status="published")
    return committed


def _fetcher(transport: CapturedSec) -> Fetcher:
    return Fetcher(client=httpx.Client(transport=httpx.MockTransport(transport)), backoff_base=0.01, max_retries=0)


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
            for t in ("document", "holding", "price_obs", "fact", "entity", "event")}


def _run(fetcher: Fetcher) -> sec_nport.RunStats:
    return sec_nport.run(today=date(2026, 10, 21), since=date(2026, 10, 15), until=date(2026, 10, 15),
                         datasets=["2026q3"], fetcher=fetcher)


def test_run_writes_holdings_marks_and_medians_once(instruments):
    db_ = instruments
    transport = CapturedSec()
    first = _run(_fetcher(transport))
    assert first.ok, first.summary()
    assert (first.filings_listed, first.filings_fetched, first.filings_skipped_no_match) == (4, 8, 3)
    assert (first.filings_matched, first.holdings_new, first.holdings_seen, first.positions_marked) == (5, 9, 10, 10)
    assert first.holdings_linked == 1  # the seeded line had no instrument
    assert first.dataset["2026q3"]["candidates"] == 4 and first.datasets_read == ["2026q3"]
    assert first.matched_by_period == {"2026-07-31": 4, "2026-08-31": 1}
    assert first.fx["EUR 2026-07-31"]["reference"] == "0.86719"
    assert first.fx["EUR 2026-07-31"][fx.PER_USD] == 1 and first.fx["EUR 2026-07-31"][fx.USD_PER] == 1

    docs = db_.execute("SELECT doc_type, external_id, extract_state FROM document ORDER BY id").fetchall()
    assert [(d["doc_type"], d["external_id"]) for d in docs] == [
        ("nport_dataset_zip", "2026q3"), ("NPORT-P", "0000940400-26-037899"), ("NPORT-P", "0000940400-26-037909"),
        ("NPORT-P", "0001410368-26-099845"), ("NPORT-P", "0001532203-26-000117"),
        ("edgar_daily_index", "form.20261015.idx"), ("NPORT-P", "0001410368-26-105017")]
    assert docs[0]["extract_state"] == "done" and docs[5]["extract_state"] == "done"

    h = db_.execute(
        "SELECT * FROM holding WHERE accession = '0001410368-26-105017' AND cusip = '00202DAA5'").fetchone()
    assert (h["fund_entity_id"], h["instrument_id"], h["period_end"], h["filed_at"]) == (
        "american-beacon-doubleline-floating-rate-fund-s000052009", "EX-APLD-010", AUG, date(2026, 10, 15))
    assert (h["par"], h["value_usd"], h["mark_price"], h["fund_net_assets"], h["line_key"]) == (
        Decimal("45000.00000000"), Decimal("47464.50000000"), Decimal("105.4767"), Decimal("119562613.63"),
        "00202DAA5|PA|45000|47464.5")
    assert h["filing_url"] == f"{ARCH}/data/809593/000141036826105017/primary_doc.xml"
    linked = {r["instrument_id"] for r in db_.execute("SELECT instrument_id FROM holding").fetchall()}
    assert linked == {"EX-APLD-010", "EX-CIFR-027", "EX-CIFR-029", "EX-CRWV-102", "EX-TEST-LOAN", "NP-DLR-DUTCH"}

    hf = {r["field"]: r["value"] for r in db_.execute(
        "SELECT field, value FROM fact WHERE subject_type = 'holding' AND subject_id = %s", (str(h["id"]),)).fetchall()}
    assert hf == {"public_date": "2026-10-15", "units": "PA", "cur_cd": "USD", "fair_value_level": "2",
                  "asset_cat": "DBT"}
    eur = db_.execute("""SELECT f.field, f.value FROM fact f JOIN holding h ON f.subject_id = h.id::text
                         WHERE f.subject_type = 'holding' AND h.accession = '0001532203-26-000117'""").fetchall()
    assert {r["field"]: r["value"] for r in eur}.items() >= {
        "cur_cd": "EUR", "exchange_rt": 1.15315, "fx_convention": "usd_per_currency"}.items()

    # the seed loader's entity for the Polen ETF is reused and now carries the series id
    polen = db_.execute("SELECT meta FROM entity WHERE id = 'polen-high-income-etf'").fetchone()
    assert polen["meta"]["series_id"] == "S000090827"
    assert db_.execute("SELECT count(*) AS n FROM holding WHERE fund_entity_id = 'polen-high-income-etf'"
                       ).fetchone()["n"] == 2
    na = db_.execute("""SELECT value, valid_from, tag, source_name FROM fact
                        WHERE subject_type = 'entity' AND field = 'net_assets_usd'
                          AND subject_id = 'american-beacon-doubleline-floating-rate-fund-s000052009'""").fetchone()
    assert (na["value"], na["valid_from"], na["tag"]) == (119562613.63, AUG, "F")
    assert na["source_name"] == "N-PORT: American Beacon DoubleLine Floating Rate Fund (2026-08-31)"

    prices = db_.execute("""SELECT instrument_id, obs_date, price, source_name, source_url FROM price_obs
                            ORDER BY instrument_id, obs_date, source_name""").fetchall()
    medians = [(r["instrument_id"], r["obs_date"], r["price"], r["source_name"]) for r in prices if not r["source_url"]]
    assert medians == [
        ("EX-APLD-010", JUL, Decimal("106.3416"), "N-PORT median of 2 funds (2026-07-31)"),
        ("EX-CRWV-102", JUL, Decimal("90.3657"), "N-PORT median of 2 funds (2026-07-31)"),
        ("NP-DLR-DUTCH", JUL, Decimal("85.1778"), "N-PORT median of 2 funds (2026-07-31)"),
    ]
    assert len(prices) == 13
    dutch = {r["price"] for r in prices if r["instrument_id"] == "NP-DLR-DUTCH" and r["source_url"]}
    assert dutch == {Decimal("85.2294"), Decimal("85.1262")}
    disp = db_.execute("""SELECT value, valid_from FROM fact WHERE field = 'fund_mark_dispersion'
                          AND subject_id = 'NP-DLR-DUTCH'""").fetchone()
    assert disp["valid_from"] == JUL and disp["value"] == {
        "period": "2026-07-31", "n_funds": 2, "median": 85.1778, "min": 85.1262, "max": 85.2294, "range_pts": 0.1032,
        "wide": False, "cusip": None, "first_public_date": "2026-09-25", "public_date": "2026-09-28"}
    dirty = {r["view"] for r in db_.execute("SELECT view FROM read_model_dirty").fetchall()}
    assert {"instrument_detail", "instruments"} <= dirty
    before = _counts(db_)

    transport.requests.clear()
    second = _run(_fetcher(transport))
    assert second.ok, second.summary()
    assert (second.documents_new, second.holdings_new, second.facts_new, second.prices_new) == (0, 0, 0, 0)
    assert (second.median_prices_new, second.median_facts_new) == (0, 0)
    assert second.index_days_done_before == ["2026-10-15"] and second.datasets_done_before == ["2026q3"]
    assert sorted(transport.requests) == sorted([INDEX_URL, ZIP_URL])  # two conditional GETs, both 304
    assert _counts(db_) == before
    runs = db_.execute("SELECT ok, items_new FROM source_run WHERE source_id = 'sec_nport' ORDER BY id").fetchall()
    assert [(r["ok"], r["items_new"]) for r in runs] == [(True, 5), (True, 0)]
    source = db_.execute("SELECT terms_note, blocked FROM source WHERE id = 'sec_nport'").fetchone()
    assert "may be copied or further distributed" in source["terms_note"] and not source["blocked"]


def test_failed_fetch_is_recorded_and_retried(instruments):
    db_ = instruments
    bad = f"{ARCH}/data/809593/000141036826105012/primary_doc.xml"
    stats = sec_nport.run(today=date(2026, 10, 21), since=date(2026, 10, 15), until=date(2026, 10, 15), datasets=[],
                          fetcher=_fetcher(CapturedSec(fail={bad})))
    assert not stats.ok and "HTTP 503" in (stats.error or "")
    assert stats.filings_matched == 1  # the other filings of the day were still collected
    source = db_.execute("SELECT last_error, last_ok_at FROM source WHERE id = 'sec_nport'").fetchone()
    assert "HTTP 503" in source["last_error"] and source["last_ok_at"] is None
    index = db_.execute("SELECT extract_state FROM document WHERE doc_type = 'edgar_daily_index'").fetchone()
    assert index["extract_state"] == "none"  # left open for the next run
    transport = CapturedSec()
    retry = sec_nport.run(today=date(2026, 10, 21), since=date(2026, 10, 15), until=date(2026, 10, 15), datasets=[],
                          fetcher=_fetcher(transport))
    assert retry.ok and (retry.filings_known, retry.holdings_new) == (1, 0)
    assert bad in transport.requests
    index = db_.execute("SELECT extract_state FROM document WHERE doc_type = 'edgar_daily_index'").fetchone()
    assert index["extract_state"] == "done"


# ---------------------------------------------------------------- live
@pytest.mark.network
def test_live_daily_index_and_primary_doc():
    with Fetcher() as fetcher:
        day, lines = today_et(), []
        for _ in range(10):  # the latest index that lists an N-PORT filing
            day = previous_business_day(day)
            try:
                resp = fetcher.get(daily_index_url(day))
            except FetchError as exc:
                assert exc.status in (403, 404)
                continue
            lines = p.nport_lines(parse_form_index(resp.text())[0])
            if lines:
                break
        assert lines, "no N-PORT filing in the last ten business days"
        line = lines[0]
        doc = p.parse_nport(fetcher.get(p.primary_doc_url(line.cik, line.accession)).content)
        assert doc.header.reg_cik == line.cik and doc.header.period <= line.filed
        assert doc.header.submission_type in p.NPORT_FORMS
