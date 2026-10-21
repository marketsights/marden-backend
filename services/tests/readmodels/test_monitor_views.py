"""Monitor builders (home, risk_list, track_record, gpus, gpu_model, gpu_deployment) on real seeded rows.

Rows are copied from the local `marden` database (monitor_rows.copy_monitor): every site and instrument
with their facts, events, prices and holdings, the status, cushion, price and mark-gap outputs, the GPU
models' outputs and the model runs. Expectations are computed from the copied rows with SQL independent
of the builders, so the tests follow the data as collectors and models add to it. Where a model has not
run (the risk ranking, the backtest), the documents must be absent or empty, never a placeholder. The
risk-list tests publish handoff/fixtures/risk-list.json as the ranking model's output (fixtures are for
tests only), since no list has been published yet.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from monitor_rows import copy_monitor
from monitor_shapes import available, check
from real_rows import copied_rows

from marden.core import db
from marden.core.jsonutil import jsonb
from marden.readmodels import business_today, rebuild_all
from marden.readmodels.context import building
from marden.readmodels.deps import targets
from marden.readmodels.registry import get_builder
from marden.readmodels.views import gpus, home, risk_list, track_record  # noqa: F401 (registers builders)
from marden.readmodels.views.track_record import MODEL_INFO

REPO = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((REPO / "handoff" / "fixtures" / "risk-list.json").read_text())
QUIET = {"none", "green"}


@pytest.fixture
def monitor() -> list[str]:
    return copy_monitor()


def build(view: str, key: str = "all", day: date | None = None) -> Any:
    """The builder's document for `key` (built in a transaction, nothing written)."""
    b = get_builder(view)
    assert b is not None, view
    with building(day or business_today()), db.transaction() as conn:
        if b.build_many is not None:
            return dict(b.build_many(conn)).get(key)
        return b.build_one(conn, key)


def keys_of(view: str, day: date | None = None) -> list[str]:
    b = get_builder(view)
    assert b is not None
    with building(day or business_today()), db.transaction() as conn:
        return b.all_keys(conn)


def output_on(model: str, subject_id: str, when: date, subject_type: str | None = None) -> dict | None:
    rows = copied_rows(
        """SELECT * FROM model_output WHERE model = %s AND subject_id = %s AND as_of <= %s
             AND (%s::text IS NULL OR subject_type = %s) ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
        (model, subject_id, when, subject_type, subject_type),
    )
    return rows[0] if rows else None


def site_instruments(slug: str) -> set[str]:
    return {r["id"] for r in copied_rows("SELECT id FROM instrument WHERE site_slug = %s", (slug,))}


def test_every_monitor_view_rebuilds(monitor):
    for view in ("home", "track_record", "gpus", "gpu_deployment"):
        stats = rebuild_all(view)
        assert not stats.errors, (view, stats.errors[:3])
        assert stats.built == 1, view
    models = {r["subject_id"] for r in copied_rows("SELECT DISTINCT subject_id FROM model_output "
                                                   "WHERE model = 'gpu_model'")}
    stats = rebuild_all("gpu_model")
    assert not stats.errors and stats.built + stats.removed == len(models)
    weeks = copied_rows("SELECT DISTINCT subject_id FROM model_output WHERE model = 'risk_list'")
    stats = rebuild_all("risk_list")
    assert not stats.errors and stats.built == len(weeks)  # no list is published until the ranking runs
    assert stats.events == 0  # monitor views announce nothing themselves


def test_documents_match_the_contracts(monitor):
    if not available():
        pytest.skip("TypeScript toolchain (apps/api/node_modules) not installed")
    docs = [("A.HomeDoc", build("home")), ("A.TrackRecord", build("track_record")), ("A.GpuIndexPage", build("gpus")),
            ("T.GpuDeploymentPage", build("gpu_deployment"))]
    docs += [("T.GpuModelDetail", d) for k in keys_of("gpu_model") if (d := build("gpu_model", k)) is not None]
    docs.append(("A.HomeDoc", build("home", day=business_today() - timedelta(days=1))))
    assert check(docs) == ""


def _cushion(slug: str, when: date) -> float | None:
    out = output_on("cushion", slug, when, "site")
    projection = (out or {"output": {}})["output"].get("projection") or {}
    value = (projection.get("cushionMonths") or {}).get("value") if projection.get("p50") else None
    return value if isinstance(value, int | float) else None


def _status_moved(slug: str, today: date) -> bool:
    now, before = output_on("status", slug, today, "site"), output_on("status", slug, today - timedelta(days=7), "site")
    if now is None or before is None or now["id"] == before["id"]:
        return False
    old = {link["role"]: link["status"] for link in before["output"].get("chain") or []}
    links = [(old.get(link["role"]), link["status"]) for link in now["output"].get("chain") or []]
    moved = any(a is not None and a != b and not {a, b} <= QUIET for a, b in links)
    return moved or now["output"].get("status") != before["output"].get("status")


def test_home_headline_pieces_follow_the_models(monitor):
    today, doc = business_today(), build("home")
    assert doc["asOf"] == today.isoformat() and doc["week"]["end"] == today.isoformat()
    under = {s for s in monitor if (c := _cushion(s, today)) is not None and c < 3}
    assert {e["watch"][0] for e in doc["under3"] if "site" not in e} == {f"site:{s}" for s in under}
    for entry in doc["under3"]:
        assert entry["cushionMonths"]["value"] < 3 and entry["cushionMonths"]["tag"] == "M"
        if "site" in entry:  # a loan on an under-3 site counts its own size
            assert entry["watch"][0].removeprefix("instrument:") in site_instruments(entry["site"])
    known = {s for s in monitor if _cushion(s, today) is not None}
    assert {k for k in doc["cushionKnown"] if k.startswith("site:")} == {f"site:{s}" for s in known}
    if "jupiter" in under:  # Jupiter: -1 months on the cushion model, $18B of filed construction debt
        site = next(e for e in doc["under3"] if e["watch"] == ["site:jupiter"])
        assert site["name"] == "Project Jupiter" and site["debtB"]["tag"] in ("F", "R")

    moved = {s for s in monitor if _status_moved(s, today)}
    assert {c["watch"][0] for c in doc["statusChanges"]} == {f"site:{s}" for s in moved}
    for line in doc["statusChanges"]:
        slug = line["watch"][0].removeprefix("site:")
        assert set(line["watch"][1:]) == {f"instrument:{i}" for i in site_instruments(slug)}
        assert " to " in line["line"]

    gaps = copied_rows("""SELECT DISTINCT ON (subject_id) subject_id, output->'markGap' AS gap FROM model_output
                           WHERE model = 'mark_gap' AND as_of <= %s ORDER BY subject_id, as_of DESC, id DESC""",
                       (today,))
    values = {g["subject_id"]: g["gap"].get("value") if isinstance(g["gap"], dict) else None for g in gaps}
    high = {i for i, v in values.items() if isinstance(v, int | float) and v <= -5}
    assert {f["watch"][0] for f in doc["fundMarksHigh"]} == {f"instrument:{i}" for i in high}
    gap_values = [f["gapPts"]["value"] for f in doc["fundMarksHigh"]]
    assert gap_values == sorted(gap_values)


def test_home_changes_are_this_weeks_and_ordered(monitor):
    today, doc = business_today(), build("home")
    assert doc["changes"] == sorted(doc["changes"], key=lambda c: -c["severity"])
    by_type: dict[str, list[dict]] = {}
    for change in doc["changes"]:
        by_type.setdefault(change["type"], []).append(change)
        assert change["severity"] in (1, 2, 3) and change["record"]["text"]
        assert change["watch"][0] == f"{change['kind']}:{change['refId']}"
    for change in by_type.get("cushion", []):
        slug = change["refId"]
        before, now = _cushion(slug, today - timedelta(days=7)), _cushion(slug, today)
        assert change["from"] == before != change["to"]["value"] == now
    for change in by_type.get("status", []):
        assert _status_moved(change["refId"], today) and change["from"] != change["to"]["value"]
        assert change["label"].endswith(" link")
    for change in by_type.get("price", []):
        out = output_on("price", change["refId"], today, "instrument")
        moved = abs(change["to"]["value"] - change["from"])
        assert moved >= 1 and change["to"]["tag"] == "M"
        if out is not None and isinstance(out["output"].get("price"), int | float):
            assert change["to"]["value"] == out["output"]["price"]
    for change in by_type.get("filing", []):
        assert today - timedelta(days=6) <= date.fromisoformat(change["record"]["date"]) <= today


def test_home_coming_up_is_the_scheduled_steps_after_today(monitor):
    today, doc = business_today(), build("home")
    items = doc["comingUp"]
    assert all(c["date"] > today.isoformat() for c in items)
    assert [c["date"] for c in items] == sorted(c["date"] for c in items)
    assert len({(c["date"], c["what"], c["refUrl"]) for c in items}) == len(items)
    steps = copied_rows("""SELECT DISTINCT subject_type, subject_id, event_date FROM event WHERE kind = 'catalyst'
                            AND status = 'published' AND event_date > %s AND subject_type IN ('site', 'instrument')""",
                        (today,))
    shown = {(c["date"], c["refUrl"]) for c in items}
    for ev in steps:
        href = f"/{'sites' if ev['subject_type'] == 'site' else 'instruments'}/{ev['subject_id']}"
        assert (ev["event_date"].isoformat(), href) in shown
    jupiter = [c for c in items if c["refUrl"] == "/sites/jupiter"]
    if jupiter:  # the step the two loaders both wrote shows once, under its short headline
        assert len({(c["date"], c["what"]) for c in jupiter}) == len(jupiter)
        assert all(c["when"] for c in jupiter)


def test_home_backfill_reads_what_was_known(monitor):
    past = business_today() - timedelta(days=1)
    doc = build("home", day=past)
    assert doc["asOf"] == past.isoformat()
    assert all(c["date"] > past.isoformat() for c in doc["comingUp"])
    for entry in doc["under3"]:
        slug = entry.get("site") or entry["watch"][0].removeprefix("site:")
        assert entry["cushionMonths"]["value"] == _cushion(slug, past)
    known = copied_rows("""SELECT DISTINCT subject_type, subject_id, event_date FROM event WHERE kind = 'catalyst'
                            AND learned_at < %s::date AND event_date > %s""", (past + timedelta(days=1), past))
    known_steps = {(e["event_date"].isoformat(),
                    f"/{'sites' if e['subject_type'] == 'site' else 'instruments'}/{e['subject_id']}") for e in known}
    for item in doc["comingUp"]:
        if not item["what"].endswith("marks public"):
            assert (item["date"], item["refUrl"]) in known_steps  # nothing learned after the day


def test_track_record_rules_models_and_cases(monitor):
    from marden.models.materiality import RULE_CODES

    doc = build("track_record")
    assert [r["link"] for r in doc["rules"]] == ["End user", "Tenant", "Developer", "Lenders"]
    assert [(r["amberCode"], r["redCode"]) for r in doc["rules"]] == [("E1", "E2"), ("T1", "T2"), ("D1", "D2"),
                                                                      ("L1", "L2")]
    assert doc["rules"][3]["redWhen"] == RULE_CODES["L2"][0].split(": ", 1)[1]
    assert [m["code"] for m in doc["material"]] == [c for c in RULE_CODES if c.startswith("M")]
    runs = copied_rows("""SELECT DISTINCT model FROM model_run WHERE model NOT LIKE 'legacy%%' AND ok IS NOT FALSE
                           AND started_at < %s::date""", (business_today() + timedelta(days=1),))
    ran = {r["model"] for r in runs}
    assert ran <= set(MODEL_INFO), f"models without a Track record entry: {ran - set(MODEL_INFO)}"
    assert {m["model"] for m in doc["models"]} == ran
    assert all(m["version"].startswith(m["model"]) or " v" in m["version"] for m in doc["models"])
    backtest = output_on("track_record", "all", business_today())
    if backtest is None:  # the backtest has not run: no cases, never a placeholder list
        assert (doc["cases"], doc["published"], doc["total"]) == ([], 0, 0)
    else:
        cases = backtest["output"].get("cases") or []
        assert [(c["name"], c["flagged"], c["moved"]) for c in doc["cases"]] == [
            (c["name"], c.get("flagged"), c.get("moved")) for c in cases]
        assert all(c["flagBasis"] in ("snapshot", "reconstructed", None) for c in doc["cases"])
        method = backtest["output"].get("methodVersion")
        assert doc["backtest"]["methodVersion"] == (method if isinstance(method, str) else backtest["version"])
        assert doc["published"] == sum(1 for c in cases if not c.get("backtestRunning"))


def _publish(week: str, output: dict) -> None:
    copied_rows(
        """INSERT INTO model_output (model, version, subject_type, subject_id, as_of, output)
           VALUES ('risk_list', '0.1', 'series', %s, %s, %s) RETURNING id""",
        (week, week, jsonb(output)),
    )


def test_risk_list_carries_the_published_list(monitor):
    today = business_today()
    published = copied_rows("SELECT DISTINCT subject_id FROM model_output WHERE model = 'risk_list' AND as_of <= %s",
                            (today,))
    assert sorted(keys_of("risk_list")) == sorted(r["subject_id"] for r in published)
    if not published:
        pytest.skip("the ranking model has not published a list yet")
    docs = []
    for week in (r["subject_id"] for r in published):
        doc, out = build("risk_list", week), output_on("risk_list", week, today)
        assert doc["week"]["start"] == week and len(doc["rows"]) == len(out["output"]["rows"])
        for row, raw in zip(doc["rows"], sorted(out["output"]["rows"], key=lambda r: r["rank"]), strict=True):
            for k in ("rank", "instrumentId", "name", "size", "why", "fundMark", "price", "change1w", "odds"):
                assert row[k] == raw[k], (week, row["rank"], k)
            price, mark = (raw["price"] or {}).get("value"), raw["fundMark"]
            if raw.get("gap") is not None:
                assert row["gap"] == raw["gap"]
            elif isinstance(price, int | float) and isinstance(mark, int | float):
                assert row["gap"] == round(price - mark, 2)  # its own published price minus its fund mark
            else:
                assert row["gap"] is None
        docs.append(("T.RiskList", doc))
    if available():
        assert check(docs) == ""


def test_risk_list_from_the_fixture(monitor):
    """The handoff fixture published as the ranking model's output (fixtures are for tests only)."""
    copied_rows("DELETE FROM model_output WHERE model = 'risk_list' RETURNING id")
    week = FIXTURE["week"]["start"]
    later = (date.fromisoformat(week) + timedelta(days=7)).isoformat()
    _publish(week, FIXTURE)
    _publish(later, {"rows": [{k: v for k, v in r.items() if k != "move"} for r in FIXTURE["rows"]]})
    assert keys_of("risk_list") == [week, later]
    doc = build("risk_list", week)
    assert doc["week"] == FIXTURE["week"] and doc["note"] == FIXTURE["note"]
    assert doc["earlierCalls"] == FIXTURE["earlierCalls"]
    for row, raw in zip(doc["rows"], FIXTURE["rows"], strict=True):
        for k in ("rank", "instrumentId", "name", "size", "why", "fundMark", "gap", "odds", "move"):
            assert row[k] == raw[k], k
        for k in ("price", "change1w"):  # an M value gets the output's method version when it has none
            assert row[k] == {**raw[k], "methodVersion": "risk_list v0.1"} if raw[k] else row[k] is None
    nxt = build("risk_list", later)
    assert [r["move"] for r in nxt["rows"]] == [{"kind": "same"}] * len(FIXTURE["rows"])  # same ranks as last week
    assert nxt["note"] == {"title": "", "paragraphs": []}  # no note drafted for that week
    assert build("risk_list", "2026-01-05") is None
    if available():
        assert check([("T.RiskList", doc), ("T.RiskList", nxt)]) == ""


def test_gpu_views_carry_the_gpu_models(monitor):
    today = business_today()
    index_out = output_on("gpu_index", "all", today)
    doc = build("gpus")
    if index_out is None:
        assert (doc["index"], doc["providers"]) == ([], [])
    else:
        body = index_out["output"]
        assert {(p["date"], p["model"]) for p in doc["index"]} == {(p["date"], p["model"]) for p in body["index"]}
        assert len(doc["providers"]) == len([p for p in body["providers"] if p["usdHr"]["value"] is not None])
        assert all(p["usdHr"]["tag"] in ("F", "R") and p["pageUrl"] for p in doc["providers"])
    models = keys_of("gpu_model")
    with_curve = set()
    for model in models:
        out = output_on("gpu_model", model, today)
        page = build("gpu_model", model)
        assert page["model"] == out["output"].get("model", model)
        ages = sorted(p["age"] for p in out["output"]["valueByAgeYears"])
        assert [p["age"] for p in page["valueByAgeYears"]] == ages
        assert page["assumptions"]["methodVersion"] == out["output"]["assumptions"]["methodVersion"]
        if any(p["usd"]["value"] is not None for p in page["valueByAgeYears"]):
            with_curve.add(model)
    assert {c["model"] for c in doc["valueByAge"]} == with_curve
    deployment_out, page = output_on("gpu_deployment", "all", today), build("gpu_deployment")
    if deployment_out is not None:
        body = deployment_out["output"]
        assert [f["id"] for f in page["facilities"]] == [f["id"] for f in body["facilities"]
                                                         if (f.get("assumptions") or {}).get("pue") is not None]
        assert len(page["companies"]) == len(body["companies"])
        assert page["totals"]["idleShare"]["tag"] == "M"


def test_gpu_views_are_empty_before_the_gpu_models_run(monitor):
    copied_rows("DELETE FROM model_output WHERE model IN ('gpu_index', 'gpu_model', 'gpu_deployment') RETURNING id")
    assert keys_of("gpu_model") == [] and build("gpu_model", "H100") is None
    assert build("gpus") == {"asOf": business_today().isoformat(), "index": [], "providers": [], "valueByAge": []}
    page = build("gpu_deployment")
    assert (page["facilities"], page["companies"]) == ([], [])
    assert all(v["value"] is None and v["tag"] == "M" for v in page["totals"].values())


def test_dependencies_reach_the_monitor_views(monitor):
    with db.transaction() as conn:
        assert ("home", "all") in targets("site", "jupiter", "@model:status", conn=conn)
        assert ("home", "all") in targets("site", "jupiter", "@event", conn=conn)
        inst = copied_rows("SELECT id FROM instrument WHERE site_slug = 'jupiter' LIMIT 1")
        if inst:
            assert ("home", "all") in targets("instrument", inst[0]["id"], "@model:price", conn=conn)
        assert ("gpu_deployment", "all") in targets("entity", "coreweave", "construction_in_progress_usd", conn=conn)
