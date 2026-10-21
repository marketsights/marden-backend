"""sec_xbrl: parsers on real captured companyfacts responses, a full run on the test database (twice), the
new-10-Q/10-K trigger, and one live check against data.sec.gov.

Captured 2026-10-21 from https://data.sec.gov/api/xbrl/companyfacts/ with SEC_USER_AGENT and trimmed to the
records the tests need (same JSON structure, labels and descriptions kept):
- CIK0001769628.json: CoreWeave, every mapped concept plus two unmapped ones (AccountsPayableCurrent,
  dei:EntityPublicFloat).
- CIK0001341439.json: Oracle, mapped concepts, periods ending on or after 2024-05-31 (fiscal year ends May;
  operating lease liability at 2024-05-31 restated from 7,545 to 7,500 million in a later 10-Q).
- CIK0001899123.json: Bitdeer, IFRS (ifrs-full) facts from its 20-F and 6-K filings.
- CIK0001513845.json: Nebius, cash and revenues since 2022 in RUB and USD (20-F; 2024 values re-presented).
- CIK0001083301.json: TeraWulf, operating cash flow and cover-page shares for 2021-2022 (stub periods after
  the 2021 merger; Q1 2022 restated in the 10-K).
- CIK0002052153.json: the filer behind the CIK the seed data gives Valor Compute (an Apollo trust).
- CIK0009999999.json.404.xml: data.sec.gov's answer for a CIK that does not exist.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import sec_index, sec_xbrl
from marden.collectors import sec_xbrl_parse as p
from marden.collectors import sec_xbrl_store as store
from marden.core import documents, sources
from marden.core.config import get_settings
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "sec_xbrl"
API = "https://data.sec.gov/api/xbrl/companyfacts"
TODAY = date(2026, 10, 21)
SINCE = date(2019, 1, 1)


def _cf(cik: str) -> p.CompanyFacts:
    return p.parse_companyfacts((DATA / f"CIK{int(cik):010d}.json").read_bytes())


def _obs(sel: p.Selection, field: str, valid_from: date) -> list[p.Observation]:
    return [o for o in sel.observations if o.field == field and o.valid_from == valid_from]


def _one(sel: p.Selection, field: str, valid_from: date) -> p.Observation:
    found = _obs(sel, field, valid_from)
    assert len(found) == 1, found
    return found[0]


# ---------------------------------------------------------------- parsers
def test_months_and_field_names():
    assert p.months(date(2026, 1, 1), date(2026, 3, 31)) == 3
    assert p.months(date(2025, 2, 3), date(2025, 5, 4)) == 3  # 13-week quarter
    assert p.months(date(2025, 11, 4), date(2026, 11, 1)) == 12  # 52-week year
    assert p.months(date(2024, 10, 28), date(2025, 11, 2)) == 12  # 53-week year
    assert p.months(date(2026, 1, 1), date(2026, 6, 30)) == 6 and p.months(date(2025, 6, 1), date(2026, 2, 28)) == 9
    assert p.months(date(2021, 2, 8), date(2021, 6, 30)) is None  # 143-day stub
    assert p.months(date(2023, 1, 1), date(2023, 5, 17)) is None
    names = {s.name(3) if s.kind == p.DURATION else s.name() for s in p.FIELDS}
    assert {"operating_cash_flow_3m_usd", "capex_3m_usd", "revenue_3m_usd", "cash_usd", "total_debt_usd",
            "operating_lease_liability_usd", "finance_lease_liability_usd", "construction_in_progress_usd",
            "ppe_net_usd", "shares_outstanding", "purchase_obligation_usd"} <= names
    assert len({c for s in p.FIELDS for c in s.concepts}) == sum(len(s.concepts) for s in p.FIELDS)


def test_parse_companyfacts_document():
    cf = _cf("1769628")
    assert (cf.cik, cf.name) == ("1769628", "CoreWeave, Inc.")
    assert "us-gaap:AccountsPayableCurrent" in cf.concepts and "dei:EntityPublicFloat" in cf.concepts
    assert cf.latest_filed() == date(2026, 8, 12)
    assert cf.accessions_filed_since(date(2026, 8, 1)) == ["0001769628-26-000366"]
    apollo = _cf("2052153")  # cik given as a zero-padded string in this file
    assert (apollo.cik, apollo.name) == ("2052153", "Apollo Origination II (UL) Capital Trust")
    assert p.companyfacts_url("1769628") == f"{API}/CIK0001769628.json"
    for bad in (b"<Error><Code>NoSuchKey</Code></Error>", b'{"cik": 1}', b'{"cik": "x", "facts": {}}'):
        with pytest.raises(ValueError):
            p.parse_companyfacts(bad)
    empty = p.parse_companyfacts(b'{"cik":"0000844779","entityName":"","facts":{}}')  # what BlackRock Funds gets
    assert (empty.cik, empty.concepts) == ("844779", {})


def test_select_coreweave_10q():
    sel = p.select(_cf("1769628"), since=SINCE, today=TODAY)
    assert (len(sel.observations), sel.values, dict(sel.skipped)) == (158, 147, {"shadowed": 0})
    end = date(2026, 6, 30)
    capex = _one(sel, "capex_6m_usd", end)
    assert (capex.value, capex.unit, capex.record.accession, capex.record.form) == (
        14_117_000_000, "USD", "0001769628-26-000366", "10-Q")
    assert capex.quote_loc() == ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment [USD]; "
                                 "10-Q 0001769628-26-000366 filed 2026-08-12")
    assert capex.record.raw == ('{"start":"2026-01-01","end":"2026-06-30","val":14117000000,'
                                '"accn":"0001769628-26-000366","fy":2026,"fp":"Q2","form":"10-Q","filed":"2026-08-12"}')
    assert _one(sel, "cash_usd", end).value == 5_524_000_000
    assert _one(sel, "operating_cash_flow_6m_usd", end).value == 3_663_000_000
    assert _one(sel, "contract_revenue_3m_usd", end).value == 2_575_000_000  # CoreWeave tags no us-gaap:Revenues
    assert _one(sel, "operating_lease_liability_usd", end).value == 16_319_000_000
    assert _one(sel, "finance_lease_liability_usd", end).value == 221_000_000
    pub = _one(sel, "public_date", end)
    assert (pub.value, pub.unit) == ("2026-08-12", None)
    assert not [o for o in sel.observations if "AccountsPayable" in o.record.concept or "Float" in o.record.concept]
    assert not [o for o in sel.observations if o.field.startswith("revenue_")]
    orders = [o.record.order for o in sel.observations]
    assert orders == sorted(orders)  # oldest filing first


def test_select_oracle_restatement_debt_and_cover_shares():
    sel = p.select(_cf("1341439"), since=SINCE, today=TODAY)
    assert (len(sel.observations), sel.values) == (163, 143)
    restated = _obs(sel, "operating_lease_liability_usd", date(2024, 5, 31))
    assert [(o.value, o.record.form, o.record.filed) for o in restated] == [
        (7_545_000_000, "10-K", date(2024, 6, 20)), (7_500_000_000, "10-Q", date(2025, 3, 11))]
    assert sel.observations.index(restated[0]) < sel.observations.index(restated[1])
    debt = _one(sel, "total_debt_usd", date(2026, 5, 31))
    assert (debt.value, debt.record.concept) == (129_541_000_000, "us-gaap:DebtLongtermAndShorttermCombinedAmount")
    assert _one(sel, "construction_in_progress_usd", date(2026, 8, 31)).value == 48_546_000_000
    assert _one(sel, "unrecorded_purchase_obligation_usd", date(2026, 8, 31)).value == 34_150_000_000
    assert _one(sel, "revenue_12m_usd", date(2026, 5, 31)).value == 67_357_000_000
    assert _one(sel, "contract_revenue_3m_usd", date(2026, 8, 31)).value == 19_345_000_000
    shares = _one(sel, "shares_outstanding", date(2026, 9, 7))  # cover-page date, not the period end
    assert (shares.value, shares.unit, shares.record.concept) == (
        3_023_736_000, "shares", "dei:EntityCommonStockSharesOutstanding")
    assert _one(sel, "public_date", date(2026, 9, 7)).value == "2026-09-11"
    assert _one(sel, "public_date", date(2026, 8, 31)).value == "2026-09-11"
    later = p.select(_cf("1341439"), since=date(2026, 6, 1), today=date(2026, 9, 10))  # before the 10-Q
    assert {o.valid_from for o in later.observations} == {date(2026, 6, 12)}
    assert later.skipped["future"] > 0 and later.skipped["before_since"] > 0


def test_select_ifrs_filer_and_other_currencies():
    sel = p.select(_cf("1899123"), since=SINCE, today=TODAY)
    end = date(2025, 6, 30)
    assert {o.record.form for o in sel.observations} == {"20-F", "6-K"}
    assert _one(sel, "cash_usd", end).record.concept == "ifrs-full:CashAndCashEquivalents"
    assert _one(sel, "total_debt_usd", end).value == 360_159_000  # ifrs-full:Borrowings
    assert _one(sel, "lease_liability_usd", end).value == 92_642_000
    assert _one(sel, "revenue_6m_usd", end).value == 225_710_000
    assert [o.value for o in _obs(sel, "contract_revenue_12m_usd", date(2022, 12, 31))] == [333_342_000, 330_266_000]
    nebius = p.select(_cf("1513845"), since=SINCE, today=TODAY)
    assert nebius.skipped["other_unit"] == 6 and {o.unit for o in nebius.observations} <= {"USD", None}
    assert [o.value for o in _obs(nebius, "cash_usd", date(2024, 12, 31))] == [2_449_600_000, 2_434_700_000]


def test_select_skips_stub_periods_and_keeps_restated_values():
    sel = p.select(_cf("1083301"), since=SINCE, today=TODAY)
    assert sel.skipped["stub_period"] == 3  # TeraWulf's 2021-02-08 inception periods (5, 8 and 11 months)
    assert [(o.value, o.record.form) for o in _obs(sel, "operating_cash_flow_3m_usd", date(2022, 3, 31))] == [
        (-21_509_000, "10-Q"), (-15_252_000, "10-K")]
    assert _one(sel, "operating_cash_flow_9m_usd", date(2021, 12, 31)).record.start == date(2021, 4, 1)
    assert not _obs(sel, "operating_cash_flow_12m_usd", date(2021, 12, 31))


def test_filer_name_check():
    assert store.name_matches("AMAZON COM INC", ["Amazon (AWS)"])
    assert store.name_matches("RIOT PLATFORMS, INC.", ["Riot"])
    assert store.name_matches("WHITEFIBER,     INC.", ["WhiteFiber"])
    assert store.name_matches("NEBIUS GROUP N.V.", ["Nebius"])
    assert store.name_matches("Alphabet Inc.", ["Google", "Alphabet Inc."])
    assert not store.name_matches("Alphabet Inc.", ["Google"])
    assert not store.name_matches("Apollo Origination II (UL) Capital Trust", ["Valor Compute", "Valor Compute (xAI)"])
    assert not store.name_matches("", ["BlackRock funds"])


def test_task_schedule_and_cli_are_registered():
    crons = {(name, pid): t.cron for (name, pid), t in app.periodic_registry.periodic_tasks.items()
             if name.startswith("collect.sec_xbrl")}
    assert crons == {("collect.sec_xbrl", "weekly"): sec_xbrl.WEEKLY_CRON,
                     ("collect.sec_xbrl_on_filing", "on_filing"): sec_xbrl.ON_FILING_CRON}
    assert app.tasks["collect.sec_xbrl"].queue == app.tasks["collect.sec_xbrl_on_filing"].queue == "collect"
    assert "sec_xbrl" in cli._groups["collect"]
    assert sec_xbrl.SOURCE.cadence_minutes == 7 * 24 * 60 and not sec_xbrl.SOURCE.blocked


# ---------------------------------------------------------------- a run on the test database
ROUTES = {f"{API}/CIK{int(c):010d}.json": (200, f"CIK{int(c):010d}.json")
          for c in ("1769628", "1341439", "1899123", "1513845", "1083301", "2052153")}
ROUTES[f"{API}/CIK0009999999.json"] = (404, "CIK0009999999.json.404.xml")


class CapturedSec:
    """Serves the captured responses at their real URLs; anything else fails the test."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        assert request.headers["user-agent"] == get_settings().sec_user_agent
        status, name = ROUTES[url]
        ctype = "application/xml" if name.endswith(".xml") else "application/json"
        return httpx.Response(status, content=(DATA / name).read_bytes(), headers={"content-type": ctype})


@pytest.fixture
def issuers(committed, monkeypatch):
    monkeypatch.setattr(store, "ISSUERS", ())  # Broadcom is not in the captured set
    committed.execute(
        """INSERT INTO entity (id, name, kind, cik, aliases) VALUES
             ('coreweave', 'CoreWeave', 'neocloud', '1769628', '{"CoreWeave, Inc."}'),
             ('oracle', 'Oracle', 'cloud', '1341439', '{"Oracle Corporation"}'),
             ('bitdeer', 'Bitdeer', 'company', '1899123', '{}'),
             ('nebius', 'Nebius', 'neocloud', '1513845', '{}'),
             ('terawulf-inc', 'TeraWulf Inc.', 'developer', '1083301', '{}'),
             ('valor-compute', 'Valor Compute', 'company', '2052153', '{"Valor Compute (xAI)"}'),
             ('no-xbrl', 'No XBRL Co', 'company', '0009999999', '{}'),
             ('some-fund', 'Some Fund', 'fund', '1234567', '{}'),
             ('no-cik', 'No CIK', 'company', NULL, '{}');
           INSERT INTO site (slug, name) VALUES ('site-a', 'Site A');
           INSERT INTO chain_party (site_slug, role, entity_id, label)
           VALUES ('site-a', 'tenant', 'oracle', 'Oracle')"""
    )
    return committed


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in ("document", "fact", "event")}


def _fetcher(transport) -> Fetcher:
    return Fetcher(client=httpx.Client(transport=httpx.MockTransport(transport)), backoff_base=0.01)


def test_targets_and_issuers(committed):
    committed.execute("""INSERT INTO entity (id, name, kind, cik) VALUES ('meta', 'Meta', 'cloud', '1326801'),
                           ('fund-x', 'Fund X', 'fund', '1234567'), ('bdc-x', 'BDC X', 'bdc', '7654321')""")
    from marden.core import db

    with db.transaction() as conn:
        assert store.ensure_issuers(conn) == ["broadcom"]
        assert store.ensure_issuers(conn) == ["broadcom"]  # idempotent
        found = store.targets(conn)
        assert [(t.cik, t.entity_ids) for t in found] == [("1326801", ["meta"]), ("1730168", ["broadcom"])]
        assert [t.cik for t in store.targets(conn, ["0001730168"])] == ["1730168"]
    row = committed.execute("SELECT name, kind, ticker FROM entity WHERE id = 'broadcom'").fetchone()
    assert row == {"name": "Broadcom", "kind": "chipmaker", "ticker": "AVGO"}


def test_run_writes_filed_facts_once(issuers):
    transport = CapturedSec()
    first = sec_xbrl.run(today=TODAY, fetcher=_fetcher(transport))
    assert first.ok, first.summary()
    assert (first.targets, first.documents_new, first.missing_ciks) == (7, 6, ["9999999"])
    assert first.name_mismatch == ["CIK 2052153 files as 'Apollo Origination II (UL) Capital Trust'; "
                                   "entity valor-compute"]
    assert first.facts_by_cik == {"1083301": 35, "1341439": 163, "1513845": 21, "1769628": 158, "1899123": 68}
    assert first.facts_new == 445 and _counts(issuers) == {"document": 6, "fact": 445, "event": 0}
    assert not issuers.execute("SELECT 1 FROM fact WHERE subject_id IN ('valor-compute', 'some-fund')").fetchall()
    docs = issuers.execute("SELECT DISTINCT doc_type, extract_state, source_id FROM document").fetchall()
    assert docs == [{"doc_type": "sec_companyfacts", "extract_state": "none", "source_id": "sec_xbrl"}]

    fact = issuers.execute(
        """SELECT f.*, d.url AS doc_url FROM fact f JOIN document d ON d.id = f.document_id
           WHERE subject_id = 'coreweave' AND field = 'capex_6m_usd' AND valid_from = '2026-06-30'"""
    ).fetchone()
    assert (fact["subject_type"], fact["value"], fact["unit"], fact["tag"], fact["status"], fact["producer"]) == (
        "entity", 14_117_000_000, "USD", "F", "published", "collector:sec_xbrl")
    assert fact["source_url"] == ("https://www.sec.gov/Archives/edgar/data/1769628/000176962826000366/"
                                  "0001769628-26-000366-index.htm")
    assert fact["source_name"] == "SEC XBRL: CoreWeave, Inc. 10-Q"
    assert fact["quote_loc"].startswith("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment [USD]; 10-Q")
    assert '"filed":"2026-08-12"' in fact["quote"] and fact["doc_url"] == f"{API}/CIK0001769628.json"

    current = {r["field"]: r for r in issuers.execute(
        "SELECT field, value, valid_from FROM fact_current WHERE subject_id = 'oracle'").fetchall()}
    assert (current["cash_usd"]["value"], current["cash_usd"]["valid_from"]) == (36_369_000_000, date(2026, 8, 31))
    assert (current["public_date"]["value"], current["public_date"]["valid_from"]) == ("2026-09-11", date(2026, 9, 7))
    assert current["shares_outstanding"]["value"] == 3_023_736_000
    restated = issuers.execute(
        """SELECT value FROM fact WHERE subject_id = 'oracle' AND field = 'operating_lease_liability_usd'
           AND valid_from = '2024-05-31' ORDER BY id""").fetchall()
    assert [r["value"] for r in restated] == [7_545_000_000, 7_500_000_000]  # the restated value is the newer row
    nebius = issuers.execute(
        """SELECT value, valid_from FROM fact WHERE subject_id = 'nebius' AND field = 'cash_usd'
           AND valid_from >= '2024-12-31' ORDER BY valid_from, id""").fetchall()
    assert [(r["valid_from"].isoformat(), r["value"]) for r in nebius] == [  # 20-F 2025, re-presented in 20-F 2026
        ("2024-12-31", 2_449_600_000), ("2024-12-31", 2_434_700_000), ("2025-12-31", 3_678_100_000)]
    dirty = {(r["view"], r["key"]) for r in issuers.execute("SELECT view, key FROM read_model_dirty").fetchall()}
    assert {("payer", "oracle"), ("payer_capacity", "coreweave"), ("payers", "all"), ("gpu_deployment", "all"),
            ("site_detail", "site-a")} <= dirty
    before = _counts(issuers)

    transport.requests.clear()
    second = sec_xbrl.run(today=TODAY, fetcher=_fetcher(transport))
    assert second.ok, second.summary()
    assert (second.documents_new, second.documents_unchanged, second.facts_new) == (0, 6, 0)
    assert _counts(issuers) == before and len(transport.requests) == 7
    runs = issuers.execute("SELECT ok, items_seen, items_new, meta FROM source_run ORDER BY id").fetchall()
    assert [(r["ok"], r["items_seen"], r["items_new"]) for r in runs] == [(True, 7, 5), (True, 7, 0)]
    assert runs[0]["meta"]["fetched"]["1769628"]["latest_filed"] == "2026-08-12"
    source = issuers.execute("SELECT terms_note, blocked, cadence_minutes FROM source WHERE id = 'sec_xbrl'").fetchone()
    assert "may be copied or further distributed" in source["terms_note"] and not source["blocked"]
    assert source["cadence_minutes"] == 7 * 24 * 60


def test_failed_fetch_is_recorded_on_the_source(issuers):
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"unavailable")

    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(down)), max_retries=0)
    stats = sec_xbrl.run(today=TODAY, ciks=["1769628"], fetcher=fetcher)
    assert not stats.ok and "fetch(es) failed" in (stats.error or "")
    assert _counts(issuers) == {"document": 0, "fact": 0, "event": 0}
    source = issuers.execute("SELECT last_error, last_ok_at FROM source WHERE id = 'sec_xbrl'").fetchone()
    assert "HTTP 503" in source["last_error"] and source["last_ok_at"] is None


def _filing_event(conn, accession: str, title: str, day: date) -> None:
    from marden.core import db
    from marden.core.records import EventWriter

    sources.upsert_spec(sec_index.SOURCE)
    url = f"https://www.sec.gov/Archives/edgar/data/1769628/{accession.replace('-', '')}/{accession}-index.htm"
    doc = documents.record_document("sec_index", url, f"index page {accession}".encode(), content_type="text/html",
                                    doc_type="filing_index", doc_date=day, external_id=accession)
    with db.transaction() as c:
        EventWriter(c).add("entity", "coreweave", day, "filing", title, tag="F", source_name="SEC EDGAR",
                           source_url=url, document_id=doc.id, status="published")


def test_new_10q_makes_its_cik_due_until_collected(issuers):
    from marden.core import db

    today = date(2026, 8, 20)
    _filing_event(issuers, "0001769628-26-000366", "10-Q for the period ended 2026-06-30", date(2026, 8, 12))
    _filing_event(issuers, "0001769628-26-000357", "8-K: Results of Operations", date(2026, 8, 10))  # not periodic

    def due(now: datetime | None = None, day: date = today) -> list[tuple[str, str]]:
        with db.transaction() as conn:
            return [(d.cik, d.accession) for d in store.due_filings(conn, today=day, now=now)]

    assert due() == [("1769628", "0001769628-26-000366")]
    assert sec_xbrl.due_ciks(today) == ["1769628"]
    transport = CapturedSec()
    stats = sec_xbrl.run_due(today, fetcher=_fetcher(transport))
    assert stats is not None and stats.ok and stats.mode == "on_filing"
    assert transport.requests == [f"{API}/CIK0001769628.json"] and stats.facts_new == 158
    assert due() == [] and sec_xbrl.run_due(today, fetcher=_fetcher(transport)) is None

    # an amendment the XBRL API has not picked up yet: retried after RETRY_HOURS, not on every poll
    _filing_event(issuers, "0001769628-26-000371", "10-Q/A for the period ended 2026-06-30", date(2026, 8, 19))
    assert due() == [("1769628", "0001769628-26-000371")]
    assert sec_xbrl.run_due(today, fetcher=_fetcher(transport)).facts_new == 0
    assert due() == []
    later = datetime.now(UTC) + timedelta(hours=store.RETRY_HOURS + 1)
    assert due(later) == [("1769628", "0001769628-26-000371")]
    assert due(later, day=date(2026, 8, 19) + timedelta(days=store.DUE_DAYS + 1)) == []  # retries stop
    assert not [d for d in due() if d[1] == "0001769628-26-000357"]


# ---------------------------------------------------------------- live
@pytest.mark.network
def test_live_companyfacts():
    with Fetcher() as fetcher:
        cf = p.parse_companyfacts(fetcher.get(p.companyfacts_url("1769628")).content)
    assert cf.name.startswith("CoreWeave")
    sel = p.select(cf, since=SINCE, today=datetime.now(sec_xbrl.EASTERN).date())
    fields = {o.field for o in sel.observations}
    assert {"cash_usd", "operating_cash_flow_6m_usd", "capex_6m_usd", "public_date"} <= fields
    assert max(o.valid_from for o in sel.observations if o.field == "cash_usd") >= date(2026, 6, 30)
