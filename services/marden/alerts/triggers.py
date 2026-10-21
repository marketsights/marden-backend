"""What the engine evaluates: published events, rating facts and model-output changes in a date window.

    for trigger in event_triggers(conn, start, today): ...
    for trigger in model_triggers(conn, start, today): ...

Events: published, not a scheduled catalyst, never evaluated (alert_trigger), dated in the window, or dated up
to LATE_DAYS back when Marden learned of it after the engine first ran (an 8-K filed four business days after
the event, a candidate approved in the review queue days later). The engine's first evaluation is the line
between history (rows loaded before alerts existed are never news) and news.
Rating facts: a published `rating` / `rating_summary` that differs from the subject's previous one; the same
window and late rule.
Model outputs (status, cushion, price, mark_gap): model_output keeps one row per subject and as-of day and
replaces it on a rerun, so the engine keeps the last output it evaluated per subject (alert_state) as the
"before" of the next change. A subject the engine has not seen starts from its newest output dated before
the window (that output is the baseline, not news). Outputs dated before the evaluated one (a backtest run
for a past day) are never news.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from marden.alerts import rules
from marden.alerts.fanout import ref_url, subject_name
from marden.core.db import Conn, Row
from marden.core.jsonutil import hash_parts

RATING_FIELDS = ("rating", "rating_summary")
SCHEDULED_KINDS = ("catalyst",)
MODELS = ("status", "cushion", "price", "mark_gap")
LATE_DAYS = 14  # an event or rating learned (or approved) late still reaches watchers when dated this recently


@dataclass
class StateUpdate:
    model: str
    subject_type: str
    subject_id: str
    as_of: date
    output_hash: str
    state: dict[str, Any]


@dataclass
class Trigger:
    key: str
    kind: str  # event | fact | model
    subject_type: str
    subject_id: str
    occurred_on: date
    findings: list[rules.Finding] = field(default_factory=list)
    material: bool = False
    event_id: int | None = None
    coalesce: bool = False  # events: a repeat of the same matter within 7 days joins the earlier alert
    state: StateUpdate | None = None

    @property
    def codes(self) -> list[str]:
        return sorted({c for f in self.findings for c in f.codes})


# ------------------------------------------------------------------ late arrivals
def engine_started(conn: Conn) -> datetime | None:
    """When the engine first evaluated anything (a backfill counts): rows learned after it are news."""
    row = conn.execute("SELECT min(evaluated_at) AS at FROM alert_trigger").fetchone()
    return row["at"] if row else None


def window(conn: Conn, start: date, today: date) -> dict[str, Any]:
    """Query parameters: the scan window, and the late window with the line between history and news (no
    line before the engine's first evaluation: then nothing older than the window is evaluated)."""
    return {"start": start, "today": today, "late": min(start, today - timedelta(days=LATE_DAYS)),
            "since": engine_started(conn)}


# ------------------------------------------------------------------ events
def event_triggers(conn: Conn, start: date, today: date) -> Iterator[Trigger]:
    rows = conn.execute(
        """SELECT e.* FROM event e
           WHERE e.status = 'published' AND e.kind <> ALL(%(scheduled)s)
             AND (e.event_date BETWEEN %(start)s AND %(today)s
                  OR (e.event_date BETWEEN %(late)s AND %(today)s AND e.learned_at >= %(since)s))
             AND NOT EXISTS (SELECT 1 FROM alert_trigger t WHERE t.trigger_key = 'event:' || e.id)
           ORDER BY e.event_date, e.id""",
        {**window(conn, start, today), "scheduled": list(SCHEDULED_KINDS)},
    ).fetchall()
    for event in rows:
        tab = "opposition" if event["kind"] == "opposition" else "events"
        name = subject_name(conn, event["subject_type"], event["subject_id"])
        finding = rules.event_finding(event, name, ref_url(conn, event["subject_type"], event["subject_id"], tab))
        yield Trigger(
            key=f"event:{event['id']}", kind="event", subject_type=event["subject_type"],
            subject_id=event["subject_id"], occurred_on=event["event_date"], event_id=event["id"],
            findings=[finding] if finding else [], material=finding is not None, coalesce=True,
        )


# ------------------------------------------------------------------ rating facts
def fact_triggers(conn: Conn, start: date, today: date) -> Iterator[Trigger]:
    rows = conn.execute(
        """SELECT f.* FROM fact f
           WHERE f.status = 'published' AND f.field = ANY(%(fields)s)
             AND (f.valid_from BETWEEN %(start)s AND %(today)s
                  OR (f.valid_from BETWEEN %(late)s AND %(today)s AND f.learned_at >= %(since)s))
             AND NOT EXISTS (SELECT 1 FROM alert_trigger t WHERE t.trigger_key = 'fact:' || f.id)
           ORDER BY f.valid_from, f.id""",
        {**window(conn, start, today), "fields": list(RATING_FIELDS)},
    ).fetchall()
    for fact in rows:
        prior = conn.execute(
            """SELECT * FROM fact WHERE subject_type = %s AND subject_id = %s AND field = %s AND id <> %s
                 AND status IN ('published', 'superseded') AND (valid_from, id) < (%s, %s)
               ORDER BY valid_from DESC, id DESC LIMIT 1""",
            (fact["subject_type"], fact["subject_id"], fact["field"], fact["id"], fact["valid_from"], fact["id"]),
        ).fetchone()
        name = subject_name(conn, fact["subject_type"], fact["subject_id"])
        finding = rules.rating_finding(name, ref_url(conn, fact["subject_type"], fact["subject_id"]), fact, prior)
        yield Trigger(
            key=f"fact:{fact['id']}", kind="fact", subject_type=fact["subject_type"], subject_id=fact["subject_id"],
            occurred_on=fact["valid_from"], findings=[finding] if finding else [], material=finding is not None,
        )


# ------------------------------------------------------------------ model outputs
def _method(row: Row) -> str:
    """'status' + '0.1' -> 'status v0.1'; a version already naming its model ('cushion v0.1') as is."""
    version = str(row["version"])
    return version if version.startswith(row["model"]) else f"{row['model']} v{version}"


def _value(v: Any) -> Any:
    return v.get("value") if isinstance(v, dict) else v


def extract(row: Row) -> dict[str, Any]:
    """The part of a model output the alert rules read (its hash says whether anything changed)."""
    out, model = row["output"] or {}, row["model"]
    if model == "status":
        links = [{k: link.get(k) for k in ("role", "party", "status", "ruleCode", "reason", "fact")}
                 for link in out.get("chain") or [] if isinstance(link, dict)]
        return {"status": out.get("status"), "chain": links, "methodVersion": _method(row)}
    if model == "cushion":
        p = out.get("projection") or {}
        return {"cushion_months": _value(p.get("cushionMonths")), "p50": p.get("p50"),
                "outside": p.get("outsideDate"), "outside_assumed": p.get("outsideDateAssumed"),
                "methodVersion": _method(row)}
    if model == "price":
        band = out.get("band") or {}
        return {"price": out.get("price"), "change1w": out.get("change1w"), "tier": out.get("tier"),
                "band": [band.get("low"), band.get("high")] if band.get("low") is not None else None,
                "methodVersion": out.get("methodVersion") or _method(row)}
    if model == "mark_gap":
        marks = [{k: h.get(k) for k in ("name", "filing", "filingDate", "lastMark", "gapPts", "filingUrl")}
                 for h in out.get("holders") or [] if isinstance(h, dict)]
        return {"marks": sorted(marks, key=lambda m: (m.get("filingDate") or "", m.get("name") or "")),
                "marden_price": _value(out.get("mardenPrice")), "methodVersion": _method(row)}
    raise ValueError(f"no alert rules for model {model!r}")


def _state_hash(model: str, state: dict[str, Any]) -> str:
    core = {k: v for k, v in state.items() if k not in ("refs", "as_of")}
    return hash_parts("alert-state", model, core)


def _findings(conn: Conn, model: str, subject_type: str, subject_id: str, before: dict[str, Any],
              after: dict[str, Any], as_of: date, before_as_of: date) -> list[rules.Finding]:
    name = subject_name(conn, subject_type, subject_id)
    if model == "status":
        return rules.status_findings(name, subject_id, before, after, as_of, before_as_of)
    if model == "cushion":
        finding = rules.cushion_finding(name, subject_id, before, after, as_of, before_as_of)
        return [finding] if finding else []
    if model == "price":
        found, refs = rules.price_findings(name, subject_id, before.get("refs") or {}, after, as_of)
        after["refs"] = refs
        return found
    finding = rules.mark_finding(name, subject_id, before, after, as_of)
    return [finding] if finding else []


def _initial_refs(state: dict[str, Any], as_of: date) -> dict[str, Any]:
    price = state.get("price")
    if isinstance(price, int | float) and not isinstance(price, bool):
        return {str(p): [price, as_of.isoformat()] for p in rules.THRESHOLDS}
    return {}


def model_triggers(conn: Conn, start: date, today: date, models: tuple[str, ...] = MODELS) -> Iterator[Trigger]:
    for model in models:
        subjects = conn.execute(
            """SELECT DISTINCT subject_type, subject_id FROM model_output
               WHERE model = %s AND as_of BETWEEN %s AND %s ORDER BY 1, 2""",
            (model, start, today),
        ).fetchall()
        for subj in subjects:
            yield from _subject_changes(conn, model, subj["subject_type"], subj["subject_id"], start, today)


def _subject_changes(conn: Conn, model: str, subject_type: str, subject_id: str, start: date,
                     today: date) -> Iterator[Trigger]:
    kept = conn.execute(
        "SELECT * FROM alert_state WHERE model = %s AND subject_type = %s AND subject_id = %s",
        (model, subject_type, subject_id),
    ).fetchone()
    prev: dict[str, Any] | None = None
    prev_as_of: date | None = None
    prev_hash: str | None = None
    if kept is not None:
        prev, prev_as_of, prev_hash = dict(kept["state"]), kept["as_of"], kept["output_hash"]
        first = prev_as_of
    else:
        base = conn.execute(
            """SELECT * FROM model_output WHERE model = %s AND subject_type = %s AND subject_id = %s AND as_of < %s
               ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
            (model, subject_type, subject_id, start),
        ).fetchone()
        if base is not None:
            prev, prev_as_of = extract(base), base["as_of"]
            prev_hash = _state_hash(model, prev)
            if model == "price":
                prev["refs"] = _initial_refs(prev, prev_as_of)
        first = start
    outputs = conn.execute(
        """SELECT DISTINCT ON (as_of) * FROM model_output
           WHERE model = %s AND subject_type = %s AND subject_id = %s AND as_of BETWEEN %s AND %s
           ORDER BY as_of, created_at DESC, id DESC""",
        (model, subject_type, subject_id, first, today),
    ).fetchall()
    for row in outputs:
        after = extract(row)
        digest = _state_hash(model, after)
        if prev is not None and digest == prev_hash:
            continue  # the same output again (or the evaluated one): nothing changed
        key = f"model:{model}:{subject_type}:{subject_id}:{row['as_of'].isoformat()}:{digest[:16]}"
        if prev is None or prev_as_of is None:
            if model == "price":
                after["refs"] = _initial_refs(after, row["as_of"])
            found: list[rules.Finding] = []
        else:
            found = _findings(conn, model, subject_type, subject_id, prev, after, row["as_of"], prev_as_of)
            if model == "price" and "refs" not in after:
                after["refs"] = prev.get("refs") or {}
        yield Trigger(
            key=key, kind="model", subject_type=subject_type, subject_id=subject_id, occurred_on=row["as_of"],
            findings=found, material=bool(found),
            state=StateUpdate(model, subject_type, subject_id, row["as_of"], digest,
                              {**after, "as_of": row["as_of"].isoformat()}),
        )
        prev, prev_as_of, prev_hash = after, row["as_of"], digest
