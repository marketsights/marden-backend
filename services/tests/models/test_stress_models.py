"""The stress, risk-list and capacity models on the real seeded rows (credit_rows.copy_credit_area), and the
SQL function stress_run(jsonb) the API calls. Expectations come from the copied rows and the upstream models'
stored outputs; formula checks use tiny hand-made inputs only.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import psycopg
import pytest
from conftest import TEST_URL
from credit_rows import copy_credit_area, rows, shape
from ts_shapes_models import assert_matches_ts, assert_matches_types

from marden.core import db
from marden.core.jsonutil import jsonb
from marden.models import capacity, payer_capacity, risk_list, risk_note, stress, stress_grid, stress_run, stress_walk
from marden.models import capacity_methods as cm
from marden.models.stress_price import Book, Shock
from marden.models.stress_scenarios import DEFAULTS
from marden.readmodels.context import business_today

PRICED = ["EX-JUPITER-190", "EX-SALINE-308", "EX-VANTAGE_ORCL-366", "EX-ABILENE-000", "EX-ORCL-230", "EX-CRWV-102"]


def stress_shape(doc: dict) -> dict:
    """The StressResult piece: bookkeeping removed, and each row's `walk` (the waterfall behind its price)."""
    out = shape(doc)
    return {**out, "rows": [{k: v for k, v in r.items() if k != "walk"} for r in out["rows"]]}


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
    assert_matches_types("StressResult", [stress_shape(o) for o in base])
    walks = [out[f"contagion:{c}:base"] for c in stress.CONTAGION_COMPANIES]
    # totalAtRiskB is n/d (null, never 0) when no edge on the walk has a payment schedule
    assert_matches_ts("NDFields<T.ContagionResult, 'totalAtRiskB'>", [shape(o) for o in walks])
    for o in walks:
        unscheduled = any("has no annual schedule" in m for m in o["missing"])
        assert o["totalAtRiskB"] is not None or unscheduled
        assert o["totalAtRiskB"] != 0 or not unscheduled
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
    # Result text names parties as screens do (entity names), never by slug.
    names = {r["id"]: r["name"] for r in rows("SELECT id, name FROM entity WHERE id IN ('oracle', 'openai')")}
    assert td["basis"]["label"] == f"{names['oracle']} cut to BB+"
    assert out["oracle"]["stressSummary"]["scenario"] == f"{names['oracle']} cut to BB+ (base)"
    assert all(r["why"] == f"Tenant {names['oracle']} cut to BB+" for r in td["rows"] if r.get("ruleCode") == "T1")
    assert td["rows"][-1]["name"].startswith(f"{names['oracle']} notes (")
    eu = out["end_user_funding_miss:base"]
    assert eu["basis"]["label"] == f"{names['openai']} cuts 30% of its contracts"
    assert all("openai" not in r["why"] and "oracle" not in r["why"] for o in base for r in o["rows"])
    assert all("_" not in r["why"] for r in out["power_delay:base"]["rows"])  # milestone names, not keys


def test_chosen_instruments_reads_a_list_or_text():
    assert stress.chosen_instruments(["EX-2", "EX-1", "EX-1", " ", ""]) == ["EX-1", "EX-2"]
    assert stress.chosen_instruments("EX-2, EX-1,") == ["EX-1", "EX-2"]  # the screen's URL form
    assert stress.chosen_instruments(None) == [] and stress.chosen_instruments([]) == []


def test_scenario_applied_to_chosen_instruments(area):
    """params.instruments: the same shock re-prices exactly the chosen instruments (each one row, the same prices as
    the whole-scenario run), kpis over those rows only; an id the engine cannot price is listed, never shown."""
    day = business_today()
    if not rows("SELECT 1 FROM model_output WHERE model = 'price' AND as_of = %s LIMIT 1", (day,)):
        pytest.skip("the price model has not run today in the source database")
    base = {"scenario": "tenant_downgrade", "params": {"tenant": "oracle", "rating": "BB+"}, "severity": "base"}
    chosen = ["EX-SALINE-308", "EX-CRWV-102", "EX-ORCL-230", "NOT-AN-INSTRUMENT"]
    with db.transaction() as conn:
        book = Book.build(conn, day)
        whole = stress.run_request(conn, book, base)
        out = stress.run_request(conn, book, {**base, "params": {**base["params"], "instruments": chosen}})
    assert_matches_types("StressResult", [stress_shape(out)])
    assert out["request"]["params"] == {"tenant": "oracle", "rating": "BB+", "instruments": sorted(chosen)}
    by = {r["instrumentId"]: r for r in out["rows"]}
    assert set(by) <= {"EX-SALINE-308", "EX-CRWV-102", "EX-ORCL-230"} and "EX-SALINE-308" in by
    assert any("NOT-AN-INSTRUMENT" in m for m in out["missing"])
    site_row = next(r for r in whole["rows"] if r["instrumentId"] == "EX-SALINE-308")
    assert (by["EX-SALINE-308"]["priceNow"], by["EX-SALINE-308"]["priceAfter"]) == (
        site_row["priceNow"],
        site_row["priceAfter"],
    )
    assert by["EX-SALINE-308"]["ruleCode"] == "T1" and by["EX-SALINE-308"]["why"] == site_row["why"]
    if "EX-CRWV-102" in by:  # CoreWeave's notes are not on an Oracle chain: priced, not moved
        assert by["EX-CRWV-102"]["priceAfter"] == by["EX-CRWV-102"]["priceNow"]
        assert by["EX-CRWV-102"]["why"].startswith("Not reached")
    if "EX-ORCL-230" in by:  # the tenant's own notes: one row each, priced lower
        assert by["EX-ORCL-230"]["priceAfter"] < by["EX-ORCL-230"]["priceNow"]
    sized = [r for r in out["rows"] if r["sizeB"]["value"]]
    lost = sum(r["sizeB"]["value"] * (r["priceNow"] - r["priceAfter"]) / 100 for r in sized)
    assert out["kpis"]["valueLostB"] == pytest.approx(lost, abs=0.002)
    assert out["kpis"]["exposedB"] == pytest.approx(sum(r["sizeB"]["value"] for r in sized), abs=0.002)
    assert "tenantNotesLossB" not in out["kpis"] and not any(
        r["instrumentId"].startswith("notes:") for r in out["rows"]
    )
    saline = next(f for f in whole["flow"] if f["debtB"] == out["flow"][0]["debtB"])
    assert len(out["flow"]) == 1 and out["flow"][0] == saline  # only the chosen instruments' hit sites


def test_end_user_walk_prices_the_rent_it_pays_on_recovery_or_a_filed_guarantee(area):
    """A 100% walk: the debt on the sites the end user pays rent for is priced on re-lease or recovery (stress_walk),
    never left near par; a facility with a filed guarantee shows it; the waterfall adds up to the price after."""
    day = business_today()
    if not rows("SELECT 1 FROM model_output WHERE model = 'price' AND as_of = %s LIMIT 1", (day,)):
        pytest.skip("the price model has not run today in the source database")
    req = {"scenario": "end_user_funding_miss", "params": {"endUser": "openai", "cutPct": 100}, "severity": "base"}
    with db.transaction() as conn:
        book = Book.build(conn, day)
        out = stress.run_request(conn, book, req)
        half = stress.run_request(conn, book, {**req, "params": {"endUser": "openai", "cutPct": 50}})
        assert book.now_state("EX-SALINE-308") is book.now_state("EX-SALINE-308")  # computed once per book
    walked = [r for r in out["rows"] if "walk" in r]
    assert walked and out["basis"]["walk"]["sharePct"] == 100
    odds = stress_walk.RELEASE_ODDS
    for r in walked:
        w = r["walk"]
        assert {w[k]["tag"] for k in ("walkDate", "reLeaseOdds", "reLeaseMonths", "rentHaircutPct")} == {"M"}
        assert r["priceAfter"] == pytest.approx(w["walkCents"], abs=0.05)  # all of the rent stops
        blend = odds * w["reLeaseCents"] + (1 - odds) * w["noReLeaseCents"]
        assert w["walkCents"] == pytest.approx(min(blend, r["priceNow"]), abs=0.1)
        assert w["reLeaseCents"] >= w["noReLeaseCents"]  # a new tenant never leaves lenders worse than the collateral
        if "guarantee" not in w:
            rec = w["recovery"]["afterWorkoutCents"] + (w.get("reserve") or {}).get("cents", 0)
            assert w["recovery"]["afterWorkoutCents"] < w["recovery"]["cents"]  # paid after the workout
            assert w["noReLeaseCents"] == pytest.approx(min(rec, 100), abs=0.1)
            # single-tenant debt lands between its recovery and the re-lease value, and never above today's price
            assert r["priceAfter"] <= r["priceNow"]
            assert min(min(rec, 100), r["priceNow"]) - 0.1 <= r["priceAfter"] <= max(w["reLeaseCents"], rec) + 0.1
        else:
            assert w["guarantee"]["tag"] == "F" and w["guarantee"]["sourceName"]
    ddtl = next((r for r in walked if r["instrumentId"] == "EX-CRWV-094"), None)
    if ddtl is not None:  # OpenAI is its only payer: it moves, and the filed CoreWeave guarantee is shown
        assert ddtl["priceAfter"] < ddtl["priceNow"] and "guarantee" in ddtl["walk"]
    by_half = {r["instrumentId"]: r for r in half["rows"]}
    for r in walked:  # half the rent stops: half way between the other effects and the walk value
        h = by_half[r["instrumentId"]]["walk"]
        assert h["priceAfter"] == pytest.approx(0.5 * h["otherShareCents"] + 0.5 * h["walkCents"], abs=0.05)


def test_stress_grid_covers_the_screen_and_is_served_by_stress_run(area):
    day = business_today()
    if not rows("SELECT 1 FROM model_output WHERE model = 'price' AND as_of = %s LIMIT 1", (day,)):
        pytest.skip("the price model has not run today in the source database")
    with db.transaction() as conn:
        reqs = stress_grid.grid_requests(conn)
        assert not any(r["params"] == DEFAULTS[r["scenario"]] for r in reqs)
        assert {"scenario": "power_delay", "params": {"months": 17}, "severity": "severe"} in reqs
        one = {"scenario": "power_delay", "params": {"months": 9}, "severity": "base"}
        subject = stress_grid.subject_of(conn, one)
    assert len(reqs) == len({json.dumps(r, sort_keys=True) for r in reqs})
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        queued = conn.execute("SELECT stress_run(%s) AS r", (jsonb(one),)).fetchone()[0]
    assert queued["subject"] == subject  # the grid keys outputs exactly as stress_run looks them up
    with db.transaction() as conn:
        book = Book.build(conn, day)
        out = stress.run_request(conn, book, one)
        conn.execute(
            """INSERT INTO model_output (model, version, subject_type, subject_id, as_of, output)
                        VALUES ('stress', %s, 'scenario', %s, %s, %s)""",
            (stress.VERSION, subject, day, jsonb(out)),
        )
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        served = conn.execute("SELECT stress_run(%s) AS r", (jsonb(one),)).fetchone()[0]
    assert served["status"] == "done" and served["rows"] == jsonb_roundtrip(out["rows"])


def jsonb_roundtrip(value):
    return json.loads(json.dumps(value))


def test_risk_note_rounds_odds_half_up():
    assert [risk_note.percent(x) for x in (0.625, 0.455, 0.571, 0.5, 0.0, 0.995)] == [63, 46, 57, 50, 0, 100]
    para = risk_note.note(date(2026, 10, 19), [{"rank": 1, "name": "Jupiter loans", "instrumentId": "X",
                                               "odds": {"value": 0.625}}], {})["paragraphs"][0]
    assert para["text"].startswith("Ranked 1 of 1, with 63% odds of a 3-point move")
    ev = risk_note.band_reason("oracle", "High", [], date(2026, 10, 21), "Oracle")
    assert ev["text"].startswith("Tenant Oracle downgrade risk High")
    assert ev["_ref"] == "model:payer_capacity@entity:oracle:2026-10-21"  # the reference keeps the id


def test_risk_note_names_link_parties_in_words():
    """A tripped link reads 'Oracle (tenant, T2)', the role in words; a party with no name on file says so
    ('Lenders (not named, L2)'), never the bare 'n/d' as if it were a name."""
    fact = {"value": "Marden spread loss 6.6 points", "tag": "M", "asOf": "2026-10-21"}
    named = risk_note.status_reason({"party": "Oracle", "role": "tenant", "ruleCode": "T2", "fact": fact}, "jupiter",
                                    date(2026, 10, 21))
    assert named is not None and named["text"] == "Oracle (tenant, T2): Marden spread loss 6.6 points"
    for party in ("n/d", "—", "", None):
        ev = risk_note.status_reason({"party": party, "role": "lenders", "ruleCode": "L2", "fact": fact}, "helios",
                                     date(2026, 10, 21))
        assert ev is not None and ev["text"] == "Lenders (not named, L2): Marden spread loss 6.6 points"
    eu = risk_note.status_reason({"party": "OpenAI", "role": "end_user", "ruleCode": "E1",
                                  "reason": "E1 Funding at risk. More text"}, "abilene", date(2026, 10, 21))
    assert eu is not None and eu["text"] == "OpenAI (end user, E1): E1 Funding at risk"


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
        assert job[0] == "models.stress_custom" and job[1] == "stress" and job[2]["key"] == first["subject"]
        conn.execute(
            """INSERT INTO model_output (model, version, subject_type, subject_id, as_of, output)
                        VALUES ('stress', '0.1', 'scenario', %s, %s, '{"rows": []}')""",
            (first["subject"], business_today()),
        )
        assert call(custom)["status"] == "done"
        chosen = {
            "scenario": "power_delay",
            "params": {"months": 6, "instruments": ["EX-JUPITER-190"]},
            "severity": "base",
        }
        assert call(chosen)["status"] == "queued"  # limited to chosen instruments: a custom run, not the saved one
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
        # half up to a whole percent, as the screen rounds it (0.625 -> 63%, never round()'s 62%)
        assert para["lead"] == row["name"] and f"{math.floor(row['odds']['value'] * 100 + 0.5)}%" in para["text"]
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
