"""Watchdog checks on values: sums that do not add, sources that contradict each other, screens that disagree.

Code computes every check; each finding is an `Issue` with its evidence (the values and where they are).

    sums            a site's debt tranches against its debt facility (they must add to its size or to its
                    drawn amount, within 0.5%); a facility drawn beyond its size; a payer's site debt
                    total against the sum of its sites' debt (the payer screen)
    contradiction   the value a screen shows is reported (R) while a filed (F) value of the same field and
                    type, dated no more than 180 days before it, says something else (filed beats reported:
                    spec 22 layer 4); two producers publishing different values for the same as-of date
    screens         the same value on two screens of the same day: a site's status, cushion and debt on the
                    Sites list against Site detail; an instrument's Marden price, chain status and size on
                    the Instruments list against Instrument detail
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from marden.core.db import Conn

SUM_TOLERANCE = 0.005
CONFLICT_DAYS = 180
KEY_FIELDS = ("iso", "utility", "power_utility", "status", "lease_tenant", "tenant", "end_user", "developer",
              "maturity", "outside_date", "rent_start", "expected_cod", "energization_date", "rating")
SUBJECT_TYPES = ("site", "instrument", "entity", "campus", "plant", "obligation", "building")


@dataclass
class Issue:
    check: str
    key: str  # stable identity of what is wrong (not of its values)
    title: str  # code-written; a model may only rephrase it
    evidence: dict[str, Any]
    subject: dict[str, str] | None = None
    severity: str = "medium"
    extra: dict[str, Any] = field(default_factory=dict)


def _num(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("value")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _close(a: float, b: float, tol: float = SUM_TOLERANCE) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1.0)


def _b(usd: float) -> str:
    return f"${usd / 1e9:,.2f}B"


# ---------------------------------------------------------------- sums
def tranche_sums(conn: Conn) -> list[Issue]:
    rows = conn.execute(
        """SELECT t.subject_id, t.id AS tranches_fact, t.value AS tranches, d.id AS facility_fact, d.value AS facility
           FROM fact_current t JOIN fact_current d ON d.subject_type = 'site' AND d.subject_id = t.subject_id
                                                  AND d.field = 'debt_facility'
           WHERE t.subject_type = 'site' AND t.field = 'tranches' AND jsonb_typeof(t.value) = 'array'
             AND jsonb_typeof(d.value) = 'object'
           ORDER BY t.subject_id""").fetchall()
    out = []
    for r in rows:
        debt = [x for x in r["tranches"] if isinstance(x, dict) and x.get("instrument_id")]
        amounts = [_num(x.get("amount_usd")) for x in debt]
        size, drawn = _num(r["facility"].get("size_usd")), _num(r["facility"].get("drawn_usd"))
        ev = {"tranches_fact": r["tranches_fact"], "facility_fact": r["facility_fact"], "size_usd": size,
              "drawn_usd": drawn, "tranches": [{"name": x.get("name"), "instrument_id": x.get("instrument_id"),
                                                "amount_usd": a} for x, a in zip(debt, amounts, strict=True)]}
        if size is not None and drawn is not None and drawn > size * (1 + SUM_TOLERANCE):
            out.append(Issue("sums", f"site:{r['subject_id']}:drawn_over_size",
                             f"{r['subject_id']}: facility drawn {_b(drawn)} exceeds its size {_b(size)}", ev,
                             {"type": "site", "id": r["subject_id"]}, "high"))
        if not debt or any(a is None for a in amounts) or size is None:
            continue
        total = sum(a for a in amounts if a is not None)
        if _close(total, size) or (drawn is not None and _close(total, drawn)):
            continue
        ev["tranche_total_usd"] = total
        against = f"size {_b(size)}" + (f" or drawn {_b(drawn)}" if drawn is not None else "")
        out.append(Issue("sums", f"site:{r['subject_id']}:tranches_vs_facility",
                         f"{r['subject_id']}: debt tranches add to {_b(total)}, not the facility {against}", ev,
                         {"type": "site", "id": r["subject_id"]}))
    return out


def payer_sums(conn: Conn) -> list[Issue]:
    rows = conn.execute(
        """SELECT DISTINCT ON (key) key, as_of, doc FROM read_model WHERE view = 'payer'
           ORDER BY key, as_of DESC""").fetchall()
    out = []
    for r in rows:
        doc = r["doc"] or {}
        sites = [s for s in doc.get("sites") or [] if isinstance(s, dict)]
        parts = [_num(s.get("debtB")) for s in sites]
        total = _num((doc.get("kpis") or {}).get("siteDebtB"))
        known = [p for p in parts if p is not None]
        summed = round(sum(known), 6)
        if total is None and summed == 0:
            continue
        if total is not None and len(known) == len(parts) and abs(total - summed) <= 0.0005:
            continue
        ev = {"view": "payer", "key": r["key"], "as_of": r["as_of"].isoformat(), "siteDebtB": total,
              "sites": [{"slug": s.get("slug"), "debtB": p} for s, p in zip(sites, parts, strict=True)],
              "sum_debtB": summed}
        shown = "n/d" if total is None else f"${total:,.3f}B"
        out.append(Issue("sums", f"payer:{r['key']}:site_debt",
                         f"Payer {r['key']}: site debt shown {shown}, its sites add to ${summed:,.3f}B", ev,
                         {"type": "entity", "id": r["key"]}))
    return out


# ---------------------------------------------------------------- contradictions between sources
def reported_vs_filed(conn: Conn) -> list[Issue]:
    rows = conn.execute(
        f"""SELECT c.subject_type, c.subject_id, c.field, c.id AS shown_id, c.value AS shown,
                   c.valid_from AS shown_asof,
                   c.source_name AS shown_source, f.id AS filed_id, f.value AS filed, f.valid_from AS filed_asof,
                   f.source_name AS filed_source, f.source_url AS filed_url
            FROM fact_current c
            JOIN fact f ON f.status = 'published' AND f.tag = 'F' AND f.subject_type = c.subject_type
                       AND f.subject_id = c.subject_id AND f.field = c.field AND f.value <> c.value
                       AND jsonb_typeof(f.value) = jsonb_typeof(c.value)
                       AND f.valid_from >= c.valid_from - {CONFLICT_DAYS}
                       AND (f.valid_to IS NULL OR f.valid_to > current_date)
            WHERE c.tag = 'R' AND c.subject_type = ANY(%s)
              AND (jsonb_typeof(c.value) = 'number' OR (jsonb_typeof(c.value) = 'string' AND c.field = ANY(%s)))
            ORDER BY c.subject_type, c.subject_id, c.field, f.valid_from DESC""",
        (list(SUBJECT_TYPES), list(KEY_FIELDS))).fetchall()
    out: dict[str, Issue] = {}
    for r in rows:
        a, b = _num(r["shown"]), _num(r["filed"])
        if a is not None and b is not None and _close(a, b, 0.01):
            continue  # a number within 1%: rounding, not a contradiction
        key = f"{r['subject_type']}:{r['subject_id']}:{r['field']}"
        if key in out:
            continue
        ev = {"shown": {"fact_id": r["shown_id"], "value": r["shown"], "as_of": r["shown_asof"].isoformat(),
                        "tag": "R", "source": r["shown_source"]},
              "filed": {"fact_id": r["filed_id"], "value": r["filed"], "as_of": r["filed_asof"].isoformat(),
                        "tag": "F", "source": r["filed_source"], "url": r["filed_url"]}}
        out[key] = Issue("contradiction", key,
                         f"{r['subject_id']} {r['field']}: shown as reported by {r['shown_source']}, "
                         f"filed differently ({r['filed_source']})", ev,
                         {"type": r["subject_type"], "id": r["subject_id"]})
    return list(out.values())


def same_day_conflicts(conn: Conn) -> list[Issue]:
    rows = conn.execute(
        """SELECT subject_type, subject_id, field, valid_from,
                  jsonb_agg(jsonb_build_object('fact_id', id, 'value', value, 'tag', tag, 'source', source_name,
                                               'producer', producer) ORDER BY id) AS facts
           FROM fact WHERE status = 'published' AND subject_type = ANY(%s)
             AND jsonb_typeof(value) IN ('number', 'string', 'boolean')
             AND (valid_to IS NULL OR valid_to > current_date)
           GROUP BY 1, 2, 3, 4 HAVING count(DISTINCT producer) > 1 AND count(DISTINCT value) > 1
           ORDER BY 1, 2, 3, 4""", (list(SUBJECT_TYPES),)).fetchall()
    out = []
    for r in rows:
        values = {str(f["value"]) for f in r["facts"]}
        key = f"{r['subject_type']}:{r['subject_id']}:{r['field']}:{r['valid_from'].isoformat()}"
        out.append(Issue("contradiction", key,
                         f"{r['subject_id']} {r['field']} on {r['valid_from'].isoformat()}: {len(values)} values "
                         "from different sources", {"facts": r["facts"]}, {"type": r["subject_type"],
                                                                           "id": r["subject_id"]}))
    return out


# ---------------------------------------------------------------- contradictions between screens
def _value(v: Any) -> Any:
    return v.get("value") if isinstance(v, dict) and "value" in v else v


def _differs(a: Any, b: Any) -> bool:
    a, b = _value(a), _value(b)
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return abs(na - nb) > 1e-6
    return a != b


SCREEN_PAIRS = (  # (list view, row id key, detail view, fields compared)
    ("sites", "slug", "site_detail", ("status", "cushionMonths", "debtB")),
    ("instruments", "id", "instrument_detail", ("mardenPrice", "chainStatus", "sizeB")),
)


def screens(conn: Conn) -> list[Issue]:
    out = []
    for list_view, id_key, detail_view, fields in SCREEN_PAIRS:
        head = conn.execute("SELECT as_of, doc FROM read_model WHERE view = %s AND key = 'all' ORDER BY as_of DESC "
                            "LIMIT 1", (list_view,)).fetchone()
        if head is None or not isinstance(head["doc"], list):
            continue
        rows = {r.get(id_key): r for r in head["doc"] if isinstance(r, dict) and r.get(id_key)}
        details = conn.execute("SELECT key, doc FROM read_model WHERE view = %s AND as_of = %s AND key = ANY(%s)",
                               (detail_view, head["as_of"], list(rows))).fetchall()
        for d in details:
            row = rows[d["key"]]
            diffs = {f: {list_view: _value(row.get(f)), detail_view: _value((d["doc"] or {}).get(f))}
                     for f in fields if f in row and _differs(row.get(f), (d["doc"] or {}).get(f))}
            if not diffs:
                continue
            kind = "site" if list_view == "sites" else "instrument"
            out.append(Issue("screens", f"{kind}:{d['key']}:{list_view}_vs_{detail_view}",
                             f"{d['key']}: {', '.join(sorted(diffs))} differ between {list_view} and {detail_view}",
                             {"as_of": head["as_of"].isoformat(), "differences": diffs},
                             {"type": kind, "id": d["key"]}, "high"))
    return out
