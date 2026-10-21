"""The stress, risk-list and capacity models on the real seeded rows (credit_rows.copy_credit_area), and the
SQL function stress_run(jsonb) the API calls. Expectations come from the copied rows and the upstream models'
stored outputs; formula checks use tiny hand-made inputs only.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import psycopg
import pytest
from conftest import TEST_URL
from credit_rows import copy_credit_area, rows, shape
from ts_shapes_models import assert_matches_types

from marden.core import db
from marden.core.jsonutil import jsonb
from marden.models import capacity, payer_capacity, risk_list, stress, stress_run
from marden.models import capacity_methods as cm
from marden.models.stress_price import Book, Shock
from marden.readmodels.context import business_today

PRICED = ["EX-JUPITER-190", "EX-SALINE-308", "EX-VANTAGE_ORCL-366", "EX-ABILENE-000", "EX-ORCL-230", "EX-CRWV-102"]


@pytest.fixture
def area(test_database):
    return copy_credit_area()


def _stored_price(iid: str, day: date) -> float | None:
    got = rows(
        """SELECT output->>'price' AS p FROM model_output WHERE model = 'price' AND subject_type = 'instrument'
                    AND subject_id = %s AND as_of = %s ORDER BY created_at DESC LIMIT 1""",
        (iid, day),
    )
    return float(got[0]["p"]) if got and got[0]["p"] is not None else None


def test_book_reprices_like_the_price_engine_and_shocks_only_today(area):
    day = business_today()
    if not rows("SELECT 1 FROM model_output WHERE model = 'price' AND as_of = %s LIMIT 1", (day,)):
        pytest.skip("the price model has not run today in the source database")
    with db.transaction() as conn:
        book = Book.build(conn, day)
        same = {iid: book.reprice(iid, Shock()) for iid in PRICED}
        oracle = {iid: book.reprice(iid, Shock(hazard_mult={"oracle": 1.5})) for iid in PRICED}
        later = book.reprice("EX-JUPITER-190", Shock(completion_months={"jupiter": 6}))
    for iid, r in same.items():
        assert r["now"] == _stored_price(iid, day)  # the engine's own price, recomputed in process
        assert r["after"] == r["now"]  # no shock, no change
    for iid in ("EX-JUPITER-190", "EX-SALINE-308", "EX-ORCL-230"):  # Oracle-backed: a riskier Oracle prices lower
        assert oracle[iid]["after"] < oracle[iid]["now"]
    assert oracle["EX-CRWV-102"]["after"] == oracle["EX-CRWV-102"]["now"]  # CoreWeave's notes do not move
    assert later["after"] < later["now"]  # a later completion keeps the completion risk longer


def test_saved_scenarios_and_their_outputs(area):
    day = business_today()
    if not rows("SELECT 1 FROM model_output WHERE model = 'price' AND as_of = %s LIMIT 1", (day,)):
        pytest.skip("the price model has not run today in the source database")
    payer_capacity.run(trigger="test")
    result = stress_run.run(trigger="test")
    out = result["outputs"]
    base = [out[f"{s}:base"] for s in ("end_user_funding_miss", "tenant_downgrade", "power_delay")]
    assert_matches_types("StressResult", [shape(o) for o in base])
    assert_matches_types("ContagionResult", [shape(out[f"contagion:{c}:base"]) for c in stress.CONTAGION_COMPANIES])
    td = out["tenant_downgrade:base"]
    # the multiplier is exp(beta x notches) with beta fitted on the priced issuers' intensities and ratings
    pts = td["basis"]["points"]
    assert len({p["notch"] for p in pts}) >= 2 and td["basis"]["hazardMultiplier"] > 1
    assert td["basis"]["ratingNow"] == "BBB-" and td["request"]["params"] == {"tenant": "oracle", "rating": "BB+"}
    sized = [r for r in td["rows"] if not r["instrumentId"].startswith("notes:") and r["sizeB"]["value"]]
    lost = sum(r["sizeB"]["value"] * (r["priceNow"] - r["priceAfter"]) / 100 for r in sized)
    assert td["kpis"]["valueLostB"] == pytest.approx(lost, abs=0.002)
    assert td["kpis"]["exposedB"] == pytest.approx(sum(r["sizeB"]["value"] for r in sized), abs=0.002)
    assert td["rows"][-1]["instrumentId"] == "notes:oracle"  # the tenant's own notes: the shaded last row
    for r in td["rows"]:
        if not r["instrumentId"].startswith("notes:"):
            assert r["priceNow"] == _stored_price(r["instrumentId"], day)
    severe = out["tenant_downgrade:severe"]
    if not rows("SELECT 1 FROM fact WHERE subject_type = 'series' AND subject_id LIKE 'ice_bofa_%%' LIMIT 1"):
        assert severe["kpis"]["valueLostB"] is None and any("2022 peak" in m for m in severe["missing"])
    pd = out["power_delay:base"]
    for slug, site in pd["basis"]["sites"].items():  # the base cushion is the stored cushion run's
        stored = rows(
            """SELECT output->'projection'->'cushionMonths'->>'value' AS c FROM model_output
                          WHERE model = 'cushion' AND subject_id = %s AND as_of = %s""",
            (slug, day),
        )
        if stored and site["cushion"] is not None and stored[0]["c"] is not None:
            assert site["cushion"][0] == float(stored[0]["c"])
            assert site["cushion"][1] <= site["cushion"][0]  # a later power date never adds cushion
    inst = out["EX-JUPITER-190"]  # InstrumentDetail.stress rows
    assert {r["scenario"] for r in inst["rows"]} and all(not math.isnan(r["change"]) for r in inst["rows"])
    assert out["oracle"]["stressSummary"]["siteDebtLossB"] == td["kpis"]["valueLostB"]


def test_stress_run_function_contract(area):
    """stress_run(jsonb): a saved scenario at once, a custom one queued once and served once stored."""
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO model_output (model, version, subject_type, subject_id, as_of, output)
                        VALUES ('stress', '0.1', 'scenario', 'power_delay:base', %s, %s)""",
            (
                business_today(),
                jsonb(
                    {
                        "kpis": {},
                        "flow": [],
                        "rows": [],
                        "request": {"scenario": "power_delay", "params": {"months": 6}, "severity": "base"},
                    }
                ),
            ),
        )

        def call(req: dict) -> dict:
            return conn.execute("SELECT stress_run(%s) AS r", (jsonb(req),)).fetchone()[0]

        hit = call({"scenario": "power_delay", "params": {"months": 6}, "severity": "base"})
        assert hit["status"] == "done" and hit["subject"] == "power_delay:base"
        assert call({"scenario": "power_delay", "params": {}, "severity": "base"})["status"] == "done"
        custom = {"scenario": "power_delay", "params": {"months": 9}, "severity": "base"}
        first, again = call(custom), call(custom)
        assert first["status"] == "queued" and again["jobId"] == first["jobId"]  # one job for both callers
        job = conn.execute(
            "SELECT task_name, queue_name, args FROM procrastinate_jobs WHERE id = %s", (first["jobId"],)
        ).fetchone()
        assert job[0] == "models.stress_custom" and job[1] == "models" and job[2]["key"] == first["subject"]
        conn.execute(
            """INSERT INTO model_output (model, version, subject_type, subject_id, as_of, output)
                        VALUES ('stress', '0.1', 'scenario', %s, %s, '{"rows": []}')""",
            (first["subject"], business_today()),
        )
        assert call(custom)["status"] == "done"
        assert call({"scenario": "meteor", "params": {}, "severity": "base"})["status"] == "error"


def test_risk_list_ranks_by_odds_and_freezes(area):
    day = business_today()
    if not rows("SELECT 1 FROM model_output WHERE model = 'price' AND as_of <= %s LIMIT 1", (day,)):
        pytest.skip("no price model output in the source database")
    payer_capacity.run(trigger="test")
    result = risk_list.run(trigger="test")
    week = risk_list.week_of(day).isoformat()
    doc = result["outputs"][week]
    assert_matches_types("RiskList", [shape(doc)])
    odds = [r["odds"]["value"] for r in doc["rows"]]
    assert odds == sorted(odds, reverse=True) and all(r["odds"]["methodVersion"] == "odds v0" for r in doc["rows"])
    for r in doc["rows"]:
        pts = doc["basis"]["points"][r["instrumentId"]]
        assert r["odds"]["value"] == round(pts / (pts + risk_list.ODDS_K), 3)
        stored = rows(
            """SELECT output->>'price' AS p FROM model_output WHERE model = 'price' AND subject_id = %s
                  AND as_of <= %s ORDER BY as_of DESC, created_at DESC LIMIT 1""",
            (r["instrumentId"], day),
        )
        assert r["price"]["value"] == pytest.approx(float(stored[0]["p"]), abs=0.001)
    for para, row in zip(doc["note"]["paragraphs"], doc["rows"], strict=False):  # code templates, own numbers
        assert para["lead"] == row["name"] and f"{round(row['odds']['value'] * 100)}%" in para["text"]
        assert all(e["text"] in para["text"] for e in row["why"][:3])
    assert risk_list.run(trigger="test").get("frozen") is True  # published: frozen
    assert risk_list.week_of(date(2026, 10, 25)) == date(2026, 10, 26)  # a Sunday run publishes Monday's list
    assert risk_list.week_of(date(2026, 10, 21)) == date(2026, 10, 19)


def test_capacity_estimates(area):
    out = capacity.run(trigger="test")["outputs"]
    campuses = [o for o in out.values() if o["scope"] == "campus"]
    assert_matches_types("CapacityEstimate", [shape(o) for o in campuses[:40]])
    v1 = {
        r["campus_id"]
        for r in rows("SELECT sl.campus_id FROM site_link sl JOIN site s ON s.slug = sl.site_slug WHERE s.in_v1")
    }
    for cid in v1:
        o = out[cid]
        f, it = o["facilityMw"], o["itMw"]
        assert (f["base"] is not None and f["low"] <= f["base"] <= f["high"]) or o["missing"]
        if f["base"] is not None and o["pue"]["value"]:
            assert it["base"] == round(f["base"] / o["pue"]["value"])
        known = [m for m in o["methods"] if m["facilityMw"]["base"] is not None]
        assert len(known) >= 2 or o["missing"]  # spec 27: two methods, or says what is missing
    abilene = out["stargate-abilene"]
    chips = rows(
        "SELECT value FROM fact WHERE subject_type = 'campus' AND subject_id = 'stargate-abilene' "
        "AND field = 'chips' AND status = 'published' ORDER BY valid_from DESC, id DESC LIMIT 1"
    )
    if chips:  # the disclosed chips decide the generation (B200 is the larger count)
        top = max(chips[0]["value"], key=lambda k: chips[0]["value"][k])
        assert abilene["gpuGeneration"]["value"] == top and abilene["gpuGeneration"]["tag"] == "R"


def test_capacity_combine_rules():
    def m(name: str, lo: float, hi: float, tag: str = "R") -> cm.Method:
        return cm.Method(name, lo, (lo + hi) / 2, hi, "", [{"tag": tag}])  # type: ignore[list-item]

    filed = m("Filed", 300, 300, tag="F")
    best, disagree, conf = cm.combine([filed, m("Utility load", 500, 500)])
    assert best is filed and disagree and conf == "High"  # a filing is the answer; the rest are checks
    best, disagree, conf = cm.combine([m("Utility load", 280, 320), m("Prime generation", 300, 360)])
    assert (best.low, best.high) == (300, 320) and not disagree and conf == "Medium"  # the overlap
    best, disagree, conf = cm.combine([m("Filed", 2450, 2450), m("Prime generation", 4850, 4850)])
    assert (best.low, best.base, best.high) == (2450, 2450, 4850) and disagree and conf == "Low"
    assert cm.mw_of("Up to 2.45 GW") == 2450 and cm.mw_of({"mw": 168}) == 168


def test_week_of_mondays():
    for d in range(14):
        day = date(2026, 10, 12) + timedelta(days=d)
        assert risk_list.week_of(day).weekday() == 0
