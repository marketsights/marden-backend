"""sec_bdc: parsers on real SEC BDC Data Sets files, linking and marks, a full run on the test
database (twice), and one live check against sec.gov.

Captured 2026-10-21 with SEC_USER_AGENT (data/sec_bdc/):
- bdc-data-sets.html: the BDC Data Sets page (zip list 2022q4..2026_09).
- 2026_08_bdc.zip: the August 2026 file cut to 60 soi.tsv rows (all columns kept; followed borrowers at 13 BDCs,
  collisions, comparative-period and unrelated rows) and tag.tsv cut to the tags the parser maps.
- 2026_09_bdc.zip: the September file's header and 6 rows (one Lambda Systems line, not followed).
- 2026_06_bdc.zip: June's empty soi.tsv ("no filings with financial statement data").
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import sec_bdc
from marden.collectors import sec_bdc_borrowers as bb
from marden.collectors import sec_bdc_link as lk
from marden.collectors import sec_bdc_parse as sp
from marden.core import db
from marden.core.config import get_settings
from marden.core.facts import FactWriter
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "sec_bdc"
AUG, SEP = f"{sp.FILES_BASE}/2026_08_bdc.zip", f"{sp.FILES_BASE}/2026_09_bdc.zip"


def _read(name: str) -> bytes:
    return (DATA / name).read_bytes()


def _lines(name: str = "2026_08_bdc.zip") -> list[sp.SoiLine]:
    return sp.parse_zip(_read(name)).lines


def _line(bdc: str, text: str, lines: list[sp.SoiLine] | None = None) -> sp.SoiLine:
    found = [ln for ln in lines or _lines() if ln.bdc_name.startswith(bdc) and text in ln.identifier
             and lk.is_position(ln)]
    assert len(found) == 1, [ln.identifier for ln in found]
    return found[0]


# ---------------------------------------------------------------- parsers
def test_index_page_lists_monthly_and_quarterly_zips():
    files = sp.parse_index_page(_read("bdc-data-sets.html").decode())
    names = [f.name for f in files]
    assert names[0] == "2022q4_bdc.zip" and names[-1] == "2026_09_bdc.zip" and len(names) == 22
    q4 = next(f for f in files if f.name == "2025q4_bdc.zip")
    assert (q4.start, q4.end, q4.url) == (date(2025, 10, 1), date(2025, 12, 1), f"{sp.FILES_BASE}/2025q4_bdc.zip")
    due, left = sec_bdc.due_files(files, date(2025, 10, 1), None)
    assert [f.name for f in due][:2] == ["2025q4_bdc.zip", "2026_01_bdc.zip"] and len(due) == 10
    assert "2025q3_bdc.zip" in left and len(left) == 12
    assert [f.name for f in sec_bdc.due_files(files, date(2025, 10, 1), ["2026_08_bdc.zip"])[0]] == ["2026_08_bdc.zip"]
    with pytest.raises(sp.ParseError):
        sp.parse_index_page("<html>maintenance</html>")


def test_parse_soi_maps_relabelled_columns_and_keeps_current_period_lines():
    parsed = sp.parse_zip(_read("2026_08_bdc.zip"))
    assert (parsed.rows, len(parsed.lines), parsed.rows_other_period, parsed.rows_jv) == (60, 32, 20, 1)
    assert parsed.excluded == {"Vantage Specialty Chemicals": 2, "Meta Buyer": 1, "Oracle Vision Bidco": 1}
    palmer = _line("PALMER SQUARE", "CoreWeave Financing DDTL V", parsed.lines)
    # us-gaap/2026 labels cost "Adjusted cost basis" and fair value "Initial fair value of Investment"
    assert (palmer.par, palmer.cost, palmer.fv) == (Decimal("1775806"), Decimal("1775806"), Decimal("1883231"))
    assert (palmer.spread, palmer.rate, palmer.maturity) == (Decimal("0.0450"), Decimal("0.0812"), date(2031, 11, 15))
    assert (palmer.cik, palmer.form, palmer.period, palmer.filed) == ("1794776", "10-Q", date(2026, 6, 30),
                                                                     date(2026, 8, 5))
    assert palmer.filing_url == ("https://www.sec.gov/Archives/edgar/data/1794776/000119312526333791/"
                                 "psbd-20260630.htm")  # the ix viewer URL unwrapped
    assert palmer.borrower == "coreweave_ddtl_v" and len(palmer.line_key) == 32
    assert {ln.borrower for ln in parsed.lines} >= {"coreweave_ccac_ii", "coreweave_ccac_iv", "qts_project_ram",
                                                    "nscale_uk", "retained_vantage", "yucca_oracle", "cogentrix"}
    assert not any(ln.period != date(2026, 6, 30) for ln in parsed.lines)  # 12/31/2025 comparatives dropped
    apollo_u = [ln for ln in parsed.lines if ln.bdc_name.startswith("APOLLO DEBT") and not ln.identifier]
    assert {(ln.issuer_axis, ln.unfunded) for ln in apollo_u} == {
        ("CoreWeave Financing DDTL V LLC [Member]", Decimal("1091000")),
        ("QTS Project Ram [Member]", Decimal("17985000"))}


def test_parse_small_empty_and_broken_files():
    september = sp.parse_zip(_read("2026_09_bdc.zip"))
    assert (september.rows, september.lines) == (6, [])  # Lambda Systems, Inc. is not followed
    june = sp.parse_zip(_read("2026_06_bdc.zip"))
    assert (june.rows, june.lines) == (0, [])
    with pytest.raises(sp.ParseError):
        sp.parse_zip(b"<Error><Code>NoSuchKey</Code></Error>")


# ---------------------------------------------------------------- borrower rules
def test_borrower_patterns_and_collisions():
    def key(text: str) -> str | None:
        b = bb.match_borrower(text)
        return b.key if b else None

    assert key("Coreweave Compute Acquisition Co. II, LLC Industry Technology") == "coreweave_ccac_ii"
    assert key("CoreWeave Compute Acquisition Co., IV, LLC Commitment Type Delayed Draw") == "coreweave_ccac_iv"
    assert key("CoreWeave Compute Acquisition Co. VIII, LLC") == "coreweave_ccac_viii"
    assert key("CoreWeave Financing DDTL V-V, LLC") == "coreweave_ddtl_v_v"
    assert key("Coreweave Financing DDTL V, LLC - Delayed Draw Term Loan") == "coreweave_ddtl_v"
    assert key("CoreWeave - Delayed Draw Term Loan Add on") == "coreweave"
    assert key("Retained Vantage Data Centers Intermediate Holdco, L.P., Senior subordinated loans") == (
        "retained_vantage")
    assert key("Vantage Data Centers Europe S.a r.l. | Private asset-backed investment") == "vantage_dc_europe"
    assert key("Technology Hardware Valor 2 VCI Asset Holdings 2 LLC") == "vci_2"
    assert key("Jupiter Borrower Inc (Janus Henderson Group PLC)") is None
    for text, label in [("Vantage Specialty Chemicals 1", "Vantage Specialty Chemicals"),
                        ("Vantage Bidco GMBH | One stop 1", "Vantage Bidco"),
                        ("Oracle Vision Bidco Limited | First Lien", "Oracle Vision Bidco"),
                        ("Crusoe Bidco Limited Investment", "Crusoe Bidco"), ("Meta Buyer LLC", "Meta Buyer"),
                        ("Galaxy Universal LLC", "Galaxy Universal")]:
        assert bb.excluded_by(text) == label
    assert bb.held_by_jv("Vantage Data Centers, LLC | Emerald JV LP")
    assert not bb.held_by_jv("Abilene JV construction loan")


def test_text_rules():
    assert bb.spread_bp_from_text("CoreWeave ... Initial Term Loan | SOFR + 6.000%| 10.002%| 05/16/2029") == 600
    assert bb.spread_bp_from_text("First Lien Secured Debt - Delayed Draw Interest Rate S+450, 0.00% Floor") == 450
    assert bb.spread_bp_from_text("Interest Rate 3M SOFR + 9.62% (13.29%)") == 962
    assert bb.spread_bp_from_text("Ref SOFR(M) Spread 4.50% Total Coupon 8.12%") == 450
    assert bb.spread_bp_from_text("Equipment Financing 11.00% fixed") is None
    assert bb.maturity_from_text("Initial Acquisition Date 8/21/2023 Maturity 7/31/2028") == date(2028, 7, 31)
    assert bb.maturity_from_text("Technology | CoreWeave | SOFR + 6.000%| 9.700 % | 05/16/2029") == date(2029, 5, 16)
    assert bb.maturity_from_text("Commitment Type Delayed Draw Commitment Expiration Date 3/31/2026") is None
    assert [bb.line_class(t) for t in ("Ark Data Centers, LLC | First-lien revolving loan",
                                       "Flexential Issuer, LLC | Structured Secured Note - Class C",
                                       "Nscale Limited | Series B Preferred Shares",
                                       "Crusoe Energy Systems LLC | Equity",
                                       "Equity Real Estate Investment Trusts (REITs) North America Data Center "
                                       "Acquisition LLC Investment First Lien")] == [
        "revolver", "security", "equity", "equity", "loan"]
    assert bb.loanx_ids("VCI AH1 1L Term Loan (LX276526)") == {"LX276526"}


# ---------------------------------------------------------------- marks
def test_raw_and_unfunded_adjusted_marks():
    palmer = _line("PALMER SQUARE", "CoreWeave Financing DDTL V")
    assert lk.raw_mark(palmer) == (Decimal("106.0494"), None)
    # Palmer Square footnote 13: "$3,224,194 was unfunded" (price-sources.md §2): P = 102.15
    assert lk.adjusted_mark(palmer, lk.Unfunded(Decimal("3224194"), Decimal(0), "footnote")) == Decimal("102.1485")
    assert lk.raw_mark(_line("HPS", "Non-Affiliated Issuer")) == (None, "cost/par outside 0.5-1.5")  # par = commitment
    assert lk.raw_mark(_line("FRANKLIN BSP", "DDTL V")) == (None, "cost/par outside 0.5-1.5")  # scale error
    lines = _lines()
    unfunded, unpaired = lk.assign_unfunded(lines)
    nscale = _line("BLUE OWL CAPITAL CORP", "NSCALE", lines)
    u = unfunded[(nscale.adsh, nscale.line_key)]
    assert (u.amount, u.source) == (Decimal("38344000"), "commitment line")
    assert lk.raw_mark(nscale) == (Decimal("64.4238"), None)  # the commitment's OID sits on the funded line
    assert lk.adjusted_mark(nscale, u) == Decimal("98.7494")
    ram = _line("APOLLO DEBT", "Project Ram", lines)
    assert lk.adjusted_mark(ram, unfunded[(ram.adsh, ram.line_key)]) == Decimal("99.4400")
    assert unpaired == 1  # Apollo Origination's Yucca commitment: two Yucca lines, so no pairing


# ---------------------------------------------------------------- the test database
INSTRUMENTS = [  # (id, kind, name, issuer, coupon, coupon_spread_bp, coupon_rate_pct, maturity) as the seed has them
    ("EX-CRWV-091", "ddtl", "DDTL 1.0", "CoreWeave CCAC II, LLC", "SOFR+9.62%", 962, None, "2028-03"),
    ("EX-CRWV-092", "ddtl", "DDTL 2.0", "CoreWeave CCAC IV, LLC", "SOFR+6.0%", 600, None, "2030-08"),
    ("EX-CRWV-093", "ddtl", "DDTL 2.1", "CoreWeave CCAC IV, LLC", "SOFR+4.25%", 425, None, "2031-03"),
    ("EX-CRWV-096", "ddtl", "DDTL 5.0", "CoreWeave Financing DDTL V, LLC", "SOFR+4.50%", 450, None, "2031-11"),
    ("EX-CRWV-104", "bond", "9.625% Sr Notes 2032", "CoreWeave, Inc.", "9.625%", None, 9.625, "2032-07"),
    ("EX-CRWV-105", "bond", "8.50% EUR Sr Notes 2032", "CoreWeave, Inc.", "8.50%", None, 8.5, "2032-07"),
    ("EX-CRUSOE-076", "term_loan", "Sr secured loan 1", "Crusoe Energy Systems LLC", "16.25%", None, 16.25, "2029-11"),
    ("EX-CRUSOE-077", "term_loan", "Sr secured loan 2", "Crusoe Energy Systems LLC", "14.25%", None, 14.25, "2029-11"),
    ("EX-CRUSOE-078", "term_loan", "Loan participation", "Crusoe Energy Systems LLC", "16.25%", None, 16.25, "2027-11"),
    ("EX-GLXY-162", "term_loan", "Senior secured term loan", "Galaxy Helios I LLC", "SOFR+4.75%", 475, None, "2028-08"),
    ("EX-QTS-302", "ddtl", "Project Ram 1L DD mortgage", "QTS Project Ram", "S+225", 225, None, "2028-05-29"),
    ("EX-NSCALE-205", "ddtl", "GPU Financing Facility", "Nscale Services UK Ltd", "S+5.00%", 500, None, "2031-02"),
    ("EX-VANTAGE-361", "term_loan", "HoldCo sr sub loan", "Retained Vantage DC", "9.50% fixed", None, 9.5, "2031-12"),
    ("NP-TLN-67507122", "term_loan", "Term loan", "Talen Energy Supply", "Floating", None, None, "2032-11-25"),
    ("NP-TLN-87422LBA7", "term_loan", "TLN TL B 1L", "Talen Energy Supply", "5.6543%", None, 5.6543, "2032-10-09"),
    ("EX-ARK-019", "term_loan", "1L term loan", "Ark Data Centers, LLC", "S+475", 475, None, "2030-11"),
    ("EX-ARK-020", "ddtl", "1L delayed-draw term loan", "Ark Data Centers, LLC", "S+475", 475, None, "2030-11"),
    ("EX-ARK-021", "facility", "1L revolver", "Ark Data Centers, LLC", "S+475", 475, None, "2030-11"),
    ("BDC-COGENTRIX-Cogentrix", "other", "BDC / private-credit loan(s)", "Cogentrix Energy", None, None, None, None),
]


@pytest.fixture
def instruments(committed):
    committed.execute(
        """INSERT INTO entity (id, name, kind, cik) VALUES ('valor-compute', 'Valor Compute', 'company', '2052153');
           INSERT INTO entity (id, name, kind) VALUES ('ares-capital-corp-manager', 'Ares Management', 'company')""")
    with db.transaction() as conn:
        facts = FactWriter(conn)
        for iid, kind, name, issuer, *values in INSTRUMENTS:
            conn.execute("INSERT INTO instrument (id, name, kind, issuer_name) VALUES (%s, %s, %s, %s)",
                         (iid, name, kind, issuer))
            for field, value in zip(("coupon", "coupon_spread_bp", "coupon_rate_pct", "maturity"), values, strict=True):
                if value is not None:
                    facts.add("instrument", iid, field, value, tag="F", source_name="test",
                              valid_from=date(2026, 1, 1), producer="seed:test", status="published")
    with db.transaction() as conn:
        loaded = lk.load_instruments(conn, {i[0] for i in INSTRUMENTS})
    return loaded


def test_linking_rules(instruments):
    lines = _lines()

    def link(bdc: str, text: str) -> lk.Link:
        return lk.link_line(_line(bdc, text, lines), instruments)

    assert link("GREAT ELM", "Compute Acquisition Co. II") == lk.Link("EX-CRWV-091", "spread", priced=True)
    assert link("CARLYLE", "Co. IV") == lk.Link("EX-CRWV-092", "spread", priced=True)
    pimco = link("PIMCO", "CoreWeave")  # SOFR+6.000% due 05/16/2029: 15 months off DDTL 2.0
    assert pimco.instrument_id is None and "maturity" in pimco.reason and "spread" in pimco.reason
    assert link("PALMER SQUARE", "DDTL V") == lk.Link("EX-CRWV-096", "spread+maturity month", priced=True)
    assert link("GREAT ELM", "Unsecured Bond") == lk.Link("EX-CRWV-104", "coupon+maturity month", priced=True)
    assert link("ARES STRATEGIC", "Retained Vantage") == lk.Link("EX-VANTAGE-361", "coupon", priced=True)
    assert link("APOLLO DEBT", "Talen") == lk.Link("NP-TLN-67507122", "maturity date", priced=True)
    assert link("APOLLO DEBT", "Project Ram").method == "spread+maturity date"
    assert link("MONROE", "LLC 2") == lk.Link("EX-CRUSOE-077", "coupon", priced=True)
    assert link("MONROE", "LLC 1").instrument_id is None  # 16.25%: two Crusoe loans carry it
    assert link("APOLLO DEBT", "Cogentrix") == lk.Link("BDC-COGENTRIX-Cogentrix", "placeholder", "borrower-level "
                                                                                                 "instrument")
    ark = [lk.link_line(ln, instruments) for ln in lines if ln.borrower == "ark" and lk.is_position(ln)]
    assert len(ark) == 3 and all(a.instrument_id is None for a in ark)  # term loan, DDTL, RCF all S+475 11/27/30
    assert lk.link_line(_line("APOLLO ORIGINATION", "Term Loan", lines), instruments).reason == (
        "no instrument for this borrower")


def _routes() -> dict[str, tuple[bytes, str]]:
    return {sp.PAGE_URL: (_read("bdc-data-sets.html"), "Wed, 21 Oct 2026 09:00:00 GMT"),
            AUG: (_read("2026_08_bdc.zip"), "Tue, 20 Oct 2026 21:07:38 GMT"),
            SEP: (_read("2026_09_bdc.zip"), "Thu, 08 Oct 2026 16:21:37 GMT")}


class CapturedSec:
    """Serves the captured files at their real URLs with Last-Modified, answering 304 to a matching
    If-Modified-Since; anything else fails the test."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []
        self.routes = _routes()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert request.headers["user-agent"] == get_settings().sec_user_agent
        body, modified = self.routes[url]
        status = 304 if request.headers.get("if-modified-since") == modified else 200
        self.requests.append((url, status))
        return httpx.Response(status, content=b"" if status == 304 else body, headers={"last-modified": modified})


def _counts(conn) -> dict[str, int]:
    tables = ("document", "holding", "fact", "price_obs", "entity", "event")
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in tables}


def test_run_writes_holdings_facts_and_marks_once(instruments, committed):
    transport = CapturedSec()
    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(transport)), backoff_base=0.01)
    before = _counts(committed)
    first = sec_bdc.run(from_month=date(2026, 8, 1), fetcher=fetcher)
    assert first.ok, first.error
    assert first.files_due == ["2026_08_bdc.zip", "2026_09_bdc.zip"] and len(first.files_outside_window) == 20
    assert (first.documents_new, first.files_parsed) == (3, ["2026_08_bdc.zip", "2026_09_bdc.zip"])
    w = first.writes
    assert (w.lines, w.positions, w.holdings_new) == (32, 23, 23)
    assert (w.prices_f_new, w.prices_m_new) == (13, 3)
    assert w.raw_refused == {"cost/par outside 0.5-1.5": 3}
    assert w.unfunded_assigned == 4 and w.unfunded_unpaired == 1
    assert w.entity_conflicts == ["CIK 2052153 (APOLLO ORIGINATION II (UL) CAPITAL TRUST) is also on entity "
                                  "valor-compute"]
    after = _counts(committed)
    assert after["holding"] - before["holding"] == 23 and after["price_obs"] == 16
    assert after["entity"] - before["entity"] == w.entities_new == 13

    marks = committed.execute(
        """SELECT p.instrument_id, p.price, p.tag, p.method_version, p.source_name, p.source_url, p.obs_date
           FROM price_obs p WHERE p.instrument_id = 'EX-CRWV-096' ORDER BY p.tag, p.price""").fetchall()
    assert [(m["price"], m["tag"]) for m in marks] == [
        (Decimal("102.1631"), "F"), (Decimal("105.5838"), "F"), (Decimal("106.0494"), "F"),
        (Decimal("100.7683"), "M")]
    assert marks[3]["method_version"] == lk.METHOD_VERSION
    assert marks[3]["source_name"] == "BDC 10-Q: APOLLO DEBT SOLUTIONS BDC (2026-06-30) unfunded-adjusted"
    assert {m["obs_date"] for m in marks} == {date(2026, 6, 30)}
    assert marks[2]["source_url"].endswith("/000119312526333791/psbd-20260630.htm")

    palmer = committed.execute(
        """SELECT h.*, e.name AS bdc, e.kind, e.cik FROM holding h JOIN entity e ON e.id = h.fund_entity_id
           WHERE e.cik = '1794776'""").fetchone()
    assert (palmer["bdc"], palmer["kind"], palmer["filing_type"], palmer["instrument_id"]) == (
        "PALMER SQUARE CAPITAL BDC INC.", "bdc", "10-Q", "EX-CRWV-096")
    assert (palmer["par"], palmer["value_usd"], palmer["mark_price"], palmer["period_end"], palmer["filed_at"]) == (
        Decimal("1775806"), Decimal("1883231"), Decimal("106.0494"), date(2026, 6, 30), date(2026, 8, 5))
    assert palmer["issuer_name"] == "CoreWeave Financing DDTL V, LLC" and palmer["accession"] == "0001193125-26-333791"
    facts = {f["field"]: f for f in committed.execute(
        "SELECT * FROM fact WHERE subject_type = 'holding' AND subject_id = %s", (str(palmer["id"]),)).fetchall()}
    assert {k: f["value"] for k, f in facts.items()} == {
        "cost_usd": 1775806, "spread_bp": 450, "interest_rate_pct": 8.12, "maturity": "2031-11-15"}
    assert {(f["tag"], f["status"], f["producer"], f["valid_from"]) for f in facts.values()} == {
        ("F", "published", "collector:sec_bdc", date(2026, 6, 30))}
    nscale = committed.execute(
        """SELECT h.mark_price, f.value FROM holding h JOIN fact f ON f.subject_type = 'holding'
           AND f.subject_id = h.id::text AND f.field = 'unfunded_commitment_usd'
           WHERE h.instrument_id = 'EX-NSCALE-205'"""
    ).fetchone()
    assert (nscale["mark_price"], nscale["value"]) == (Decimal("64.4238"), 38344000)
    hps = committed.execute(
        "SELECT mark_price, instrument_id FROM holding WHERE title LIKE 'CoreWeave Financing DDTL V, LLC | Non-Aff%'"
    ).fetchone()
    assert (hps["mark_price"], hps["instrument_id"]) == (None, "EX-CRWV-096")  # held, linked, no price
    placeholder = committed.execute("SELECT mark_price FROM holding WHERE instrument_id LIKE 'BDC-COGENTRIX%'")
    assert [p["mark_price"] for p in placeholder.fetchall()] == [Decimal("100.0165")]
    placeholder_prices = committed.execute("SELECT count(*) AS n FROM price_obs WHERE instrument_id LIKE 'BDC-%'")
    assert placeholder_prices.fetchone()["n"] == 0
    apollo_ul = committed.execute("SELECT id, cik FROM entity WHERE name = 'APOLLO ORIGINATION II (UL) CAPITAL TRUST'"
                                  ).fetchone()
    assert apollo_ul["id"] != "valor-compute" and apollo_ul["cik"] == "2052153"
    dirty = {r["view"] for r in committed.execute("SELECT view FROM read_model_dirty").fetchall()}
    assert {"instrument_detail", "instruments", "curve"} <= dirty

    transport.requests.clear()
    second = sec_bdc.run(from_month=date(2026, 8, 1), fetcher=fetcher)
    assert second.ok and second.files_parsed == [] and second.files_unchanged == ["2026_08_bdc.zip", "2026_09_bdc.zip"]
    assert {status for _, status in transport.requests} == {304}
    assert _counts(committed) == after
    third = sec_bdc.run(from_month=date(2026, 8, 1), reparse=True, fetcher=fetcher)
    assert third.ok and third.writes.holdings_new == 0 and third.writes.facts_new == 0
    assert (third.writes.prices_f_new, third.writes.prices_m_new, third.writes.entities_new) == (0, 0, 0)
    assert _counts(committed) == after
    runs = committed.execute("SELECT ok, items_seen, items_new FROM source_run WHERE source_id = 'sec_bdc' ORDER BY id"
                             ).fetchall()
    assert [(r["ok"], r["items_seen"], r["items_new"]) for r in runs] == [(True, 32, 23), (True, 0, 0), (True, 32, 0)]
    source = committed.execute("SELECT terms_note, blocked FROM source WHERE id = 'sec_bdc'").fetchone()
    assert "may be copied or further distributed" in source["terms_note"] and not source["blocked"]


def test_failed_fetch_is_recorded_on_the_source(committed):
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"unavailable")

    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(down)), max_retries=0)
    stats = sec_bdc.run(fetcher=fetcher)
    assert not stats.ok and "HTTP 503" in (stats.error or "")
    assert committed.execute("SELECT count(*) AS n FROM holding").fetchone()["n"] == 0
    source = committed.execute("SELECT last_error, last_ok_at FROM source WHERE id = 'sec_bdc'").fetchone()
    assert "HTTP 503" in source["last_error"] and source["last_ok_at"] is None


def test_task_schedule_and_cli_are_registered():
    task = app.tasks["collect.sec_bdc"]
    assert task.queue == "collect"
    periodic = app.periodic_registry.periodic_tasks.items()
    crons = {pid: t.cron for (name, pid), t in periodic if name == "collect.sec_bdc"}
    assert crons == {"daily_check": sec_bdc.CRON_UTC}
    assert "sec_bdc" in cli._groups["collect"]
    assert all(sec_bdc.due_on(d) for d in (date(2026, 10, 9), date(2026, 11, 3), date(2026, 11, 20),
                                            date(2026, 2, 15), date(2026, 3, 10), date(2026, 5, 1), date(2026, 8, 20)))
    assert not any(sec_bdc.due_on(d) for d in (date(2026, 10, 21), date(2026, 11, 21), date(2026, 2, 14),
                                                date(2026, 3, 11), date(2026, 6, 1), date(2026, 12, 8)))


def test_a_line_another_loader_holds_is_reused(instruments, committed):
    """The seed's fundpos import holds Palmer Square's DDTL V line ($k-rounded) and priced it: the collector
    puts its facts on that row instead of adding a second holding or a second mark for the filing."""
    url = "https://www.sec.gov/Archives/edgar/data/1794776/000119312526333791/psbd-20260630.htm"
    committed.execute("""INSERT INTO source (id, name, kind, terms_note) VALUES ('seed_test', 'seed', 'seed', 'test');
        INSERT INTO document (source_id, url, sha256, storage_key) VALUES ('seed_test', 'seed://fundpos.json',
          repeat('a', 64), 'raw/seed_test/aa/' || repeat('a', 64));
        INSERT INTO entity (id, name, kind, cik) VALUES ('palmer', 'PALMER SQUARE CAPITAL BDC', 'bdc', '1794776')""")
    committed.execute(
        """INSERT INTO holding (fund_entity_id, instrument_id, period_end, filing_type, filing_url, accession, par,
             value_usd, mark_price, document_id, line_key)
           SELECT 'palmer', 'EX-CRWV-096', '2026-06-30', '10-Q', %(url)s, '0001193125-26-333791', 1776000, 1883000,
             106.0248, id, 'coreweave financing|1L|450|2031-11' FROM document WHERE source_id = 'seed_test'""",
        {"url": url})
    committed.execute(
        """INSERT INTO price_obs (instrument_id, obs_date, kind, price, tag, source_name, source_url, document_id,
             dedupe_key) SELECT 'EX-CRWV-096', '2026-06-30', 'fund_mark', 106.0248, 'F', 'BDC 10-Q: PALMER',
             %(url)s, id, 'seed-palmer' FROM document WHERE source_id = 'seed_test'""", {"url": url})
    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(CapturedSec())), backoff_base=0.01)
    w = sec_bdc.run(from_month=date(2026, 8, 1), fetcher=fetcher).writes
    assert (w.holdings_new, w.holdings_reused, w.marks_left_to_other_loader, w.prices_f_new) == (22, 1, 1, 12)
    rows = committed.execute("SELECT id, par FROM holding WHERE fund_entity_id = 'palmer'").fetchall()
    assert [r["par"] for r in rows] == [Decimal("1776000")]
    facts = committed.execute("SELECT field FROM fact WHERE subject_type = 'holding' AND subject_id = %s",
                              (str(rows[0]["id"]),)).fetchall()
    assert {f["field"] for f in facts} == {"cost_usd", "spread_bp", "interest_rate_pct", "maturity"}


# ---------------------------------------------------------------- live
@pytest.mark.network
def test_live_index_page_and_a_published_zip():
    with Fetcher() as fetcher:
        files = sp.parse_index_page(fetcher.get(sp.PAGE_URL).text())
        assert len(files) >= 22 and files[-1].start >= date(2026, 9, 1)
        january = next(f for f in files if f.name == "2026_01_bdc.zip")  # 0.9 MB, a closed month
        parsed = sp.parse_zip(fetcher.get(january.url).content)
        assert parsed.rows > 1000 and all(ln.period <= date(2026, 1, 31) for ln in parsed.lines)
