"""The GPU models (gpu_index, gpu_model, gpu_collateral, gpu_deployment) and `recovery` on real seeded rows
(model_rows.copy_model_rows): the archive's provider list prices, chips, resale and residual points, useful
lives, GPU-loan issue comps, the UST curve, the CoreWeave / IREN / xAI GPU facilities, Abilene and Lake
Mariner site debt and a CoreWeave unsecured note. Expectations are recomputed from the copied rows; tiny
hand-made numbers appear only in the formula checks.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections import defaultdict
from datetime import date
from typing import Any

import pytest
from model_rows import copy_model_rows, rows
from scipy.integrate import quad

from marden.models import gpu_collateral, gpu_deployment, gpu_index, recovery
from marden.models.gpu_facilities import range_text
from marden.models.gpu_inputs import chip_key, provider_name, ship_date
from marden.models.gpu_value import HAIRCUT, annuity
from marden.models.recovery_shell import rank_of

DDTL1, DDTL2, DDTL3, DDTL55 = "EX-CRWV-091", "EX-CRWV-092", "EX-CRWV-094", "EX-CRWV-097"
IREN_DDTL, IREN_NOTES, XAI1 = "EX-IREN-184", "EX-IREN-185", "EX-XAI_VCI-388"
ABILENE1, ABILENE2, WULF, NOTE = "EX-ABILENE-000", "EX-ABILENE-001", "EX-WULF-369", "EX-CRWV-102"
GPU_IDS = [DDTL1, DDTL2, DDTL3, DDTL55, IREN_DDTL, IREN_NOTES, XAI1]
IDS = [*GPU_IDS, ABILENE1, ABILENE2, WULF, NOTE]
EXTRAS = ("missing", "basis")


@pytest.fixture
def copied(test_database):
    return copy_model_rows(IDS)


def _typed(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in EXTRAS}


def _list_prices() -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for r in rows("""SELECT subject_id, valid_from, value FROM fact WHERE subject_type = 'series'
                      AND subject_id LIKE 'gpu_list_price:%%' AND field = 'usd_per_gpu_hr' AND status = 'published'"""):
        _, provider, model = r["subject_id"].split(":", 2)
        out[(r["valid_from"].isoformat(), chip_key(model) or model)][provider] = float(r["value"])
    return {k: sorted(v.values()) for k, v in out.items()}


def test_index_is_the_median_of_the_providers(copied):
    from ts_shapes_models import assert_matches_types

    out = gpu_index.run_index(trigger="test")["outputs"]["all"]
    expected = _list_prices()
    assert len(out["index"]) == len(expected) > 0
    for p in out["index"]:
        prices = expected[(p["date"], p["model"])]
        assert p["medianUsdHr"] == pytest.approx(statistics.median(prices), abs=1e-4)
        assert p["nProviders"] == len(prices) and p["p25"] <= p["medianUsdHr"] <= p["p75"]
    assert out["decline"]["annualDecline"] > 0 and len(out["decline"]["models"]) >= 3
    assert_matches_types("GpuPricePoint", [out["index"]], element=True)
    assert_matches_types("GpuProviderPrice", [out["providers"]], element=True)


def test_model_page_values_fall_with_age_and_new_units_are_worth_their_price(copied):
    from ts_shapes_models import assert_matches_types

    out = gpu_index.run_models(trigger="test")["outputs"]
    h100 = out["H100"]
    usd = [p["usd"]["value"] for p in h100["valueByAgeYears"]]
    assert usd and all(a > b for a, b in itertools.pairwise(usd)) and usd[-1] == 0
    assert all(p["usd"]["tag"] == "M" and "model v0" in p["usd"]["methodVersion"] for p in h100["valueByAgeYears"])
    implied = h100["basis"]["decline"]["impliedByModel"]
    assert h100["assumptions"]["utilization"] == pytest.approx(statistics.median(implied.values()), abs=1e-4)
    median_model = min(implied, key=lambda m: abs(implied[m] - h100["assumptions"]["utilization"]))
    page = out[median_model]  # the model whose implied share is the median is valued at its new price at launch
    assert page["valueByAgeYears"][0]["usd"]["value"] == pytest.approx(page["basis"]["newPriceUsd"], rel=0.01)
    lives = rows("""SELECT DISTINCT ON (subject_id) value FROM fact WHERE subject_type = 'entity'
                     AND field = 'gpu_useful_life_years' AND jsonb_typeof(value) = 'number'
                     ORDER BY subject_id, tag, valid_from DESC""")
    assert h100["assumptions"]["usefulLifeYears"]["value"] == statistics.median(float(r["value"]) for r in lives)
    assert {f["instrumentId"] for f in h100["facilities"]} >= {DDTL1, DDTL2}
    assert h100["resale"] and h100["basis"]["resaleCheck"]["resaleUsd"] == h100["resale"][-1]["usd"]
    assert_matches_types("GpuModelDetail", [_typed(out[m]) for m in ("H100", "GB300")])


def test_backtest_before_any_captured_price_is_empty_and_says_so(copied):
    day = date(2026, 10, 1)
    index = gpu_index.run_index(day, trigger="test")["outputs"]["all"]
    assert index["index"] == [] and index["providers"] == [] and index["missing"]
    page = gpu_index.run_models(day, ["H100"], trigger="test")["outputs"]["H100"]
    assert page["valueByAgeYears"] == [] and page["missing"]
    # the facility's GPU model and collateral record were first published on 2026-10-20: not a known GPU
    # facility on Oct 1, so no collateral output that day
    assert DDTL1 not in gpu_collateral.run(day, [DDTL1], trigger="test")["outputs"]
    late = gpu_collateral.run(date(2026, 10, 20), [DDTL1], trigger="test")["outputs"][DDTL1]
    assert late["valueTodayB"]["value"] is not None and late["units"]["value"] == 43000


def test_collateral_value_ltv_and_recovery(copied):
    from ts_shapes_models import assert_matches_types

    out = gpu_collateral.run(trigger="test")["outputs"]
    assert set(GPU_IDS) <= set(out)
    drawn = {r["subject_id"]: float(r["value"]) for r in rows(
        "SELECT DISTINCT ON (subject_id) subject_id, value FROM fact WHERE field = 'drawn_usd' "
        "AND status = 'published' ORDER BY subject_id, valid_from DESC")}
    d1 = out[DDTL1]
    assert d1["balanceB"]["value"] == pytest.approx(drawn[DDTL1] / 1e9) and d1["balanceB"]["tag"] == "F"
    assert d1["ltvToday"]["value"] == pytest.approx(d1["balanceB"]["value"] / d1["valueTodayB"]["value"], rel=1e-3)
    low, _, high = d1["recoveryIfCustomerWalks"]["value"].partition("\u2013")
    assert 0 <= int(low) <= int(high or low) <= 100
    record = rows("SELECT value FROM fact WHERE subject_type = 'instrument' AND subject_id = %s "
                  "AND field = 'gpu_collateral'", (DDTL55,))[0]["value"]
    scaled = record["units"]["base"] * drawn[DDTL55] / record["balance_usd"]
    assert out[DDTL55]["units"]["value"] == round(scaled) and out[DDTL55]["basis"]["unitsScaledToDrawn"]
    assert out[IREN_DDTL]["ltvToday"] == out[IREN_NOTES]["ltvToday"]  # one security package, pari passu
    assert out[IREN_DDTL]["basis"]["pool"] == [IREN_DDTL, IREN_NOTES]
    assert out[DDTL2]["basis"]["valuedModel"] in ("H100", "H200")  # mixed pool: the lower-valued model
    assert_matches_types("GpuCollateral", [_typed(out[i]) for i in GPU_IDS])


def test_deployment_is_nd_without_deliveries_and_ties_cip_to_xbrl(copied):
    from ts_shapes_models import assert_matches_types

    out = gpu_deployment.run(trigger="test")["outputs"]["all"]
    assert {f["id"] for f in out["facilities"]} >= set(GPU_IDS)
    for f in out["facilities"]:
        assert f["status"] == "n/d" and f["unitsDelivered"]["value"] is None
        assert any("units delivered" in m for m in f["missing"])
    facts = rows("""SELECT e.name, f.valid_from, f.field, f.value, f.quote_loc FROM fact f
                      JOIN entity e ON e.id = f.subject_id WHERE f.subject_type = 'entity'
                       AND f.field IN ('construction_in_progress_usd', 'ppe_net_usd')""")
    by: dict[tuple[str, str, str], float] = {}
    filed: dict[tuple[str, str, str], date] = {}
    for r in facts:  # the newest filing's value for each period (restatements replace originals)
        key = (r["name"], r["valid_from"].strftime("%Y-%m"), r["field"])
        when = gpu_deployment.filed_on(r) or date.min
        if key not in by or when > filed[key]:
            by[key], filed[key] = float(r["value"]), when
    checked = 0
    for c in out["companies"]:
        q, yy = int(c["quarter"][1]), 2000 + int(c["quarter"][-2:])
        match = [k for k in by if k[0] == c["company"] and k[2] == "construction_in_progress_usd"
                 and int(k[1][:4]) == yy and (int(k[1][5:]) - 1) // 3 + 1 == q]
        assert match and c["cipB"]["value"] == pytest.approx(by[match[0]] / 1e9, abs=1e-4) and c["cipB"]["tag"] == "F"
        ppe = by.get((match[0][0], match[0][1], "ppe_net_usd"))
        if ppe is not None:
            assert c["ppeInServiceB"]["value"] == pytest.approx((ppe - by[match[0]]) / 1e9, abs=1e-4)
            checked += 1
    assert checked >= 5
    assert_matches_types("GpuDeploymentPage", [_typed(out)])


def test_recovery_by_collateral_type(copied):
    coll = gpu_collateral.run(trigger="test")["outputs"]
    out = recovery.run(instruments=[*GPU_IDS, ABILENE1, ABILENE2, WULF, NOTE], trigger="test")["outputs"]
    for iid in GPU_IDS:
        assert out[iid]["method"] == "gpu_collateral"
        assert out[iid]["recovery"]["value"] == coll[iid]["recoveryIfCustomerWalks"]["value"]
    ab = out[ABILENE1]
    comps = ab["basis"]["comps"]
    assert len(comps) >= 3 and all(c["site"] for c in comps)
    gross = float(rows("SELECT value FROM fact WHERE subject_type = 'site' AND subject_id = 'abilene' "
                       "AND field = 'gross_mw' AND tag IN ('F', 'R') ORDER BY valid_from DESC LIMIT 1")[0]["value"])
    pct = float(rows("SELECT value FROM fact WHERE subject_type = 'site' AND subject_id = 'abilene' "
                     "AND field = 'pct_complete' AND tag IN ('F', 'R') ORDER BY valid_from DESC LIMIT 1")[0]["value"])
    median = statistics.median(c["usdPerGrossMw"] for c in comps)
    # v0.2: the energized share at comp value, the unfinished rest less the haircut, plus unspent proceeds
    b = ab["basis"]
    share, e, cut = pct / 100, b["energizedShare"], b["unfinishedHaircut"]
    works = gross * (e + (share - e) * (1 - cut)) * median
    assert ab["valueB"]["value"] == pytest.approx((works + (b["unspentProceedsUsd"] or 0)) / 1e9, rel=1e-3)
    assert ab["basis"]["pariClaimsB"] == pytest.approx(out[ABILENE2]["basis"]["pariClaimsB"])
    assert out[WULF]["basis"]["grossMwBasis"].startswith("critical_it_mw x PUE")
    assert out[NOTE]["method"] is None and out[NOTE]["recovery"]["value"] is None and out[NOTE]["missing"]
    assert "model v0" in ab["recovery"]["methodVersion"]


def test_recovery_basis_carries_its_arithmetic(copied):
    """The basis shows how the number is made: shell value at the 25th / 50th / 75th comp and the cover
    before the 0-100c cap, which give the range; a GPU pool names its haircut."""
    out = recovery.run(instruments=[ABILENE1, DDTL1, NOTE], trigger="test")["outputs"]
    b = out[ABILENE1]["basis"]
    e, share = b["energizedShare"], b["completion"]
    factor = e + (share - e) * (1 - b["unfinishedHaircut"])  # v0.2 haircut on works not yet energized
    works = [b["grossMw"] * factor * q for q in b["compQuartilesUsdPerMw"]]
    assert b["worksUsd"] == pytest.approx(works, rel=1e-3)
    value = [w + (b["unspentProceedsUsd"] or 0) for w in works]
    assert b["valueUsd"] == pytest.approx(value, rel=1e-3)
    assert b["valueUsd"][1] / 1e9 == pytest.approx(out[ABILENE1]["valueB"]["value"], rel=1e-4)
    ahead, pari = b["seniorClaimsB"] * 1e9, b["pariClaimsB"] * 1e9
    assert b["coverCents"] == pytest.approx([(v - ahead) / pari * 100 for v in b["valueUsd"]], abs=0.5)
    capped = [max(0.0, min(1.0, c / 100)) for c in b["coverCents"]]
    assert out[ABILENE1]["recovery"]["value"] == range_text(capped[0], capped[2])
    assert out[DDTL1]["basis"]["haircut"] == HAIRCUT
    assert "valueUsd" not in out[NOTE]["basis"] and out[NOTE]["recovery"]["value"] is None


def test_live_runs_mark_their_views(copied, committed):
    gpu_index.run_both(["*"], "test")
    gpu_collateral.run(trigger="test")
    gpu_deployment.run(trigger="test")
    dirty = {(r["view"], r["key"]) for r in committed.execute("SELECT view, key FROM read_model_dirty")}
    assert {("gpus", "all"), ("gpu_model", "H100"), ("gpu_deployment", "all"), ("gpu_collateral", DDTL1),
            ("instrument_detail", DDTL1)} <= dirty
    models = {(r["model"], r["key"]) for r in committed.execute("SELECT model, key FROM model_dirty")}
    assert ("recovery", DDTL1) in models  # gpu_collateral feeds recovery


def test_formulas():
    k = math.log1p(0.09) + 0.25
    assert annuity(-0.25, 0.09, 3.0) == pytest.approx(quad(lambda t: math.exp(-k * t), 0, 3.0)[0], rel=1e-9)
    assert annuity(-0.25, 0.09, 0) == 0
    assert range_text(0.621, 0.734) == "62\u201373" and range_text(1.0, 1.0) == "100" and range_text(None, 1) is None
    assert rank_of("Secured (SPV)") == 1 and rank_of("Senior unsecured") == 3 and rank_of("Junior subordinated") == 2
    assert rank_of("Guarantee / off-balance-sheet") is None
    assert chip_key("GB200 NVL72") == "GB200" and chip_key("H100/H200") == "H100" and chip_key("TPU") is None
    assert ship_date("2024-Q3") == date(2024, 8, 15) and ship_date("2022-10") == date(2022, 10, 15)
    assert ship_date("2027") == date(2027, 7, 1) and ship_date(None) is None
    assert provider_name("CoreWeave HGX H100 list ($49.24/hr per 8-GPU)", "coreweave") == "CoreWeave"
    assert provider_name("Hot Aisle (cheapest in-stock)", "hot-aisle") == "Hot Aisle"
