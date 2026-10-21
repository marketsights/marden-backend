"""Shared reads for the site views (completion, sites, site_detail, site_schedule, site_tab:*, project_file).

Every read honors the snapshot being built: facts through `known_facts` / `known_fact_rows`, and
events, prices, holdings, model outputs and site links dated with `build_date()` (what Marden had
learned by the end of that day). A value nobody has sourced is n/d: `nd(tag)` for a Sourced<T>
field (value null), `None` for a field the type allows to be null. Model outputs (status, cushion,
price, recovery, mark_gap) are read, never recomputed; when a model has not run, its fields are n/d.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from marden.core.db import Conn, Row
from marden.core.facts import sourced
from marden.core.jsonutil import jsonb
from marden.readmodels.context import build_date, known_fact_rows, known_facts
from marden.readmodels.views.sites_text import text_of

Doc = dict[str, Any]
ROLES = ("end_user", "tenant", "developer", "lenders")
TAG_RANK = {"F": 0, "R": 1, "M": 2}
US_STATES = frozenset((
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR",
))
NOT_COUNTED = frozenset({"repaid", "proposed", "cancelled", "canceled"})
INSTRUMENT_FIELDS = (
    "amount_usd", "amount_basis", "status", "coupon", "maturity", "seniority", "drawn_usd", "commitment_usd",
    "rating", "exposure_links",
)


# ---------------------------------------------------------------- dates and Sourced values
def day() -> date:
    return build_date()


def next_day() -> date:
    """Rows learned before this instant were known on the build date."""
    return build_date() + timedelta(days=1)


def nd(tag: str = "R") -> Doc:
    """A Sourced<T> nobody has sourced yet: shown as n/d. `tag` is the kind of source that fills it."""
    return {"value": None, "tag": tag, "asOf": day().isoformat()}


def src(row: Row | None) -> Doc | None:
    return sourced(row)


def src_or_nd(row: Row | None, tag: str = "R") -> Doc:
    return sourced(row) or nd(tag)


def with_value(row: Row, value: Any) -> Doc:
    """A Sourced value carried by `row` (fact) but shown in another form (MW -> GW text, USD -> $B)."""
    out = sourced(row)
    assert out is not None
    out["value"] = value
    return out


def billions(usd: Any) -> float | None:
    return round(float(usd) / 1e9, 3) if isinstance(usd, int | float) else None


def gw_text(mw: Any, suffix: str = "") -> str | None:
    if not isinstance(mw, int | float):
        return None
    gw = f"{mw / 1000:.2f}".rstrip("0").rstrip(".")
    return f"{gw} GW{suffix}"


def weakest_tag(tags: Iterable[str]) -> str:
    tags = list(tags)
    return max(tags, key=lambda t: TAG_RANK.get(t, 2)) if tags else "R"


# ---------------------------------------------------------------- sites
def site_slugs(conn: Conn) -> list[str]:
    return [r["slug"] for r in conn.execute("SELECT slug FROM site ORDER BY slug")]


def site_rows(conn: Conn) -> list[Row]:
    return conn.execute("SELECT * FROM site ORDER BY slug").fetchall()


def site_row(conn: Conn, slug: str) -> Row | None:
    return conn.execute("SELECT * FROM site WHERE slug = %s", (slug,)).fetchone()


def is_us(site: Row) -> bool:
    """V1 is US only (spec 05). A site is non-US when its state text names no US state ('VIC/TAS
    (Australia)'); an empty state or 'multi' (several US states) stays in."""
    text = (site.get("state") or "").strip()
    if not text or text.lower() == "multi":
        return True
    tokens = [t for t in re.split(r"[^A-Za-z]+", text) if t]
    return any(t.upper() in US_STATES and len(t) == 2 for t in tokens)


def site_county(site: Row) -> str:
    return site.get("county") or "n/d"


def gw_value(facts: dict[str, Row]) -> Doc:
    """Site power in GW: gross (utility) MW, else critical IT MW, as the fact states it."""
    if (row := facts.get("gross_mw")) and (text := gw_text(row["value"])):
        return with_value(row, text)
    if (row := facts.get("critical_it_mw")) and (text := gw_text(row["value"], " IT")):
        return with_value(row, text)
    return nd("R")


# ---------------------------------------------------------------- chain parties and status
def chain_parties(conn: Conn, slug: str) -> dict[str, Row]:
    rows = conn.execute("SELECT * FROM chain_party WHERE site_slug = %s", (slug,)).fetchall()
    return {r["role"]: r for r in rows}


def party_label(parties: dict[str, Row], role: str) -> str | None:
    row = parties.get(role)
    return row["label"] if row and row.get("label") else None


def latest_output(conn: Conn, model: str, subject_type: str, subject_id: str) -> Row | None:
    """The newest output of `model` for a subject dated on or before the build date. A model run for a
    past day reads only what was public that day (marden.models.pit), so its output is that day's
    value even when it was computed later (as views/instrument_inputs.latest_output)."""
    return conn.execute(
        """SELECT * FROM model_output WHERE model = %s AND subject_type = %s AND subject_id = %s
             AND as_of <= %s ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
        (model, subject_type, subject_id, day()),
    ).fetchone()


def output_history(conn: Conn, model: str, subject_type: str, subject_id: str, since: date) -> list[Row]:
    """The last output per as_of day between `since` and the build date, oldest first."""
    return conn.execute(
        """SELECT DISTINCT ON (as_of) * FROM model_output WHERE model = %s AND subject_type = %s
             AND subject_id = %s AND as_of BETWEEN %s AND %s ORDER BY as_of, created_at DESC, id DESC""",
        (model, subject_type, subject_id, since, day()),
    ).fetchall()


def method_label(out: Row) -> str:
    version = str(out.get("version") or "")
    if out["model"] in version:
        return version
    return f"{out['model']} v{version}" if version[:1].isdigit() else f"{out['model']} {version}".strip()


def is_sourced(value: Any) -> bool:
    return isinstance(value, dict) and "value" in value and "tag" in value and "asOf" in value


def msourced(out: Row | None, value: Any, tag: str = "M") -> Doc:
    """A model output value as Sourced<T>; values the model already sourced pass through."""
    if out is None or value is None:
        return nd(tag)
    if is_sourced(value):
        return value
    return {"value": value, "tag": "M", "asOf": out["as_of"].isoformat(), "methodVersion": method_label(out),
            "sourceName": "Marden model"}


def output_field(out: Row | None, *path: str) -> Any:
    node: Any = out["output"] if out else None
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def chain_links(parties: dict[str, Row], status_out: Row | None) -> list[Doc]:
    """The four links in role order: the status model's links, or the parties with no status (n/d)."""
    modeled = output_field(status_out, "chain")
    if isinstance(modeled, list) and modeled:
        by_role = {link.get("role"): link for link in modeled if isinstance(link, dict)}
        if all(r in by_role for r in ROLES):
            return [by_role[r] for r in ROLES]
    return [{"role": r, "party": party_label(parties, r) or "n/d", "status": "none", "reason": "n/d"} for r in ROLES]


def chain_status(status_out: Row | None) -> str | None:
    status = output_field(status_out, "status")
    return status if status in ("Broken", "Strained", "Holding", "No build") else None


# ---------------------------------------------------------------- debt
def site_instruments(conn: Conn, slug: str) -> list[Doc]:
    """Instruments that need this site finished, each with its current facts."""
    rows = conn.execute(
        "SELECT * FROM instrument WHERE site_slug = %s ORDER BY in_v1 DESC, id", (slug,)
    ).fetchall()
    return [{**r, "facts": known_facts(conn, "instrument", r["id"], INSTRUMENT_FIELDS)} for r in rows]


def counts_toward_debt(inst: Doc) -> bool:
    facts = inst["facts"]
    status = (facts.get("status") or {}).get("value")
    basis = (facts.get("amount_basis") or {}).get("value")
    return "amount_usd" in facts and status not in NOT_COUNTED and basis != "holder_par"


def debt_value(instruments: Sequence[Doc]) -> Doc:
    """Debt $B: the sum of the instruments' sizes, tagged with the weakest of their tags."""
    rows = [i["facts"]["amount_usd"] for i in instruments if counts_toward_debt(i)]
    rows = [r for r in rows if isinstance(r["value"], int | float)]
    if not rows:
        return nd("R")
    if len(rows) == 1:
        return with_value(rows[0], billions(rows[0]["value"]))
    names = sorted({r["source_name"] for r in rows if r.get("source_name")})
    return {"value": billions(sum(r["value"] for r in rows)), "tag": weakest_tag(r["tag"] for r in rows),
            "asOf": max(r["valid_from"] for r in rows).isoformat(), "sourceName": " + ".join(names)}


def main_instrument(instruments: Sequence[Doc]) -> Doc | None:
    counted = [i for i in instruments if counts_toward_debt(i)]
    pool = counted or list(instruments)
    if not pool:
        return None
    return max(pool, key=lambda i: (bool(i.get("in_v1")), (i["facts"].get("amount_usd") or {}).get("value") or 0))


def dealer_quote(conn: Conn, instrument_id: str) -> Row | None:
    return conn.execute(
        """SELECT * FROM price_obs WHERE instrument_id = %s AND kind = 'dealer' AND obs_date <= %s
             AND learned_at < %s::date ORDER BY obs_date DESC, id DESC LIMIT 1""",
        (instrument_id, day(), next_day()),
    ).fetchone()


def price_sourced(row: Row | None, as_text: bool = False) -> Doc | None:
    if row is None:
        return None
    price = float(row["price"])
    out: Doc = {"value": f"{price:.1f}" if as_text else round(price, 4), "tag": row["tag"],
                "asOf": row["obs_date"].isoformat(), "sourceName": row["source_name"]}
    if row.get("source_url"):
        out["sourceUrl"] = row["source_url"]
    if row.get("method_version"):
        out["methodVersion"] = row["method_version"]
    return out


def holders(conn: Conn, instrument_ids: Sequence[str]) -> dict[str, Any]:
    """Filed holders known on the build date: each fund's latest filed period, its par summed."""
    if not instrument_ids:
        return {"count": 0, "par": 0.0, "period": None, "filings": []}
    rows = conn.execute(
        """WITH known AS (
             SELECT * FROM holding WHERE instrument_id = ANY(%s) AND learned_at < %s::date
               AND coalesce(filed_at, period_end) <= %s),
           latest AS (SELECT fund_entity_id, max(period_end) AS period_end FROM known GROUP BY 1)
           SELECT k.fund_entity_id, k.period_end, k.filing_type, sum(k.par) AS par
             FROM known k JOIN latest l USING (fund_entity_id, period_end) GROUP BY 1, 2, 3""",
        (list(instrument_ids), next_day(), day()),
    ).fetchall()
    return {
        "count": len({r["fund_entity_id"] for r in rows}),
        "par": float(sum(r["par"] or 0 for r in rows)),
        "period": max((r["period_end"] for r in rows), default=None),
        "filings": sorted({r["filing_type"] for r in rows if r["filing_type"]}),
    }


def holders_note(summary: dict[str, Any]) -> str:
    if summary["count"] == 0:
        return "No N-PORT, BDC or insurer filing in Marden reports holding this debt."
    par = summary["par"] / 1e6
    filings = ", ".join(summary["filings"]) or "filings"
    return (f"{summary['count']} filers report ${par:,.1f}mm par ({filings}; latest period "
            f"{summary['period'].isoformat()}).")


def subjects_listing(conn: Conn, subject_type: str, field: str, item: Any) -> list[str]:
    """Subjects whose `field` fact (a JSON array) has ever listed `item` as published: the candidates
    a builder then checks against what was known on the build date (`known_facts`). Two branches so
    each uses an index (published facts by subject; superseded ones by status)."""
    probe = jsonb([item])
    rows = conn.execute(
        """SELECT subject_id FROM fact WHERE subject_type = %(t)s AND field = %(f)s AND status = 'published'
             AND jsonb_typeof(value) = 'array' AND value @> %(v)s
           UNION SELECT subject_id FROM fact WHERE status = 'superseded' AND subject_type = %(t)s
             AND field = %(f)s AND jsonb_typeof(value) = 'array' AND value @> %(v)s
           ORDER BY 1""",
        {"t": subject_type, "f": field, "v": probe},
    ).fetchall()
    return [r["subject_id"] for r in rows]


# ---------------------------------------------------------------- events and evidence
def site_events(conn: Conn, slug: str, *, kinds: Sequence[str] | None = None, future: bool = False) -> list[Row]:
    """Published events on the site that Marden knew by the build date, newest first. Past-dated
    only, unless `future` (hearings and deadlines still to come)."""
    sql = """SELECT * FROM event WHERE subject_type = 'site' AND subject_id = %s AND status = 'published'
               AND learned_at < %s::date AND event_date {op} %s"""
    params: list[Any] = [slug, next_day(), day()]
    sql = sql.format(op=">" if future else "<=")
    if kinds is not None:
        sql += " AND kind = ANY(%s)"
        params.append(list(kinds))
    sql += " ORDER BY event_date DESC, id DESC" if not future else " ORDER BY event_date, id"
    return conn.execute(sql, params).fetchall()


def event_evidence(ev: Row) -> Doc:
    return {"date": ev["event_date"].isoformat(), "text": ev["title"], "tag": ev["tag"],
            "sourceName": ev["source_name"], "sourceUrl": ev.get("source_url") or ""}


def fact_evidence(row: Row, text: str | None = None, when: str | None = None) -> Doc:
    return {"date": when or row["valid_from"].isoformat(), "text": text or text_of(row["value"]) or row["field"],
            "tag": row["tag"], "sourceName": row.get("source_name") or "", "sourceUrl": row.get("source_url") or ""}


def materiality_notes(conn: Conn, slug: str) -> dict[tuple[str, str], Doc]:
    """(event date, title) -> the archive's materiality judgment (M fact) for events it reviewed."""
    rows = known_fact_rows(conn, "site", slug, ["materiality_note"])["materiality_note"]
    out: dict[tuple[str, str], Doc] = {}
    for r in rows:
        v = r["value"] if isinstance(r["value"], dict) else {}
        if v.get("judgment") == "material" and v.get("event_date") and v.get("title"):
            out.setdefault((v["event_date"], v["title"]), v)
    return out


SEVERITIES = ("red", "amber", "green", "none")


def severity(ev: Row) -> str:
    return ev["severity"] if ev.get("severity") in SEVERITIES else "none"


def material_events(conn: Conn, slug: str, events: Sequence[Row] | None = None) -> list[tuple[Row, Doc | None]]:
    """Material events, newest first: approved material events and events the archive judged material
    (materiality_note), with that judgment."""
    notes = materiality_notes(conn, slug)
    rows = events if events is not None else site_events(conn, slug)
    out = []
    for ev in rows:
        note = notes.get((ev["event_date"].isoformat(), ev["title"]))
        if ev.get("material") or note is not None:
            out.append((ev, note))
    return out


def last_evidence(conn: Conn, slug: str, events: Sequence[Row]) -> Doc | None:
    """Completion's 'Last evidence': the latest material event, else the latest red/amber event, else
    the latest record of any kind (None only for a site with no record at all)."""
    material = material_events(conn, slug, events)
    if material:
        return event_evidence(material[0][0])
    flagged = [e for e in events if severity(e) in ("red", "amber")]
    records = flagged or [e for e in events if e["kind"] != "catalyst"]
    return event_evidence(records[0]) if records else None
