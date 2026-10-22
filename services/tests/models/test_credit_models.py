"""The obligation graph, contagion, payer-capacity and lease-map models on the real seeded rows
(credit_rows.copy_credit_area). Expectations are recomputed from the copied facts with SQL or plain Python
independent of the models; formula checks use tiny hand-made inputs only.
"""

from __future__ import annotations

from datetime import date

import pytest
from credit_rows import copy_credit_area, rows, shape
from ts_shapes_models import assert_matches_ts, assert_matches_types

from marden.core import db
from marden.models import contagion, graph, lease_map, obligations, payer_capacity
from marden.models.credit_inputs import notch, parse_ratings
from marden.readmodels.context import building, business_today

ORACLE_CUT = date(2026, 7, 9)  # S&P cut Oracle to BBB- (spec 24 worked example)
QUARTER_AHEAD = date(2026, 4, 1)


@pytest.fixture
def area(test_database):
    return copy_credit_area()


def _fact(subject_type: str, sid: str, field: str):
    got = rows(
        """SELECT value, tag FROM fact WHERE subject_type = %s AND subject_id = %s AND field = %s
                    AND status = 'published' ORDER BY valid_from DESC, id DESC LIMIT 1""",
        (subject_type, sid, field),
    )
    return got[0] if got else None


def test_graph_edges_schedules_and_company_pages(area):
    out = obligations.run(trigger="test")["outputs"]
    doc = out["all"]
    assert_matches_types("ObligationGraph", [shape(doc)])
    edges = {e["id"]: e for e in doc["edges"]}
    with_evidence = {
        r["subject_id"]
        for r in rows(
            "SELECT DISTINCT subject_id FROM fact WHERE subject_type = 'obligation' AND field = 'evidence' "
            "AND status = 'published'"
        )
    }
    typed = {
        r["subject_id"]
        for r in rows(
            "SELECT DISTINCT subject_id FROM fact WHERE subject_type = 'obligation' AND field = 'edge_type' "
            "AND status = 'published'"
        )
    }
    assert set(edges) == with_evidence & typed  # no edge without a document
    for eid, e in edges.items():
        total = _fact("obligation", eid, "total_usd")
        if total is not None:  # the edge's own total wins over any funding fact
            assert e["totalB"]["value"] == round(total["value"] / 1e9, 3) and e["totalB"]["tag"] == total["tag"]
        term, start = _fact("obligation", eid, "term_years"), _fact("obligation", eid, "start")
        sized = None not in (total, term, start) and _fact("obligation", eid, "annual_usd") is None
        if sized and e["type"] in graph.PAYMENT_TYPES:  # straight line: the schedule adds back to the total
            assert sum(a["amountB"]["value"] for a in e["annual"]) == pytest.approx(total["value"] / 1e9, abs=0.01)
            assert all(a["amountB"]["tag"] == "M" for a in e["annual"])
        if e["type"] not in graph.PAYMENT_TYPES:
            assert e["annual"] == []  # guarantees, warrants and stakes carry no schedule
        assert out[eid]["annual"] == e["annual"]  # the per-edge output the read model shows
    # OpenAI -> Oracle has no total_usd: OpenAI's funding fact names it ("Oracle contract", "~$300bn",
    # "~5 years; payments reported to start 2027, ~$60bn a year"), tag R as reported.
    oo = edges["openai--oracle--compute-purchase"]
    funding = [
        r
        for r in rows(
            "SELECT value, tag FROM fact WHERE subject_type = 'entity' AND subject_id = 'openai' "
            "AND field = 'funding_fact' AND status = 'published'"
        )
        if r["value"].get("label") == "Oracle contract"
    ]
    assert funding and oo["totalB"]["value"] == round(graph.usd_of_text(funding[0]["value"]["value"]) / 1e9, 3)
    assert [a["year"] for a in oo["annual"]] == [2027, 2028, 2029, 2030, 2031]
    assert {a["amountB"]["value"] for a in oo["annual"]} == {60.0} and oo["annual"][0]["amountB"]["tag"] == "R"
    total = sum(e["totalB"]["value"] or 0 for e in doc["edges"])
    assert doc["kpis"]["totalB"] == round(total, 3)

    companies = obligations.run_companies(trigger="test")["outputs"]
    assert_matches_types("CompanyObligations", [shape(companies[k]) for k in ("oracle", "openai", "coreweave")])
    oracle = companies["oracle"]
    assert {e["id"] for e in oracle["owes"]} == {e["id"] for e in doc["edges"] if e["from"] == "oracle"}
    rpo = rows("""SELECT value FROM fact WHERE subject_type = 'entity' AND subject_id = 'oracle'
                    AND field = 'remaining_performance_obligation_usd' AND status = 'published'
                  ORDER BY valid_from DESC LIMIT 1""")
    if rpo:  # OpenAI's share of Oracle's filed backlog
        share = next(c for c in oracle["concentration"] if c["payerId"] == "openai")["share"]["value"]
        assert share == round(oo["totalB"]["value"] * 1e9 / float(rpo[0]["value"]), 4)
    assert companies["openai"]["fundingGap"] == []  # no filed cash flow: no gap rows, and it says so
    assert any("operating cash flow" in m for m in companies["openai"]["missing"])
    assert "files no operating cash flow" in companies["openai"]["ndReasons"]["fundingGap"]
    # An empty list says why in plain words: Meta is owed nothing on the map, and every unscheduled contract
    # it owes is named by its counterparty (its gap is n/d, never the scheduled part read as complete).
    meta = companies["meta"]
    names = {r["id"]: r["name"] for r in rows("SELECT id, name FROM entity")}
    paying_in = [e for e in doc["edges"] if e["to"] == "meta" and e["type"] in graph.PAYMENT_TYPES]
    if not paying_in:
        assert meta["concentration"] == [] and "owes Meta" in meta["ndReasons"]["concentration"]
    unscheduled = [e for e in meta["owes"] if e["type"] in graph.PAYMENT_TYPES and e["status"] != "Terminated"
                   and not e["annual"]]
    if unscheduled:
        assert all(g["gapB"]["value"] is None for g in meta["fundingGap"])
        assert all(names[e["to"]] in meta["ndReasons"]["fundingGap"] for e in unscheduled)
        assert f"{len(unscheduled)} of the" in meta["ndReasons"]["fundingGap"]
    for o in companies.values():  # a reason only for a list with nothing known in it
        if o["concentration"]:
            assert "concentration" not in o["ndReasons"]
        if any(g["gapB"]["value"] is not None for g in o["fundingGap"]):
            assert "fundingGap" not in o["ndReasons"]


def test_contagion_walks_the_payment_edges(area):
    day = business_today()
    with db.transaction() as conn, building(day):
        out = contagion.contagion(conn, day, "openai", 50)
    assert_matches_types("ContagionResult", [shape(out)])
    hop1 = {c["id"]: c for c in out["hops"][0]["companies"]}
    assert hop1["oracle"]["lossB"] == 30.0  # half of the $60bn due in 2027 on the reported Oracle contract
    assert out["totalAtRiskB"] == round(sum(c["lossB"] for h in out["hops"] for c in h["companies"]), 3)
    assert any("openai--coreweave--compute-purchase" in m for m in out["missing"])  # unsized: never guessed


def test_walk_passes_on_only_what_a_payee_cannot_cover():
    """Formula check on two tiny edges: A pays 0 -> B loses 100 against spare cash flow 30 (OCF 80 - 50 owed),
    so B pays 1 - (100 - 30) / 50 = 0 of its own 50 to C (hop 2)."""

    class Fig:
        def __init__(self, usd: float) -> None:
            self.usd = usd

    class Fin:
        def __init__(self, ocf: float | None) -> None:
            self.ocf = ocf

        def ttm(self, base: str):
            return Fig(self.ocf) if self.ocf is not None else None

    def edge(eid: str, payer: str, payee: str, usd: float) -> graph.Edge:
        e = graph.Edge(
            {"id": eid, "from_entity_id": payer, "to_entity_id": payee, "type": "Lease", "instrument_ids": []},
            {},
            {"evidence": [], "conditions": []},
        )
        e.total, e.term, e.start = graph.Amount(usd, {}, "F"), 1.0, 2030  # type: ignore[arg-type]
        return e

    edges = [edge("a-b", "a", "b", 100.0), edge("b-c", "b", "c", 50.0)]
    hops, pay, cut, _ = contagion.walk(edges, {"b": Fin(80.0), "c": Fin(None)}, "a", 0.0, date(2030, 1, 1))  # type: ignore[arg-type]
    assert hops[0]["companies"] == [{"id": "b", "lossB": 0.0, "coverageAfter": -0.4}]
    assert pay["b"] == 0.0 and cut == {"a-b": 1.0, "b-c": 1.0}
    assert hops[1]["companies"][0]["id"] == "c"


def test_ratings_text_parses_only_named_agencies():
    row = {"valid_from": date(2026, 10, 20)}
    got = {
        r.agency: (r.rating, r.outlook)
        for r in parse_ratings("Moody's Baa2 (neg) / S&P BBB- / Fitch BBB (Oracle)", row)
    }
    assert got == {"S&P": ("BBB-", None), "Moody's": ("Baa2", "Negative"), "Fitch": ("BBB", None)}
    site = parse_ratings("Baa2 (neg) / BBB- (S&P, cut 2026-07-09) / BBB", row)
    assert {r.agency: (r.rating, r.action) for r in site} == {
        "S&P": ("BBB-", "cut 2026-07-09"),
        "Moody's": ("Baa2", None),
    }
    issuer = parse_ratings("S&P B / Fitch BB- (sr unsecured); Issuer: Moody's Ba3 CFR / S&P B+ (positive)", row)
    assert {r.agency: r.rating for r in issuer} == {"S&P": "B+", "Moody's": "Ba3"}
    assert parse_ratings("KBRA AAA (sf)", row) == []
    assert notch("BBB-") == notch("Baa3") == 9 and notch("AAA") == 0


def test_payer_capacity_leverage_band_and_backtest(area):
    out = payer_capacity.run(trigger="test")["outputs"]
    assert_matches_types("PayerCapacity", [shape(o) for o in out.values()])
    o = out["oracle"]
    b = o["basis"]
    expected = (b["debt"]["b"] + b["leaseLiabilities"]["b"]) / b["ebitda"]["b"]  # (debt + leases) / EBITDA
    assert o["leaseAdjLeverageToday"]["value"] == pytest.approx(expected, abs=0.01)
    xbrl = rows(
        """SELECT field, value FROM fact WHERE subject_type = 'entity' AND subject_id = 'oracle'
                     AND field = 'operating_lease_liability_usd' AND valid_from = %s AND status = 'published'""",
        (b["leaseLiabilities"]["asOf"],),
    )
    assert xbrl and b["leaseLiabilities"]["b"] >= round(float(xbrl[0]["value"]) / 1e9, 3)  # + finance leases
    assert {r["agency"]: r["rating"]["value"] for r in o["ratings"]}["S&P"] == "BBB-"
    assert o["downgradeRisk"]["value"] == "High" and o["downgradeRisk"]["tag"] == "M"
    assert o["triggerHeadroomTurns"]["value"] is None and any("triggers" in m for m in o["missing"])
    # spec 24 backtest: the July 2026 S&P cut must show as Watch or High at least one quarter ahead, on the
    # data public then (XBRL 40 days after the period end; no rating fact was public yet).
    before = payer_capacity.run(QUARTER_AHEAD, ["oracle"], trigger="test", mark=False)["outputs"]["oracle"]
    assert (ORACLE_CUT - QUARTER_AHEAD).days >= 90
    assert before["downgradeRisk"]["value"] in ("Watch", "High")
    assert before["ratings"] == []  # honest backtest: the rating facts are dated after it
    assert before["basis"]["ebitda"]["asOf"] <= "2026-02-20"  # the latest period public 40 days before Apr 1


def _filed(field: str, end) -> float | None:
    got = rows(
        """SELECT value FROM fact WHERE subject_type = 'entity' AND subject_id = 'oracle' AND field = %s
             AND valid_from = %s AND status = 'published' ORDER BY learned_at DESC, id DESC LIMIT 1""",
        (field, end),
    )
    return float(got[0]["value"]) if got else None


def test_payer_capacity_filed_years_carry_filed_leverage(area):
    """FY2024-26 (the last three filed years) show (debt + lease liabilities at the year end) / that year's
    EBITDA, each straight from the filed XBRL lines; projected years stay n/d while a lease is unsized, and
    the output names those leases in plain words."""
    o = payer_capacity.run(trigger="test")["outputs"]["oracle"]
    assert_matches_types("PayerCapacity", [shape(o)])
    ends = sorted(
        {r["valid_from"] for r in rows("""SELECT DISTINCT valid_from FROM fact WHERE subject_type = 'entity'
                AND subject_id = 'oracle' AND field = 'operating_cash_flow_12m_usd' AND status = 'published'""")}
    )[-3:]
    years = {y["fiscalYear"]: y for y in o["years"]}
    assert [f"FY{e.year}" for e in ends] == list(o["basis"]["filedLeverage"])
    for end in ends:
        debt = _filed("total_debt_usd", end)
        leases = sum(_filed(f, end) or 0.0 for f in ("operating_lease_liability_usd", "finance_lease_liability_usd"))
        parts = [_filed(f"{b}_12m_usd", end) for b in ("operating_income", "depreciation", "amortization_intangibles")]
        lev = years[f"FY{end.year}"]["leaseAdjLeverage"]
        assert lev["tag"] == "M" and lev["methodVersion"].startswith(payer_capacity.METHOD)
        if debt is None or None in parts:
            assert lev["value"] is None
        else:
            assert lev["value"] == round((debt + leases) / sum(parts), 2)
    assert all(years[f"FY{e.year}"]["leaseAdjLeverage"]["value"] is not None for e in ends)  # Oracle files all three
    unsized = rows("""SELECT e.name FROM obligation o JOIN entity e ON e.id = o.to_entity_id
                       WHERE o.from_entity_id = 'oracle' AND o.type = 'Lease' AND NOT EXISTS (
                         SELECT 1 FROM fact f WHERE f.subject_type = 'obligation' AND f.subject_id = o.id
                           AND f.field IN ('total_usd', 'annual_usd') AND f.status = 'published')""")
    projected = [y for label, y in years.items() if label not in o["basis"]["filedLeverage"]]
    if unsized:  # an unsized lease leaves every projected year n/d, and the reason names it
        assert projected and all(y["leaseAdjLeverage"]["value"] is None for y in projected)
        assert all(r["name"] in o["ndReasons"]["leveragePath"] for r in unsized)
        assert all(r["name"] in o["ndReasons"]["fundingGap"] for r in unsized)
    floor = o["basis"].get("fundingGap3yFloorB")
    if o["fundingGap3yB"]["value"] is None and floor and floor > 0:
        assert f"${floor:.1f}B" in o["ndReasons"]["fundingGap"]
    assert "trigger" in o["ndReasons"]["triggerHeadroom"]


def test_lease_map_ranks_by_priority(area):
    payer_capacity.run(trigger="test")
    doc = lease_map.run(trigger="test")["outputs"]["all"]
    # totals the 10-K lease notes and agency triggers would give are n/d (null), never 0 (00-global rule 3)
    assert_matches_ts("Omit<T.LeaseMapPage, 'totals'> & { totals: NDFields<T.LeaseMapPage['totals'], "
                      "'disclosedB' | 'unattributedB' | 'attributedShare' | 'triggerLeases'> }", [shape(doc)])
    assert doc["reconciliation"] == [] and doc["totals"]["disclosedB"] is None  # not collected: never estimated
    assert doc["totals"]["triggerLeases"] is None and doc["totals"]["attributedShare"] is None
    scores = [r["priorityScore"]["value"] for r in doc["leases"] if r["priorityScore"]["value"] is not None]
    assert scores == sorted(scores, reverse=True)
    for r in doc["leases"]:
        band = r["lesseeBand"]["value"]
        if r["priorityScore"]["value"] is not None:  # P(cut | band) x debt x rating link / (1 + years out)
            debt = sum(d["amountB"]["value"] or 0 for d in r["dependentDebt"])
            weight = lease_map.RATING_LINK_WEIGHT if any(d["ratingLinked"] for d in r["dependentDebt"]) else 1.0
            full = lease_map.P_CUT[band] * debt * weight
            assert any(r["priorityScore"]["value"] == pytest.approx(full / (1 + k), abs=0.002) for k in range(12))
        else:
            assert band not in lease_map.P_CUT  # no band (private lessee): no score
        assert r["triggerLease"] is False
