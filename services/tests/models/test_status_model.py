"""The `status` model against a slice of the real archive (MARDEN_SEED_DIR).

The slice: the five Oracle-leased Stargate sites (Jupiter, Frontier, Lighthouse, The Barn, Abilene, so the
tenant's rating is pooled across sites), Meta's Hyperion and the cancelled Southgate project, loaded with
the real `sites` and `cases` loaders into the throwaway test database. Every expectation is read from the
archive files themselves (the archive's own dates and wording); nothing is made up.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from marden.core import config, events
from marden.models import status as status_model
from marden.models import triggers
from marden.models.materiality import is_material
from marden.readmodels.context import business_today
from marden.seed import common

pytestmark = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")

CASES = ["JUPITER", "VANTAGE_TX", "VANTAGE_WI", "SALINE", "ABILENE", "BEIGNET", "SOUTHGATE"]
ROLES = ["end_user", "tenant", "developer", "lenders"]
CHAIN_STATUSES = {"Broken", "Strained", "Holding", "No build"}


@pytest.fixture(scope="module")
def seed():
    config.reload_settings()
    common.seed_dir.cache_clear()
    return common.load_json


@pytest.fixture
def loaded(seed, committed):
    from marden.seed.cases import load as load_cases
    from marden.seed.sites import load as load_sites

    load_sites(CASES)
    load_cases(CASES)
    return seed


def _link(output: dict, role: str) -> dict:
    return next(link for link in output["chain"] if link["role"] == role)


def _jupiter_changes(seed) -> list[dict]:
    return next(p for p in seed("aiwc/aiw_proj.json") if p["project"] == "Project Jupiter (Stargate New Mexico)"
                and any("force majeure" in (c.get("what") or "") for c in p["changes"]))["changes"]


def test_today_follows_the_rules_and_the_types(loaded, committed):
    result = status_model.run()
    outputs = result["outputs"]
    assert set(outputs) == {r["slug"] for r in committed.execute("SELECT slug FROM site").fetchall()}
    for slug, out in outputs.items():
        assert [link["role"] for link in out["chain"]] == ROLES and out["status"] in CHAIN_STATUSES
        events.validate({"type": "status", "siteSlug": slug, "chain": out["chain"]})  # types.ts ChainLink
        assert isinstance(out["missing"], list)

    jupiter = outputs["jupiter"]
    assert jupiter["status"] == "Broken"
    tenant = _link(jupiter, "tenant")
    assert (tenant["status"], tenant["ruleCode"]) == ("red", "T2")
    notice = next(c for c in _jupiter_changes(loaded) if "force majeure notice" in c["what"])
    assert tenant["fact"]["asOf"] == notice["date"] and "force majeure" in tenant["fact"]["value"].lower()
    assert "BBB-" in tenant["reason"]  # the archive's tenant rating: 'Baa2 (neg) / BBB- (S&P ...) / BBB'
    assert _link(jupiter, "developer")["ruleCode"] == "D1"  # County: initial operations Q4 2026 -> 2028
    lenders = _link(jupiter, "lenders")
    assert (lenders["status"], lenders["ruleCode"]) == ("amber", "L1") and "yndication" in lenders["reason"]
    assert "lenders: Marden price (price model output)" in jupiter["missing"]  # no price model output yet

    # Frontier: the same tenant, no force majeure: amber on the rating (pooled from the other Oracle sites).
    frontier_tenant = _link(outputs["frontier"], "tenant")
    assert (frontier_tenant["status"], frontier_tenant["ruleCode"]) == ("amber", "T1")
    # Hyperion: Meta is AA- (archive: 'Meta AA- (S&P) / Aa3 (Moody's)'); no end user apart from the tenant.
    hyperion = outputs["hyperion"]
    assert _link(hyperion, "tenant")["status"] == "green" and _link(hyperion, "end_user")["status"] == "none"
    # Southgate: the dossier's expected completion reads 'cancelled (CDC partnership ended)'.
    assert loaded("aiwc/cases/SOUTHGATE.json")["capacity"]["scheduled_completion"].startswith("cancelled")
    assert _link(outputs["southgate"], "developer")["ruleCode"] == "D2" and outputs["southgate"]["status"] == "Broken"


def test_backtests_use_only_what_was_public(loaded, committed):
    """Jupiter through 2026: Holding before S&P's cut, Strained (T1) after it, Broken (T2) after the notice."""
    cut = next({"date": e["d"]} for e in loaded("aiwc/cases/JUPITER.json")["risks"]["tenant"]["evidence"]
               if "BBB-" in (e.get("w") or ""))
    notice = next(c for c in _jupiter_changes(loaded) if "force majeure notice" in c["what"])
    before_cut = date.fromisoformat(cut["date"]).replace(day=1)
    between = date(2026, 9, 1)
    after_notice = date.fromisoformat(notice["date"]).replace(day=30)
    assert before_cut < date.fromisoformat(cut["date"]) < between < date.fromisoformat(notice["date"]) < after_notice

    early = status_model.run(before_cut, ["jupiter"])["outputs"]["jupiter"]
    assert _link(early, "tenant")["status"] == "green" and "tenant: rating for Oracle" in early["missing"]
    mid = status_model.run(between, ["jupiter"])["outputs"]["jupiter"]
    mid_tenant = _link(mid, "tenant")  # the latest rating statement public by then, at or after S&P's cut
    assert mid_tenant["ruleCode"] == "T1" and "BBB-" in mid_tenant["reason"]
    assert cut["date"] <= mid_tenant["fact"]["asOf"] <= between.isoformat() and mid["status"] == "Strained"
    late = status_model.run(after_notice, ["jupiter"])["outputs"]["jupiter"]
    assert (_link(late, "tenant")["ruleCode"], _link(late, "tenant")["fact"]["asOf"]) == ("T2", notice["date"])
    # The alert engine's test on the same pair: the notice is a tenant notice and turns the tenant link red.
    change = is_material({"title": notice["what"], "kind": "news"}, mid, late)
    assert {"M4", "T2"} <= set(change.codes) and change.severity == "red"
    for out in (early, mid, late):  # nothing dated after the run day reaches a backtest
        for link in out["chain"]:
            assert "fact" not in link or link["fact"]["asOf"] <= after_notice.isoformat()
    stored = committed.execute(
        "SELECT as_of FROM model_output WHERE model = 'status' AND subject_id = 'jupiter' ORDER BY as_of"
    ).fetchall()
    assert [r["as_of"] for r in stored] == [before_cut, between, after_notice]
    assert committed.execute("SELECT count(*) AS n FROM read_model_dirty WHERE reason LIKE 'model:status%'"
                             ).fetchone()["n"] == 0  # backtests mark no screen


def test_reruns_replace_the_day_and_mark_only_changes(loaded, committed):
    first = status_model.run()
    assert sorted(first["changed"]) == sorted(first["outputs"])
    marked = committed.execute(
        "SELECT view, key FROM read_model_dirty WHERE reason = %s", (f"model:status@{first['run_id']}",)
    ).fetchall()
    assert {("site_detail", "jupiter"), ("sites", "all"), ("completion", "all")} <= {(r["view"], r["key"])
                                                                                     for r in marked}
    second = status_model.run()
    assert second["changed"] == [] and second["outputs"] == first["outputs"]
    rows = committed.execute(
        "SELECT count(*) AS n, count(DISTINCT subject_id) AS sites FROM model_output WHERE model = 'status'"
    ).fetchone()
    assert rows["n"] == rows["sites"] == len(first["outputs"])  # one output per site and day
    runs = committed.execute("SELECT ok, trigger, inputs_hash FROM model_run WHERE model = 'status' ORDER BY id"
                             ).fetchall()
    assert [r["ok"] for r in runs] == [True, True] and runs[0]["inputs_hash"] == runs[1]["inputs_hash"]
    with pytest.raises(ValueError):
        status_model.run(date.fromordinal(business_today().toordinal() + 1))


def test_published_inputs_mark_the_model_and_run_dirty_clears_them(seed, committed):
    """Loading a case publishes its facts; the writers' hook marks the status model for that site."""
    from marden.seed.cases import load as load_cases
    from marden.seed.sites import load as load_sites

    load_sites(["JUPITER", "BEIGNET"])
    marks = {r["key"] for r in committed.execute("SELECT key FROM model_dirty WHERE model = 'status'").fetchall()}
    assert {"jupiter", "hyperion"} <= marks
    out = triggers.run_dirty(["status"])
    assert out["status"]["sites"] == 2
    assert committed.execute("SELECT count(*) AS n FROM model_dirty WHERE model = 'status'").fetchone()["n"] == 0
    load_cases(["JUPITER"])  # the dossier's risks, schedule changes and debt notes land on Jupiter only
    marks = {r["key"] for r in committed.execute("SELECT key FROM model_dirty WHERE model = 'status'").fetchall()}
    assert "jupiter" in marks and "hyperion" not in marks
    assert triggers.run_dirty(["status"])["status"]["changed"] == ["jupiter"]


def test_task_and_command_are_registered():
    from marden import cli
    from marden.core.queue import app

    task = app.tasks["models.status"]
    assert task.queue == "models"
    periodic = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert periodic["models.status"] == status_model.CRON_UTC
    assert "status" in triggers.registered_models()
    parser = cli.build_parser()
    args = parser.parse_args(["model", "status", "--as-of", "2026-09-30", "--site", "jupiter"])
    assert args.as_of == date(2026, 9, 30) and args.sites == ["jupiter"]
