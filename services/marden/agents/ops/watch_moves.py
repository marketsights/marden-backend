"""Watchdog check `unexplained_move`: a model output that changed with no input change behind it.

For each headline model, the newest output of every subject (current version, last 14 days) is compared with
its previous output:

    status   site         chain status changed (Holding / Strained / Broken)
    cushion  site         projected completion P50 moved 2 months or more
    price    instrument   Marden price moved 0.5 points or more

A move is explained when any input the model declares (`marden.models.triggers.model_depends`) changed
between the two outputs, either learned between their creation times or dated between their as-of days (a
backfill writes several days at once): a fact published or withdrawn, or valid from a day in between; an
event; a price observation; another model's output it reads (`@model:<name>`); a review decision that
published a fact; a site link decided. Inputs are mapped to the model's subjects with the model's own key
functions. When a change cannot be attributed (an input every subject reads, or too many changed subjects to
map), every move of that model counts as explained: the check only raises what it can show.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from marden.agents.ops.watch_checks import Issue, _num
from marden.core.db import Conn
from marden.models import triggers

LOOKBACK_DAYS = 14
MAX_MAPPED = 3000


def _month(v: Any) -> int | None:
    if isinstance(v, str) and len(v) >= 7 and v[:4].isdigit() and v[5:7].isdigit():
        return int(v[:4]) * 12 + int(v[5:7])
    return None


def _status(o: dict[str, Any]) -> Any:
    return o.get("status") if isinstance(o.get("status"), str) else None


def _p50(o: dict[str, Any]) -> Any:
    return ((o.get("projection") or {}).get("p50")) if isinstance(o.get("projection"), dict) else None


def _price(o: dict[str, Any]) -> Any:
    return _num(o.get("price"))


def _moved_status(a: Any, b: Any) -> bool:
    return a is not None and b is not None and a != b


def _moved_months(a: Any, b: Any) -> bool:
    ma, mb = _month(a), _month(b)
    return ma is not None and mb is not None and abs(ma - mb) >= 2


def _moved_points(a: Any, b: Any) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) >= 0.5


HEADLINES: dict[str, tuple[str, str, Callable[[dict[str, Any]], Any], Callable[[Any, Any], bool]]] = {
    "status": ("site", "chain status", _status, _moved_status),
    "cushion": ("site", "completion P50", _p50, _moved_months),
    "price": ("instrument", "Marden price", _price, _moved_points),
}


def pairs(conn: Conn, model: str, today: date) -> list[dict[str, Any]]:
    """The newest output per subject and the one before it (same, current version)."""
    return conn.execute(
        """WITH v AS (SELECT version FROM model_output WHERE model = %(m)s ORDER BY created_at DESC LIMIT 1),
                o AS (SELECT DISTINCT ON (subject_id, as_of) id, subject_type, subject_id, as_of, output, created_at
                      FROM model_output WHERE model = %(m)s AND version = (SELECT version FROM v)
                        AND as_of >= %(since)s::date
                      ORDER BY subject_id, as_of, created_at DESC, id DESC),
                cur AS (SELECT DISTINCT ON (subject_id) * FROM o ORDER BY subject_id, as_of DESC)
           SELECT c.subject_type, c.subject_id, c.id AS cur_id, c.as_of AS cur_day, c.output AS cur,
                  c.created_at AS cur_at, p.id AS prev_id, p.as_of AS prev_day, p.output AS prev,
                  p.created_at AS prev_at
           FROM cur c JOIN LATERAL (SELECT * FROM o WHERE o.subject_id = c.subject_id AND o.as_of < c.as_of
                                    ORDER BY o.as_of DESC LIMIT 1) p ON true
           ORDER BY c.subject_id""",
        {"m": model, "since": date.fromordinal(today.toordinal() - LOOKBACK_DAYS)}).fetchall()


def changed_inputs(conn: Conn, types: list[str], lo: datetime, hi: datetime, d_lo: date, d_hi: date,
                   ) -> list[tuple[str, str, str]]:
    """(subject_type, subject_id, field) of every input that changed in the window, pseudo-fields included."""
    rows = conn.execute(
        """SELECT DISTINCT subject_type, subject_id, field FROM fact
            WHERE subject_type = ANY(%(t)s) AND status IN ('published', 'superseded')
              AND ((learned_at > %(lo)s AND learned_at <= %(hi)s) OR (valid_from > %(dlo)s AND valid_from <= %(dhi)s))
           UNION
           SELECT DISTINCT subject_type, subject_id, '@event' FROM event
            WHERE subject_type = ANY(%(t)s) AND status IN ('published', 'rejected')
              AND ((learned_at > %(lo)s AND learned_at <= %(hi)s) OR (event_date > %(dlo)s AND event_date <= %(dhi)s))
           UNION
           SELECT DISTINCT 'instrument', instrument_id, '@price' FROM price_obs
            WHERE 'instrument' = ANY(%(t)s)
              AND ((learned_at > %(lo)s AND learned_at <= %(hi)s) OR (obs_date > %(dlo)s AND obs_date <= %(dhi)s))
           UNION
           SELECT DISTINCT subject_type, subject_id, '@model:' || model FROM model_output
            WHERE subject_type = ANY(%(t)s)
              AND ((created_at > %(lo)s AND created_at <= %(hi)s) OR (as_of > %(dlo)s AND as_of <= %(dhi)s))
           UNION
           SELECT DISTINCT f.subject_type, f.subject_id, f.field FROM audit_log a
             CROSS JOIN LATERAL (SELECT x::bigint AS id FROM jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(a.meta->'published') = 'array' THEN a.meta->'published' ELSE '[]' END
                 || CASE WHEN jsonb_typeof(a.meta->'released_facts') = 'array' THEN a.meta->'released_facts'
                         ELSE '[]' END
                 || CASE WHEN a.meta ? 'fact_id' THEN jsonb_build_array(a.meta->'fact_id') ELSE '[]' END
                 || CASE WHEN a.target LIKE 'fact:%%' THEN jsonb_build_array(split_part(a.target, ':', 2))
                         ELSE '[]' END) x
                 WHERE x ~ '^[0-9]+$') ids
             JOIN fact f ON f.id = ids.id
            WHERE a.at > %(lo)s AND a.at <= %(hi)s
              AND (a.action LIKE 'review.%%' OR a.action = 'fact.withdraw') AND f.subject_type = ANY(%(t)s)
           UNION
           SELECT 'site', site_slug, 'site_link' FROM site_link
            WHERE 'site' = ANY(%(t)s) AND approved_at > %(lo)s AND approved_at <= %(hi)s""",
        {"t": types, "lo": lo, "hi": hi, "dlo": d_lo, "dhi": d_hi}).fetchall()
    return [(r["subject_type"], r["subject_id"], r["field"]) for r in rows]


def touched(conn: Conn, model: str, inputs: list[tuple[str, str, str]]) -> tuple[set[str], str | None]:
    """The model's subject keys these inputs reach, or (set(), why) when everything counts as touched."""
    keys: set[str] = set()
    cache: dict[tuple[int, str], list[str]] = {}
    deps = triggers.model_dependencies(model)
    for i, dep in enumerate(deps):
        hits = sorted({sid for st, sid, fld in inputs if st == dep.subject_type and fld.startswith(dep.field_prefix)})
        if not hits:
            continue
        if dep.keys is triggers.everything:
            return set(), f"an input every {model} subject reads changed ({dep.subject_type} {dep.field_prefix or '*'})"
        if dep.keys is not triggers.same and len(hits) > MAX_MAPPED:
            return set(), f"{len(hits)} changed {dep.subject_type} subjects: too many to map"
        for sid in hits:
            mapped = cache.setdefault((i, sid), [k for k in dep.keys(conn, sid) if k is not None])
            if "*" in mapped:
                return set(), f"{dep.subject_type} {sid} feeds every {model} subject"
            keys.update(mapped)
    return keys, None


def unexplained_moves(conn: Conn, today: date) -> tuple[list[Issue], dict[str, Any]]:
    out: list[Issue] = []
    report: dict[str, Any] = {}
    for model, (subject_type, label, headline, moved) in HEADLINES.items():
        rows = pairs(conn, model, today)
        moves = [r for r in rows if moved(headline(r["prev"] or {}), headline(r["cur"] or {}))]
        entry: dict[str, Any] = {"compared": len(rows), "moved": len(moves), "unexplained": 0}
        report[model] = entry
        if not moves:
            continue
        if not triggers.model_dependencies(model):
            entry["note"] = f"no inputs declared for {model}: moves not checked"
            continue
        lo = min(min(r["prev_at"], r["cur_at"]) for r in moves)
        hi = max(max(r["prev_at"], r["cur_at"]) for r in moves)
        d_lo, d_hi = min(r["prev_day"] for r in moves), max(r["cur_day"] for r in moves)
        types = sorted({d.subject_type for d in triggers.model_dependencies(model)})
        keys, everything = touched(conn, model, changed_inputs(conn, types, lo, hi, d_lo, d_hi))
        if everything:
            entry["note"] = f"every move explained: {everything}"
            continue
        for r in moves:
            if r["subject_id"] in keys:
                continue
            a, b = headline(r["prev"] or {}), headline(r["cur"] or {})
            entry["unexplained"] += 1
            out.append(Issue(
                "unexplained_move", f"{model}:{r['subject_id']}:{r['cur_day'].isoformat()}",
                f"{r['subject_id']}: {label} moved from {a} to {b} ({r['prev_day']:%b %d} to {r['cur_day']:%b %d}) "
                "with no input change",
                {"model": model, "subject_type": subject_type, "subject_id": r["subject_id"],
                 "previous": {"output_id": r["prev_id"], "as_of": r["prev_day"].isoformat(), "value": a},
                 "current": {"output_id": r["cur_id"], "as_of": r["cur_day"].isoformat(), "value": b},
                 "window": [lo.isoformat(), hi.isoformat()],
                 "inputs_checked": sorted({f"{d.subject_type}:{d.field_prefix or '*'}"
                                           for d in triggers.model_dependencies(model)})},
                {"type": subject_type, "id": r["subject_id"]}, "high"))
    return out, report
