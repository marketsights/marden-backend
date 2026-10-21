"""The eval set: gold facts from real stored documents, confirmed against their quotes, and the recorded
answers scored per area (CI-safe; `python -m marden agents eval` re-runs it live on prompt/model changes)."""

from __future__ import annotations

import json

import pytest

from marden.agents.areas import AREAS
from marden.agents.config import PROMPT_VERSION
from marden.agents.evals import RECORDED_DIR, load_cases, run_eval, value_match
from marden.agents.extract import Candidate
from marden.agents.fields import fields_for
from marden.agents.validators import check_candidate

CASES = load_cases()


def test_every_area_has_gold_from_a_real_stored_document():
    assert {c.area for c in CASES} == set(AREAS)
    for c in CASES:
        doc = c.document
        assert doc["document_id"] and len(doc["sha256"]) == 64 and doc["url"].startswith("http"), c.id
        assert len(c.excerpt) < 8000 and c.gold, c.id


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_gold_facts_are_confirmed_against_their_quotes(case):
    doc = case.text()
    for g in case.gold:
        spec = fields_for(case.area)[g["field"]]
        kinds = g["subject_kind"] if isinstance(g["subject_kind"], list) else [g["subject_kind"]]
        assert set(kinds) & set(spec.kinds), (case.id, g["field"])
        c = Candidate(area=case.area, section="all", subject_kind=next(k for k in kinds if k in spec.kinds),
                      subject_name="gold", subject_identifier=None, field=g["field"], value=g["value"],
                      unit=spec.unit, as_of=None, quote=g["quote"], page=None, confidence=1.0)
        check_candidate(c, doc)
        failed = {k: v for k, v in c.checks.items() if not v["ok"]}
        assert not failed, (case.id, g["field"], failed)


def test_recorded_answers_score_per_area():
    for c in CASES:
        rec = json.loads((RECORDED_DIR / f"{c.id}.json").read_text())
        assert rec["prompt_version"] == PROMPT_VERSION and rec["model"].startswith("claude-")
    report = run_eval(recorded=True)
    assert report["status"] == "ok" and report["mode"] == "recorded"
    total = report["total"]
    assert total["gold"] == sum(len(c.gold) for c in CASES)
    assert total["precision"] >= 0.9 and total["recall"] >= 0.9, total
    assert set(report["by_area"]) == set(AREAS)
    debt = next(r for r in report["results"] if r["case"] == "debt_coreweave_8k_ddtl55")
    assert debt["matched"] >= 17 and debt["rejected"] == 0


def test_value_matching_rules():
    assert value_match(2600000000, 2600000000.0) and not value_match(2600000000, 2700000000)
    assert value_match("2026-12", "2026-12") and not value_match("2026-12", "2026-11")
    assert value_match("JPMorgan Chase Bank, N.A.", "JPMorgan Chase Bank, N.A., as administrative agent")
    assert value_match(["Cognition", "Databricks"], ["Cognition", "Databricks", "Rescale"])
    assert not value_match(["Cognition", "Databricks"], ["Bentley Systems"])
    assert value_match({"docket": "EL26-101", "outcome": "denied"}, {"Docket": "EL26-101-000", "outcome": "denied"})
    assert not value_match(True, 1)


def test_agent_tasks_are_registered_on_their_queue():
    from marden.agents import tasks
    from marden.core.queue import app
    from marden.plugins import load_plugins

    load_plugins()
    names = {"agents.sweep": tasks.SWEEP_CRON, "agents.work": tasks.WORK_CRON, "agents.batch_poll": tasks.BATCH_CRON,
             "agents.r2_inbox": tasks.INBOX_CRON}
    periodic = {name: task.cron for (name, _), task in app.periodic_registry.periodic_tasks.items()}
    for name, cron in names.items():
        assert app.tasks[name].queue == "agents" and periodic[name] == cron
