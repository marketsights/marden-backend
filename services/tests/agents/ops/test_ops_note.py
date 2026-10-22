"""Note writer: code fills every figure, the model writes only the words, the validator refuses any number or
claim of its own, the draft waits for a person, and the approved note is what GET /api/risk-list serves."""

from __future__ import annotations

import json
from datetime import date

import pytest
from ops_rows import fake_llm, recorded

from marden.agents.config import BackendStatus, set_backend_status
from marden.agents.ops import note, note_prose
from marden.agents.ops.numbers import Figure, check_filled, check_prose, vocabulary
from marden.agents.review import decide
from marden.core import db
from marden.core.jsonutil import jsonb

WEEK = date(2026, 10, 19)
LIST = json.loads(recorded("risk_list_2026-10-19.json"))


@pytest.fixture(autouse=True)
def _no_backend():
    set_backend_status(BackendStatus("not_configured", "test: no backend"))
    yield
    set_backend_status(None)


def seed_list() -> int:
    with db.transaction() as conn:
        row = conn.execute(
            """INSERT INTO model_output (model, version, subject_type, subject_id, as_of, output)
               VALUES ('risk_list', %s, 'week', %s, %s, %s) RETURNING id""",
            (LIST["version"], WEEK.isoformat(), LIST["as_of"], jsonb(LIST["output"]))).fetchone()
    return int(row["id"])


FIGS = {"rank": Figure("rank", "1", "rank"), "count": Figure("count", "15", "count"),
        "odds": Figure("odds", "63% odds of a 3-point move in 90 days (odds v0, uncalibrated)", "odds"),
        "change": Figure("change", "down 1.4 points on the week", "change")}
KNOWN = vocabulary("Project Jupiter Construction loans", *(f.text for f in FIGS.values()))


@pytest.mark.parametrize("template, problem", [
    ("Ranked {{rank}} of {{count}} with {{odds}}.", None),
    ("Ranked {{rank}} of 15, with {{odds}}.", "a number written in the prose"),
    ("Ranked {{rank}} of {{count}}; three records explain {{odds}}.", "number words"),
    ("Ranked {{rank}} of {{count}}, its price fell sharply: {{change}}.", "direction or size words"),
    ("Ranked {{rank}} of {{count}}; Moody's sees {{odds}}.", "names the figures do not hold"),
    ("Ranked {{rank}} of {{count}}, {{yield}}.", "unknown placeholders"),
    ("Ranked {{rank}} of {{count}} {odds}.", "a brace outside"),
])
def test_prose_validator(template, problem):
    problems = check_prose(template, FIGS, KNOWN)
    if problem is None:
        assert problems == []
    else:
        assert any(problem in p for p in problems), problems


def test_filled_text_holds_only_figures():
    used = [FIGS["rank"], FIGS["count"]]
    assert check_filled("Ranked 1 of 15.", used) == []
    assert check_filled("Ranked 1 of 15, 63% odds.", used) == ["numbers that are not filled figures: 63"]


def test_figures_are_code_written_and_rounded_like_the_app():
    paragraphs = note.paragraphs_of(LIST["output"])
    assert [p["id"] for p in paragraphs] == ["EX-JUPITER-190", "EX-SALINE-308", "EX-GLXY-163", "EX-APLD-011"]
    figs = {f.name: f for f in paragraphs[0]["figures"]}
    assert figs["count"].text == "15"
    assert figs["odds"].text.startswith("63% odds")  # 0.625 half up, as the list itself says
    assert figs["price"].text == "Marden price 85.8" and figs["change"].text == "down 1.4 points on the week"
    assert figs["r2"].ref == "model:cushion@site:jupiter:2026-10-21"
    assert LIST["output"]["note"]["paragraphs"][0]["text"].startswith("Ranked 1 of 15, with 63% odds")


def test_draft_without_a_model_is_the_code_note_waiting_for_review():
    seed_list()
    report = note.draft(WEEK)
    assert report["writer"] == "code" and "not configured" in report["model"]
    assert set(report["paragraph_writers"].values()) == {"code"}
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM weekly_note").fetchone()
        item = conn.execute("SELECT * FROM review_item WHERE kind = 'note'").fetchone()
        run = conn.execute("SELECT * FROM agent_ops_run WHERE role = 'note_writer'").fetchone()
    assert row["status"] == "draft" and row["review_item_id"] == item["id"] and item["status"] == "open"
    first = row["note"]["paragraphs"][0]
    assert first["lead"] == "Project Jupiter Construction loans"
    assert first["text"].startswith("Ranked 1 of 15, with 63% odds of a 3-point move in 90 days")
    assert "model:cushion@site:jupiter:2026-10-21" in first["refs"]
    assert run["status"] == "ok" and run["model_status"] == "not_configured"
    again = note.draft(WEEK)
    assert "already covers these figures" in again["result"]
    with db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM weekly_note").fetchone()["n"] == 1


def _answers(req):
    """The model's templates: one good, one that writes a number (then fixes it), one that keeps adding claims."""
    body = json.loads(req.content[0].removeprefix("<paragraphs>\n").removesuffix("\n</paragraphs>"))
    retry = len(req.content) > 1
    out = []
    for p in body:
        names = [f["placeholder"] for f in p["figures"]]
        records = " ".join(f"{n}." for n in names if n.startswith("{{r"))
        if p["id"] == "EX-JUPITER-190":
            t = f"Ranked {{{{rank}}}} of {{{{count}}}}, with {{{{odds}}}}. The records behind it: {records}"
        elif p["id"] == "EX-SALINE-308":
            t = ("Ranked {{rank}} of {{count}}, with {{odds}}. " + records) if retry else \
                f"Second of 15 on the list, with {{{{odds}}}}. {records}"
        elif p["id"] == "EX-GLXY-163":
            t = f"Ranked {{{{rank}}}} of {{{{count}}}}; spreads widened sharply, with {{{{odds}}}}. {records}"
        else:
            t = f"Ranked {{{{rank}}}} of {{{{count}}}}, with {{{{odds}}}}. {records}"
        out.append({"id": p["id"], "template": t})
    return {"paragraphs": out}


def test_draft_with_a_model_keeps_only_checked_words_then_approval_serves_it():
    seed_list()
    llm, backend = fake_llm(_answers)
    report = note.draft(WEEK, llm=llm)
    assert report["paragraph_writers"] == {"EX-JUPITER-190": "model", "EX-SALINE-308": "model",
                                           "EX-GLXY-163": "code", "EX-APLD-011": "model"}
    assert report["writer"] == "model" and len(backend.requests) == 2
    retry = backend.requests[1].content
    assert "EX-SALINE-308" in retry[-1] and "number words in the prose: second" in retry[-1]
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM weekly_note").fetchone()
    paragraphs = {p["lead"]: p["text"] for p in row["note"]["paragraphs"]}
    assert paragraphs["Project Jupiter Construction loans"].startswith(
        "Ranked 1 of 15, with 63% odds of a 3-point move in 90 days (odds v0, uncalibrated). The records behind it:")
    assert row["checks"]["EX-GLXY-163"]["model_rejected"]  # kept the code template
    assert row["template"]["EX-JUPITER-190"].startswith("Ranked {{rank}} of {{count}}")

    out = decide(row["review_item_id"], "approve", "staff@marden.ai")
    assert out["ok"] and out["note"] == "approved"
    from marden.readmodels.rebuild import rebuild_dirty

    rebuild_dirty()
    with db.transaction() as conn:
        doc = conn.execute("SELECT doc FROM read_model WHERE view = 'risk_list' AND key = %s ORDER BY as_of DESC "
                           "LIMIT 1", (WEEK.isoformat(),)).fetchone()["doc"]
        status = conn.execute("SELECT status FROM weekly_note").fetchone()["status"]
    assert status == "approved"
    assert doc["note"] == row["note"]  # the approved draft, word for word
    assert note.draft(WEEK, llm=llm)["result"].startswith("approved note version 1 already covers")


def test_rejected_draft_keeps_the_code_note_and_redrafts_with_the_reason():
    seed_list()
    note.draft(WEEK)
    with db.transaction() as conn:
        item = conn.execute("SELECT id FROM review_item WHERE kind = 'note'").fetchone()
    assert decide(item["id"], "edit", "staff@marden.ai", edit={"value": "x"})["ok"] is False
    out = decide(item["id"], "reject", "staff@marden.ai", reason="lead with the cushion, not the rank")
    assert out["ok"] and out["note"] == "rejected"
    assert "already covers" in note.draft(WEEK)["result"]  # same figures: no new draft without --redraft
    llm, backend = fake_llm(_answers)
    report = note.draft(WEEK, llm=llm, redraft=True)
    assert report["version"] == 2
    assert "lead with the cushion" in backend.requests[0].content[1]
    with db.transaction() as conn:
        rows = conn.execute("SELECT version, status FROM weekly_note ORDER BY version").fetchall()
    assert [(r["version"], r["status"]) for r in rows] == [(1, "rejected"), (2, "draft")]


def test_no_list_no_draft():
    report = note.draft(WEEK)
    assert report["result"].startswith("no risk list published") and "note_id" not in report


def test_prompt_names_the_rules_and_only_placeholders():
    paragraphs = note.paragraphs_of(LIST["output"])
    req = note_prose.request(paragraphs)
    assert req.role == "writer" and req.tool["name"] == "write_note"
    assert "{{rank}}" in req.content[0] and "Never write a digit" in req.system
