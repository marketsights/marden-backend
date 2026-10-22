"""Note writer (spec 22: "Drafts the weekly note from computed numbers only; every figure is filled in by code,
not written by the model"; Sunday night draft, Monday out after approval).

    python -m marden agents note [--week YYYY-MM-DD] [--redraft]

From the week's risk list (model `risk_list`, frozen once published) code builds, for each of the top four
rows, the figures (`Figure`): rank, count, odds, size, Marden price, week change, fund marks and the records
behind the ranking, each as the text code writes and with its record reference. The model writes templates
with {{placeholders}} (note_prose); code checks them (numbers.check_prose: no digit, number word, direction
word or outside name), fills them and checks the filled text (numbers.check_filled). A paragraph that fails
twice keeps the code template ("Ranked {{rank}} of {{count}}, with {{odds}}. {{r1}}. ..."), and without a
model backend every paragraph does (writer 'code', reported as not configured). Every paragraph must use the
rank, count, odds and every record.

The draft is stored (weekly_note: figures, templates, checks, the filled note) and waits for a person in the
review queue (review_item kind 'note'). Approving it makes it the note GET /api/risk-list serves for the week
(read model `risk_list`); rejecting it keeps the list's code-written note until a new draft is approved.
Idempotent: the same list and figures make no new draft; an approved week is never redrafted; a rejected
draft is redrafted only when its inputs change, or with --redraft.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from marden.agents.llm import Llm
from marden.agents.ops import note_prose, runs
from marden.agents.ops.numbers import Figure, check_filled, check_prose, fill, used_figures, vocabulary
from marden.core import db
from marden.core.db import Conn, Row
from marden.core.jsonutil import canonical_json, hash_parts, jsonb
from marden.models.risk_list import week_of
from marden.readmodels.context import business_today

NOTE_PARAGRAPHS = 4
RECORDS = 3
CODE_TEMPLATE = "Ranked {{rank}} of {{count}}, with {{odds}}."
REQUIRED = ("rank", "count", "odds")


def title(week: date) -> str:
    return f"Risk list, week of {week.strftime('%b')} {week.day}, {week.year}"


def source_output(conn: Conn, week: date) -> Row | None:
    """The published risk list for the week (the newest output dated in or before it)."""
    return conn.execute(
        """SELECT id, as_of, created_at, output FROM model_output
           WHERE model = 'risk_list' AND subject_type = 'week' AND subject_id = %s
           ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""", (week.isoformat(),)).fetchone()


def _num(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("value")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def row_figures(row: dict[str, Any], count: int, refs: list[str]) -> list[Figure]:
    """The figures of one ranked row, each written by code, with the record reference it rests on."""
    figs = [Figure("rank", str(int(row["rank"])), "the instrument's rank on this week's list"),
            Figure("count", str(count), "how many instruments this week's list ranks")]
    odds = _num(row.get("odds"))
    odds_text = (f"{round(odds * 100)}% odds of a 3-point move in 90 days (odds v0, uncalibrated)"
                 if odds is not None else "odds n/d")
    figs.append(Figure("odds", odds_text, "the ranking model's odds of a 3-point price move in 90 days"))
    if isinstance(row.get("size"), str) and row["size"] != "n/d":
        figs.append(Figure("size", row["size"], "the instrument's size"))
    if (price := _num(row.get("price"))) is not None:
        figs.append(Figure("price", f"Marden price {price:.1f}", "Marden's model price, in points of par"))
    if (change := _num(row.get("change1w"))) is not None:
        text = ("unchanged on the week" if abs(change) < 0.05 else
                f"{'down' if change < 0 else 'up'} {abs(change):.1f} points on the week")
        figs.append(Figure("change", text, "the Marden price's change over the week"))
    gap, mark = _num(row.get("gap")), row.get("fundMark")
    if gap is not None:
        side = "above" if gap < 0 else "below"
        figs.append(Figure("marks", f"fund marks {abs(gap):.1f} points {side} the Marden price",
                           "where the funds that hold it mark it, against the Marden price"))
    elif isinstance(mark, str) and mark:
        figs.append(Figure("marks", {"traded": "a traded instrument (no fund marks compared)",
                                     "private": "privately held (no fund marks public)"}.get(mark, mark),
                           "who holds it and whether their marks are public"))
    move = row.get("move") or {}
    by = int(move["by"]) if isinstance(move.get("by"), int | float) else None
    places = f"{by} place{'' if by == 1 else 's'}"
    move_text = {"new": "new to the list this week", "same": "at the same rank as last week",
                 "up": f"{places} higher than last week", "down": f"{places} lower than last week"}
    if move.get("kind") in move_text and (move["kind"] in ("new", "same") or by):
        figs.append(Figure("move", move_text[move["kind"]], "its move against last week's list"))
    for j, ev in enumerate((row.get("why") or [])[:RECORDS]):
        if not isinstance(ev, dict) or not ev.get("text"):
            continue
        ref = refs[j] if j < len(refs) else (ev.get("sourceUrl") or None)
        figs.append(Figure(f"r{j + 1}", f"{str(ev['text']).rstrip('.')} ({ev.get('date') or 'n/d'})",
                           f"a record behind the ranking (tag {ev.get('tag') or 'n/d'}, "
                           f"{ev.get('sourceName') or 'source n/d'})", ref))
    return figs


def paragraphs_of(out: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [r for r in out.get("rows") or [] if isinstance(r, dict) and r.get("instrumentId")]
    rows.sort(key=lambda r: r.get("rank") or 0)
    by_lead = {p.get("lead"): p.get("refs") or [] for p in (out.get("note") or {}).get("paragraphs") or []
               if isinstance(p, dict)}
    paragraphs = []
    for row in rows[:NOTE_PARAGRAPHS]:
        refs = [r for r in by_lead.get(row.get("name"), []) if isinstance(r, str)]
        figs = row_figures(row, len(rows), refs)
        paragraphs.append({"id": row["instrumentId"], "lead": str(row.get("name") or row["instrumentId"]),
                           "figures": figs})
    return paragraphs


def code_template(figs: list[Figure]) -> str:
    records = " ".join("{{" + f.name + "}}." for f in figs if re.fullmatch(r"r\d+", f.name))
    return f"{CODE_TEMPLATE} {records}".strip()


def problems_of(template: str, p: dict[str, Any], week_title: str) -> list[str]:
    figs = {f.name: f for f in p["figures"]}
    known = vocabulary(p["lead"], week_title, *(f.text for f in p["figures"]), *(f.meaning for f in p["figures"]))
    out = check_prose(template, figs, known)
    used = {f.name for f in used_figures(template, figs)}
    must = [n for n in figs if n in REQUIRED or re.fullmatch(r"r\d+", n)]
    if missing := [n for n in must if n not in used]:
        out.append(f"figures left out: {', '.join(missing)}")
    if len(re.findall(r"\{\{\s*(\w+)\s*\}\}", template)) != len(used):
        out.append("a placeholder used twice")
    if not out:
        out += check_filled(fill(template, figs), used_figures(template, figs), (p["lead"],))
    return out


def write_templates(llm: Llm | None, paragraphs: list[dict[str, Any]], week_title: str,
                    reviewer: str | None = None) -> dict[str, Any]:
    """{id: {template, writer, problems}} plus the calls made; code templates where the model's fail."""
    result: dict[str, Any] = {"paragraphs": {}, "calls": [], "errors": []}
    pending = {p["id"]: p for p in paragraphs}
    feedback: dict[str, list[str]] | None = None
    for _attempt in range(2):
        if llm is None or not pending:
            break
        answers, call, error = note_prose.ask(llm, list(pending.values()), feedback, reviewer)
        if call is not None:
            result["calls"].append({"call_id": call.call_id, "model": call.model, "cached": call.cached,
                                    "tokens": call.tokens, "cost_usd": call.cost})
        if error:
            result["errors"].append(error)
            break
        feedback = {}
        for pid, p in list(pending.items()):
            template = answers.get(pid)
            problems = problems_of(template, p, week_title) if template else ["no template for this paragraph"]
            if problems:
                feedback[pid] = problems
                result["paragraphs"][pid] = {"template": template, "writer": "rejected", "problems": problems}
            else:
                result["paragraphs"][pid] = {"template": template, "writer": "model", "problems": []}
                del pending[pid]
    for pid, p in pending.items():
        template = code_template(p["figures"])
        problems = problems_of(template, p, week_title)
        if problems:  # a code template that fails its own checks is a bug, never published
            raise ValueError(f"code template for {pid} fails the checks: {problems}")
        rejected = result["paragraphs"].get(pid, {}).get("problems") or []
        result["paragraphs"][pid] = {"template": template, "writer": "code", "problems": [],
                                     "model_rejected": rejected}
    return result


def build_note(paragraphs: list[dict[str, Any]], written: dict[str, Any], week: date) -> dict[str, Any]:
    out = []
    for p in paragraphs:
        figs = {f.name: f for f in p["figures"]}
        template = written["paragraphs"][p["id"]]["template"]
        refs = [f.ref for f in used_figures(template, figs) if f.ref]
        out.append({"lead": p["lead"], "text": fill(template, figs), "refs": list(dict.fromkeys(refs))})
    return {"title": title(week), "paragraphs": out}


def _figures_json(paragraphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": p["id"], "lead": p["lead"],
             "figures": [{"name": f.name, "text": f.text, "meaning": f.meaning, "ref": f.ref} for f in p["figures"]]}
            for p in paragraphs]


def existing(conn: Conn, week: date) -> list[Row]:
    return conn.execute("SELECT * FROM weekly_note WHERE week = %s ORDER BY version DESC", (week,)).fetchall()


def store(conn: Conn, week: date, *, run_id: int, out_row: Row, paragraphs: list[dict[str, Any]],
          written: dict[str, Any], note: dict[str, Any], inputs_hash: str) -> Row:
    """Supersede the open draft, store the new one and open its review item (one transaction)."""
    notes = existing(conn, week)
    version = (notes[0]["version"] + 1) if notes else 1
    writers = {w["writer"] for w in written["paragraphs"].values()}
    writer = "model" if "model" in writers else "code"
    calls = written["calls"]
    for old in notes:
        if old["status"] == "draft":
            conn.execute("UPDATE weekly_note SET status = 'superseded', reason = %s WHERE id = %s",
                         (f"superseded by version {version}", old["id"]))
            conn.execute("""UPDATE review_item SET status = 'rejected', decided_by = 'agent:note_writer',
                                   decided_at = now() WHERE id = %s AND status = 'open'""", (old["review_item_id"],))
    checks = {pid: {k: v for k, v in w.items() if k != "template"} for pid, w in written["paragraphs"].items()}
    checks["errors"] = written["errors"]
    row = conn.execute(
        """INSERT INTO weekly_note (week, version, writer, note, figures, template, checks, source_output_id,
                                    inputs_hash, model, call_id, run_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (week, version, writer, jsonb(note), jsonb(_figures_json(paragraphs)),
         jsonb({pid: w["template"] for pid, w in written["paragraphs"].items()}), jsonb(checks), out_row["id"],
         inputs_hash, calls[-1]["model"] if calls and writer == "model" else None,
         calls[-1]["call_id"] if calls and writer == "model" else None, run_id)).fetchone()
    assert row is not None
    item = conn.execute(
        """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
           VALUES ('note', 'weekly_note', %s, %s, %s, %s) RETURNING id""",
        (str(row["id"]), f"Weekly note draft: {note['title']} (version {version})",
         "the weekly note goes out only after a person approves it (spec 22)",
         jsonb({"week": week.isoformat(), "version": version, "writer": writer, "note": note,
                "paragraph_writers": {pid: w["writer"] for pid, w in written["paragraphs"].items()},
                "source_output_id": out_row["id"]}))).fetchone()
    assert item is not None
    conn.execute("UPDATE weekly_note SET review_item_id = %s WHERE id = %s", (item["id"], row["id"]))
    return conn.execute("SELECT * FROM weekly_note WHERE id = %s", (row["id"],)).fetchone()  # type: ignore[return-value]


def draft(week: date | None = None, *, llm: Llm | None = None, trigger: str = "manual",
          redraft: bool = False) -> dict[str, Any]:
    """Draft the week's note (module docstring); returns the run report."""
    week = week_of(week or business_today())
    run_id = runs.start("note_writer", trigger=trigger)
    report: dict[str, Any] = {"run_id": run_id, "week": week.isoformat()}
    try:
        with db.transaction() as conn:
            out_row = source_output(conn, week)
            notes = existing(conn, week)
        if out_row is None or not isinstance(out_row["output"], dict):
            report["result"] = "no risk list published for this week yet: nothing to draft"
            runs.finish(run_id, "ok", report=report, model_status="not_used")
            return report
        paragraphs = paragraphs_of(out_row["output"])
        week_title = title(week)
        inputs_hash = hash_parts("weekly_note", week.isoformat(), out_row["id"],
                                 canonical_json(_figures_json(paragraphs)), note_prose.SYSTEM)
        approved = next((n for n in notes if n["status"] == "approved"), None)
        same = next((n for n in notes if n["inputs_hash"] == inputs_hash and n["status"] in ("draft", "rejected")),
                    None)
        if approved is not None or (same is not None and (same["status"] == "draft" or not redraft)):
            hit = approved or same
            assert hit is not None
            report |= {"result": f"{hit['status']} note version {hit['version']} already covers these figures",
                       "note_id": hit["id"], "review_item_id": hit["review_item_id"]}
            runs.finish(run_id, "ok", report=report, model_status="not_used")
            return report
        model, status = runs.writer_llm(llm)
        rejected = next((n for n in notes if n["status"] == "rejected"), None)
        written = write_templates(model, paragraphs, week_title, rejected["reason"] if rejected else None)
        note = build_note(paragraphs, written, week)
        with db.transaction() as conn:
            row = store(conn, week, run_id=run_id, out_row=out_row, paragraphs=paragraphs, written=written,
                        note=note, inputs_hash=inputs_hash)
        report |= {"result": "draft waiting for approval", "note_id": row["id"], "version": row["version"],
                   "writer": row["writer"], "review_item_id": row["review_item_id"],
                   "paragraph_writers": {pid: w["writer"] for pid, w in written["paragraphs"].items()},
                   "calls": written["calls"], "errors": written["errors"], "note": note}
        model_status = status.kind if model is not None else "not_configured"
        if model is None:
            report["model"] = f"not configured ({status.detail}): every paragraph is the code template"
        partial = bool(written["errors"]) and model is not None
        runs.finish(run_id, "partial" if partial else "ok", report=report, model_status=model_status)
        return report
    except Exception as exc:
        runs.finish(run_id, "failed", report=report, model_status="not_used", detail=f"{type(exc).__name__}: {exc}")
        raise


def approved_note(conn: Conn, week: str, on_or_before: date | None = None) -> dict[str, Any] | None:
    """The approved note for a week (RiskList.note), as approved by the given day (US Eastern)."""
    row = conn.execute(
        """SELECT note FROM weekly_note WHERE week = %s AND status = 'approved'
             AND (%s::date IS NULL OR (decided_at AT TIME ZONE 'America/New_York')::date <= %s::date)
           ORDER BY decided_at DESC LIMIT 1""", (week, on_or_before, on_or_before)).fetchone()
    return row["note"] if row is not None else None
