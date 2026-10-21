"""Read model `risk_list` (key: the Monday a list was published, YYYY-MM-DD): GET /api/risk-list?week=,
RiskList (spec 10).

The list is the risk-ranking model's output ('risk_list', subject = the week's Monday; Sunday-night run,
spec 22 "Risk ranking": a rule-based score until the backtest supports a fitted model). It is frozen
when published (spec 10: past weeks are read-only snapshots), so the builder carries the published rows
and note as they are and adds only what the output leaves out:

- `week` = the key and the Sunday after it;
- a row's `name` and `size` from the instrument when the output has none (its identity row and its
  size fact as known on the build day); `price`, `change1w`, `fundMark` and `gap` stay null (n/d)
  when the output has none, since a later price is not what the list was published with;
- `odds` without a value is n/d under 'odds v0' (spec 00-global: uncalibrated until the backtest);
- `move` against the previous published week when the output has none: 'new', 'up' / 'down' by the
  rank change, or 'same';
- `earlierCalls` from the backtest's published cases flagged before the week (model 'track_record').

Rows are ordered by rank. No output, no document: the API answers 503 until the first list exists.
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.instrument_inputs import day, instrument_identity, num
from marden.readmodels.views.instrument_text import billions, display_name
from marden.readmodels.views.monitor_common import (
    Doc,
    evidence_of,
    is_iso_date,
    items_of,
    latest_output_any,
    sourced_of,
    week_end,
)

MODEL = "risk_list"
BACKTEST = "track_record"
ODDS_METHOD = "odds v0"
FUND_WORDS = ("no fund holds it", "traded", "private")
MOVES = ("up", "down", "same", "new")


def risk_weeks(conn: Conn) -> list[str]:
    """Weeks with a published list by the build day (keys: the Monday, YYYY-MM-DD)."""
    rows = conn.execute(
        "SELECT DISTINCT subject_id FROM model_output WHERE model = %s AND as_of <= %s ORDER BY 1",
        (MODEL, day()),
    ).fetchall()
    return [r["subject_id"] for r in rows if is_iso_date(r["subject_id"])]


def output_rows(out: Row | None) -> list[dict[str, Any]]:
    body = out["output"] if out is not None else None
    rows = body.get("rows") if isinstance(body, dict) else body
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _ranks(out: Row | None) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for r in output_rows(out):
        rank = num(r.get("rank"))
        if isinstance(r.get("instrumentId"), str) and rank is not None:
            ranks.setdefault(r["instrumentId"], int(rank))
    return ranks


def move_of(raw: Any, instrument_id: str, rank: int, before: dict[str, int] | None) -> Doc:
    """The published move, else the change against the previous week's list (no list: 'new')."""
    if isinstance(raw, dict) and raw.get("kind") in MOVES:
        out: Doc = {"kind": raw["kind"]}
        if num(raw.get("by")) is not None and raw["kind"] in ("up", "down"):
            out["by"] = int(num(raw["by"]))  # type: ignore[arg-type]
        return out
    if before is None or instrument_id not in before:
        return {"kind": "new"}
    change = before[instrument_id] - rank
    if change == 0:
        return {"kind": "same"}
    return {"kind": "up" if change > 0 else "down", "by": abs(change)}


def size_text(conn: Conn, instrument_id: str) -> str:
    """'$18.0B' from the instrument's size fact; 'n/d' without one."""
    facts = known_facts(conn, "instrument", instrument_id, ["amount_usd", "commitment_usd"])
    row = facts.get("amount_usd") or facts.get("commitment_usd")
    value = billions(row["value"]) if row else None
    if value is None:
        return "n/d"
    return f"${value:,.2f}B" if value < 1 else f"${value:,.1f}B"


def fund_mark(raw: Any) -> float | str | None:
    if isinstance(raw, str) and raw in FUND_WORDS:
        return raw
    return num(raw)


def risk_row(conn: Conn, out: Row, raw: dict[str, Any], before: dict[str, int] | None,
             names: dict[str, str]) -> Doc | None:
    """types.ts RiskRow from a published row, or None without an instrument and a rank."""
    iid, rank = raw.get("instrumentId"), num(raw.get("rank"))
    if not isinstance(iid, str) or not iid or rank is None:
        return None
    raw_odds = raw.get("odds")
    odds = sourced_of(out, raw_odds, "number")
    if not (isinstance(raw_odds, dict) and isinstance(raw_odds.get("methodVersion"), str)):
        odds["methodVersion"] = ODDS_METHOD  # the model's own label until the backtest calibrates it
    return {
        "rank": int(rank),
        "instrumentId": iid,
        "name": raw["name"] if isinstance(raw.get("name"), str) and raw["name"] else names.get(iid, iid),
        "size": raw["size"] if isinstance(raw.get("size"), str) and raw["size"] else size_text(conn, iid),
        "why": items_of(raw.get("why"), evidence_of),
        "price": sourced_of(out, raw["price"], "number") if raw.get("price") is not None else None,
        "change1w": sourced_of(out, raw["change1w"], "number") if raw.get("change1w") is not None else None,
        "fundMark": fund_mark(raw.get("fundMark")),
        "gap": num(raw.get("gap")),
        "odds": odds,
        "move": move_of(raw.get("move"), iid, int(rank), before),
    }


def note_of(raw: Any) -> Doc:
    """RiskList.note: the agent's checked note as published; an empty note until one is drafted."""
    if not isinstance(raw, dict):
        return {"title": "", "paragraphs": []}
    paragraphs = []
    for p in raw.get("paragraphs") or []:
        if isinstance(p, dict) and isinstance(p.get("lead"), str) and isinstance(p.get("text"), str):
            refs = [r for r in p.get("refs") or [] if isinstance(r, str)]
            paragraphs.append({"lead": p["lead"], "text": p["text"], "refs": refs})
    return {"title": raw["title"] if isinstance(raw.get("title"), str) else "", "paragraphs": paragraphs}


def earlier_call(raw: Any) -> Doc | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        return None
    flagged, moved, lead = raw.get("flagged"), raw.get("moved"), num(raw.get("leadDays"))
    if not (is_iso_date(flagged) and is_iso_date(moved)) or lead is None:
        return None
    return {"name": raw["name"], "flagged": flagged, "moved": moved, "leadDays": int(lead)}


def earlier_calls(conn: Conn, out: Row, week: str) -> list[Doc]:
    """The published list's earlier calls, else the backtest's finished cases flagged before the week."""
    body = out["output"] if isinstance(out["output"], dict) else {}
    if isinstance(body.get("earlierCalls"), list):
        return items_of(body["earlierCalls"], earlier_call)
    backtest = latest_output_any(conn, BACKTEST, "all")
    body = backtest["output"] if backtest is not None and isinstance(backtest["output"], dict) else {}
    # The backtest's own earlierCalls are the calls Marden made first (lead > 0); its cases also hold late
    # calls and misses, which are not earlier calls.
    cases = body.get("earlierCalls") if isinstance(body.get("earlierCalls"), list) else body.get("cases")
    calls = [c for c in items_of(cases, earlier_call) if c["flagged"] < week]
    return sorted(calls, key=lambda c: c["flagged"], reverse=True)


def previous_week(conn: Conn, week: str) -> Row | None:
    """The list published for the latest week before `week`."""
    row = conn.execute(
        """SELECT subject_id FROM model_output WHERE model = %s AND subject_id < %s AND as_of <= %s
            ORDER BY subject_id DESC LIMIT 1""",
        (MODEL, week, day()),
    ).fetchone()
    return latest_output_any(conn, MODEL, row["subject_id"]) if row and is_iso_date(row["subject_id"]) else None


@builder("risk_list", keys=risk_weeks)
def build_risk_list(conn: Conn, key: str) -> Doc | None:
    if not is_iso_date(key):
        return None
    out = latest_output_any(conn, MODEL, key)
    if out is None:
        return None
    raw_rows = output_rows(out)
    prev = previous_week(conn, key)
    before = _ranks(prev) if prev is not None else None
    ids = sorted({r["instrumentId"] for r in raw_rows if isinstance(r.get("instrumentId"), str)})
    names = {i["id"]: display_name(i) for i in instrument_identity(conn, ids)} if ids else {}
    rows = [row for raw in raw_rows if (row := risk_row(conn, out, raw, before, names)) is not None]
    rows.sort(key=lambda r: (r["rank"], r["instrumentId"]))
    body = out["output"] if isinstance(out["output"], dict) else {}
    return {
        "week": {"start": key, "end": week_end(key)},
        "rows": rows,
        "note": note_of(body.get("note")),
        "earlierCalls": earlier_calls(conn, out, key),
    }
