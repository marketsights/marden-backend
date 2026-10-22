"""Watchdog check `unexplained_move`: a model output that changed with no input change behind it.

For each headline model, the newest output of every subject (current version, last 14 days) is compared with
its previous output:

    status   site         chain status changed (Holding / Strained / Broken)
    cushion  site         projected completion P50 moved 2 months or more
    price    instrument   Marden price moved 0.5 points or more

A move is explained when any input the model declares (`marden.models.triggers.model_depends`) changed
between that subject's two outputs, either learned between their creation times or dated between their as-of
days (a backfill writes several days at once): a fact published or withdrawn, or valid from a day in between;
an event; a price observation; another model's output it reads (`@model:<name>`); a review decision that
published a fact; a site link decided. Each move is judged on its own window (a change to the subject a week
before its move does not explain it). Inputs are mapped to the model's subjects with the model's own key
functions. When a change in the move's window cannot be attributed (an input every subject reads, or too many
changed subjects to map), that move counts as explained, and the report counts such moves (`broad`): the
check only raises what it can show.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
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


@dataclass
class Change:
    """One input (subject, field or pseudo-field) and when it changed: learned at `ats`, dated `days`."""

    subject_type: str
    subject_id: str
    field: str
    ats: list[datetime] = field(default_factory=list)
    days: list[date] = field(default_factory=list)

    def within(self, lo: datetime, hi: datetime, d_lo: date, d_hi: date) -> bool:
        return any(lo < a <= hi for a in self.ats) or any(d_lo < d <= d_hi for d in self.days)


CHANGES_SQL = """
    SELECT subject_type, subject_id, field, array_agg(DISTINCT at) FILTER (WHERE at IS NOT NULL) AS ats,
           array_agg(DISTINCT d) FILTER (WHERE d IS NOT NULL) AS days
    FROM (
      SELECT subject_type, subject_id, field, learned_at AS at, valid_from AS d FROM fact
       WHERE subject_type = ANY(%(t)s) AND status IN ('published', 'superseded')
         AND ((learned_at > %(lo)s AND learned_at <= %(hi)s) OR (valid_from > %(dlo)s AND valid_from <= %(dhi)s))
      UNION ALL
      SELECT subject_type, subject_id, '@event', learned_at, event_date FROM event
       WHERE subject_type = ANY(%(t)s) AND status IN ('published', 'rejected')
         AND ((learned_at > %(lo)s AND learned_at <= %(hi)s) OR (event_date > %(dlo)s AND event_date <= %(dhi)s))
      UNION ALL
      SELECT 'instrument', instrument_id, '@price', learned_at, obs_date FROM price_obs
       WHERE 'instrument' = ANY(%(t)s)
         AND ((learned_at > %(lo)s AND learned_at <= %(hi)s) OR (obs_date > %(dlo)s AND obs_date <= %(dhi)s))
      UNION ALL
      SELECT subject_type, subject_id, '@model:' || model, created_at, as_of FROM model_output
       WHERE subject_type = ANY(%(t)s)
         AND ((created_at > %(lo)s AND created_at <= %(hi)s) OR (as_of > %(dlo)s AND as_of <= %(dhi)s))
      UNION ALL
      SELECT f.subject_type, f.subject_id, f.field, a.at, NULL::date FROM audit_log a
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
      UNION ALL
      SELECT 'site', site_slug, 'site_link', approved_at, NULL::date FROM site_link
       WHERE 'site' = ANY(%(t)s) AND approved_at > %(lo)s AND approved_at <= %(hi)s
    ) c
    GROUP BY 1, 2, 3"""


def changed_inputs(conn: Conn, types: list[str], lo: datetime, hi: datetime, d_lo: date, d_hi: date,
                   ) -> list[Change]:
    """Every input (pseudo-fields included) that changed in the window, with when it changed."""
    rows = conn.execute(CHANGES_SQL, {"t": types, "lo": lo, "hi": hi, "dlo": d_lo, "dhi": d_hi}).fetchall()
    return [Change(r["subject_type"], r["subject_id"], r["field"], list(r["ats"] or []), list(r["days"] or []))
            for r in rows]


class Reach:
    """Maps changed inputs to the model's subject keys with the model's own key functions (cached)."""

    def __init__(self, conn: Conn, model: str):
        self.conn, self.model = conn, model
        self.deps = triggers.model_dependencies(model)
        self.cache: dict[tuple[int, str], list[str]] = {}

    def keys(self, inputs: list[Change]) -> tuple[set[str], str | None]:
        """The subject keys these inputs reach, or (set(), why) when everything counts as touched."""
        keys: set[str] = set()
        for i, dep in enumerate(self.deps):
            hits = sorted({c.subject_id for c in inputs
                           if c.subject_type == dep.subject_type and c.field.startswith(dep.field_prefix)})
            if not hits:
                continue
            if dep.keys is triggers.everything:
                return set(), (f"an input every {self.model} subject reads changed "
                               f"({dep.subject_type} {dep.field_prefix or '*'})")
            if dep.keys is not triggers.same and len(hits) > MAX_MAPPED:
                return set(), f"{len(hits)} changed {dep.subject_type} subjects: too many to map"
            for sid in hits:
                mapped = self.cache.setdefault((i, sid), [k for k in dep.keys(self.conn, sid) if k is not None])
                if "*" in mapped:
                    return set(), f"{dep.subject_type} {sid} feeds every {self.model} subject"
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
        changes = changed_inputs(conn, types, lo, hi, d_lo, d_hi)
        reach = Reach(conn, model)
        broad: dict[str, int] = {}
        for r in moves:  # each move on its own window: what changed between this subject's two outputs
            m_lo, m_hi = min(r["prev_at"], r["cur_at"]), max(r["prev_at"], r["cur_at"])
            keys, why = reach.keys([c for c in changes if c.within(m_lo, m_hi, r["prev_day"], r["cur_day"])])
            if why:
                broad[why] = broad.get(why, 0) + 1
                continue
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
                 "window": [m_lo.isoformat(), m_hi.isoformat()],
                 "inputs_checked": sorted({f"{d.subject_type}:{d.field_prefix or '*'}"
                                           for d in triggers.model_dependencies(model)})},
                {"type": subject_type, "id": r["subject_id"]}, "high"))
        if broad:  # explained only because an input that cannot be attributed changed in the move's window
            entry["broad"] = sum(broad.values())
            entry["note"] = "; ".join(f"{n} move(s) explained by: {why}" for why, n in sorted(broad.items())[:3])
    return out, report
