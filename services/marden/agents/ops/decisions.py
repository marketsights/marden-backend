"""Review-queue decisions on the operations agents' items (called from marden.agents.review.apply_decision).

    note   (ref_type 'weekly_note')   approve: the draft becomes the week's note (the risk_list read model is
                                      marked dirty, so GET /api/risk-list serves it within seconds);
                                      reject (reason required): the draft is closed and the list keeps its
                                      code-written note; the note writer drafts again when the figures change
                                      (or `agents note --redraft`), with the reason as guidance on wording
    issue  (ref_type 'watchdog:<check>')  approve: resolved or acknowledged; reject (reason): not an issue.
                                      The watchdog raises a new item only if the evidence changes.
Notes and issues are never edited: a note's figures are code's, and an issue's evidence is what code found.
"""

from __future__ import annotations

from typing import Any

from marden.core.checks import ValidationError
from marden.core.db import Conn
from marden.readmodels.deps import mark_dirty

KINDS = frozenset({"note", "issue"})


def decide_ops(conn: Conn, item: dict[str, Any], action: str, by: str, reason: str | None) -> dict[str, Any]:
    if item["kind"] == "note" and item["ref_type"] == "weekly_note":
        return _note(conn, int(item["ref_id"]), action, by, reason)
    if item["kind"] == "issue" and str(item["ref_type"]).startswith("watchdog:"):
        if action == "edit":
            raise ValidationError("watchdog issues are resolved (approve) or dismissed (reject), not edited")
        return {"issue": "resolved" if action == "approve" else "dismissed", "check": item["ref_type"]}
    raise ValidationError(f"no decision rule for review items of kind {item['kind']} / {item['ref_type']}")


def _note(conn: Conn, note_id: int, action: str, by: str, reason: str | None) -> dict[str, Any]:
    note = conn.execute("SELECT * FROM weekly_note WHERE id = %s FOR UPDATE", (note_id,)).fetchone()
    if note is None:
        raise ValidationError("weekly note not found")
    if note["status"] != "draft":
        raise ValidationError(f"this note is {note['status']}, not a draft")
    if action == "edit":
        raise ValidationError("notes are approved or rejected, not edited: every figure is code's; reject with a "
                              "reason and the note writer drafts again")
    week = note["week"].isoformat()
    if action == "approve":
        conn.execute("""UPDATE weekly_note SET status = 'superseded', reason = %s
                        WHERE week = %s AND status = 'approved'""", (f"superseded by version {note['version']}",
                                                                      note["week"]))
        conn.execute("""UPDATE weekly_note SET status = 'approved', decided_by = %s, decided_at = now(), reason = %s
                        WHERE id = %s""", (by, reason, note_id))
        mark_dirty("risk_list", week, conn=conn, reason=f"weekly_note:{note_id} approved")
        return {"note": "approved", "week": week, "note_id": note_id, "version": note["version"]}
    conn.execute("""UPDATE weekly_note SET status = 'rejected', decided_by = %s, decided_at = now(), reason = %s
                    WHERE id = %s""", (by, reason, note_id))
    return {"note": "rejected", "week": week, "note_id": note_id, "version": note["version"]}
