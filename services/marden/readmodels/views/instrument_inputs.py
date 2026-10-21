"""What the instrument builders read (instruments, instrument_detail, curve, gpu_collateral).

Every read is dated by the snapshot being built (`build_date()`): facts come through `known_facts`,
model outputs are the latest per model and subject with `as_of` on or before the build date, prices
and holdings are those observed (and, for a backfilled day, learned) by then. Rows from sources
without a commercial licence are left out when MARDEN_SHOW_UNLICENSED_PRICES is false
(marden.core.licensing).

Model outputs (written by marden.models): 'price' {price, spreadBp, yieldPct, recovery, change1w,
inputs, band}, 'mark_gap' {holders, count, parMm, fundMarks, nextMarksPublic}, 'recovery', 'stress',
'status' (site: {chain, status}), 'curve' ('all': CurveTier[]), 'gpu_collateral', 'gpu_deployment'.
A value an output carries is either a bare number/string (tag M, the output's as-of and method
version) or already a Sourced<T> dict, which is passed through. A missing output or field is n/d.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from marden.core.db import Conn, Row
from marden.core.facts import sourced
from marden.core.licensing import show_unlicensed_prices, unlicensed_source_names
from marden.readmodels.context import build_date, is_backfill

SOURCED_KEYS = ("value", "tag", "sourceUrl", "sourceName", "asOf", "methodVersion")
OBSERVED_KINDS = ("trade", "exchange", "quote", "dealer")
# Fund filings that name holders (N-PORT, BDC 10-Q/10-K). Insurer NAIC Schedule D lines are held for
# legal review (docs/methods/price-sources.md section 3) and are not shown as holders.
FUND_FILINGS = ("N-PORT", "10-Q", "10-K")


def day() -> date:
    return build_date()


def iso(value: date | str) -> str:
    return value if isinstance(value, str) else value.isoformat()


def num(value: Any) -> float | None:
    """A JSON/SQL number as float (bools and text are not numbers)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float | Decimal):
        return float(value)
    return None


def nd(tag: str) -> dict[str, Any]:
    """A Sourced<T> nobody has sourced yet: value null (n/d), dated the build day."""
    return {"value": None, "tag": tag, "asOf": day().isoformat()}


def _known_by_clause(column: str = "learned_at") -> str:
    """On a backfilled day, only rows Marden had learned by the end of that day."""
    return f" AND {column} < (%(day)s::date + 1)::timestamptz" if is_backfill() else ""


def _licence_clause() -> tuple[str, list[str]]:
    if show_unlicensed_prices():
        return "", []
    return " AND source_name <> ALL(%(hidden)s)", unlicensed_source_names()


# ---------------------------------------------------------------- facts
def fact_sourced(row: Row | None, value: Any = None) -> dict[str, Any] | None:
    """A fact row as Sourced<T> (marden.core.facts.sourced: tag, source, as-of, method version),
    optionally with a display value derived from the fact's value."""
    out = sourced(row)
    if out is not None and value is not None:
        out["value"] = value
    return out


# ---------------------------------------------------------------- model outputs
def latest_output(conn: Conn, model: str, subject_id: str, subject_type: str | None = None) -> Row | None:
    """The newest output of `model` for the subject with as_of on or before the build date. A model
    run for a past day reads only what was public that day (marden.models.pit), so its output is
    that day's value even when it was computed later."""
    sql = (
        "SELECT id, model, version, subject_type, subject_id, as_of, output, created_at FROM model_output"
        " WHERE model = %(model)s AND subject_id = %(id)s AND as_of <= %(day)s"
        " AND (%(type)s::text IS NULL OR subject_type = %(type)s)"
        " ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1"
    )
    return conn.execute(sql, {"model": model, "id": subject_id, "type": subject_type, "day": day()}).fetchone()


def method_version(out: Row) -> str:
    """'price' + '0.3' -> 'price v0.3'; a version that names its method already is kept."""
    version = str(out["version"])
    if version[:1].isdigit():
        return f"{out['model']} v{version}"
    if version.startswith("v") and version[1:2].isdigit():
        return f"{out['model']} {version}"
    return version


def model_sourced(out: Row, raw: Any) -> dict[str, Any] | None:
    """A value from a model output as Sourced<T>. Sourced dicts inside the output pass through (keys
    outside Sourced dropped, missing as-of / method filled from the output row); None stays None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        if "value" not in raw:
            return None
        got = {k: raw[k] for k in SOURCED_KEYS if raw.get(k) is not None or k == "value"}
        got.setdefault("tag", "M")
        got.setdefault("asOf", iso(out["as_of"]))
        if got["tag"] == "M":
            got.setdefault("methodVersion", method_version(out))
        return got
    out_doc: dict[str, Any] = {
        "value": raw,
        "tag": "M",
        "asOf": iso(out["as_of"]),
        "methodVersion": method_version(out),
    }
    return out_doc


def output_field(out: Row | None, *keys: str) -> Any:
    """First present key of a model output's JSON (None when the output or every key is missing)."""
    if out is None or not isinstance(out["output"], dict):
        return None
    for key in keys:
        if out["output"].get(key) is not None:
            return out["output"][key]
    return None


def sourced_number(out: Row | None, *keys: str) -> dict[str, Any] | None:
    """A numeric model value as Sourced<number> (None when absent or not a number)."""
    raw = output_field(out, *keys)
    got = model_sourced(out, raw) if out is not None else None
    if got is None:
        return None
    value = got["value"]
    if value is not None and num(value) is None:
        return None
    got["value"] = None if value is None else num(value)
    return got


# ---------------------------------------------------------------- prices
def price_rows(conn: Conn, instrument_id: str, kinds: Sequence[str]) -> list[Row]:
    """price_obs of these kinds observed by the build date, oldest first (licence switch applied)."""
    licence, hidden = _licence_clause()
    sql = (
        "SELECT id, obs_date, obs_at, kind, price, yield_pct, spread_bp, tag, source_name, source_url,"
        " method_version FROM price_obs"
        " WHERE instrument_id = %(id)s AND kind = ANY(%(kinds)s) AND obs_date <= %(day)s"
        + _known_by_clause()
        + licence
        + " ORDER BY obs_date, obs_at NULLS FIRST, id"
    )
    return conn.execute(sql, {"id": instrument_id, "kinds": list(kinds), "day": day(), "hidden": hidden}).fetchall()


def price_sourced(row: Row, value: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"value": value, "tag": row["tag"].strip(), "asOf": iso(row["obs_date"])}
    if row.get("source_url"):
        out["sourceUrl"] = row["source_url"]
    if row.get("source_name"):
        out["sourceName"] = row["source_name"]
    if row.get("method_version"):
        out["methodVersion"] = row["method_version"]
    return out


def daily_series(rows: Sequence[Row]) -> list[Row]:
    """One row per observation date: the last one seen that day (rows are oldest first)."""
    by_day: dict[date, Row] = {}
    for row in rows:
        if num(row["price"]) is not None:
            by_day[row["obs_date"]] = row
    return [by_day[d] for d in sorted(by_day)]


def week_change(series: Sequence[Row]) -> dict[str, Any] | None:
    """Last price minus the price on or before seven days earlier, as Sourced<number> (None when the
    series does not reach back a week)."""
    if len(series) < 2:
        return None
    last = series[-1]
    cutoff = last["obs_date"] - timedelta(days=7)
    before = [r for r in series[:-1] if r["obs_date"] <= cutoff]
    if not before:
        return None
    change = round(float(last["price"]) - float(before[-1]["price"]), 4)
    return price_sourced(last, change)


def is_median(row: Row) -> bool:
    """Cross-fund medians ('N-PORT median of N funds (...)') are derived rows, not one holder's mark."""
    return " median of " in (row.get("source_name") or "")


def filed_marks(rows: Sequence[Row]) -> list[Row]:
    """Each holder's own filed mark (tag F, not a cross-fund median or an adjusted M mark)."""
    return [r for r in rows if r["tag"].strip() == "F" and not is_median(r) and num(r["price"]) is not None]


# ---------------------------------------------------------------- holdings
def holder_lines(conn: Conn, instrument_id: str) -> list[Row]:
    """Each fund's lines of this instrument in its latest filing known by the build date, kept only
    when that filing is also the fund's newest known filing (a fund whose newer filing no longer
    lists the instrument no longer holds it). N-PORT and BDC filings only."""
    known = (
        " h.period_end <= %(day)s AND (h.filed_at IS NULL OR h.filed_at <= %(day)s)"
        " AND h.filing_type = ANY(%(filings)s)" + _known_by_clause("h.learned_at")
    )
    sql = f"""WITH mine AS (
                 SELECT h.*, max(h.period_end) OVER (PARTITION BY h.fund_entity_id) AS fund_last
                   FROM holding h WHERE h.instrument_id = %(id)s AND {known}),
               newest AS (
                 SELECT h.fund_entity_id, max(h.period_end) AS period_end FROM holding h
                  WHERE h.fund_entity_id IN (SELECT fund_entity_id FROM mine) AND {known}
                  GROUP BY h.fund_entity_id)
               SELECT m.id, m.fund_entity_id, e.name AS fund_name, m.period_end, m.filing_type, m.filing_url,
                      m.filed_at, m.par, m.value_usd, m.mark_price, m.fund_net_assets
                 FROM mine m JOIN newest n ON n.fund_entity_id = m.fund_entity_id AND n.period_end = m.period_end
                 JOIN entity e ON e.id = m.fund_entity_id
                WHERE m.period_end = m.fund_last
                ORDER BY m.par DESC NULLS LAST, m.id"""
    return conn.execute(sql, {"id": instrument_id, "day": day(), "filings": list(FUND_FILINGS)}).fetchall()


def holder_totals(lines: Sequence[Row]) -> tuple[int, float]:
    """Funds reporting the instrument and their par in $mm (lines without a par add no par)."""
    funds = {r["fund_entity_id"] for r in lines}
    par = sum(float(r["par"]) for r in lines if num(r["par"]) is not None)
    return len(funds), round(par / 1e6, 3)


# ---------------------------------------------------------------- identity
def instrument_identity(conn: Conn, ids: Sequence[str] | None = None, *, v1_only: bool = False) -> list[Row]:
    """Instrument rows with their site, issuer and issuer parent names."""
    sql = """SELECT i.id, i.name, i.kind, i.issuer_name, i.site_slug, i.cusip, i.isin, i.in_v1,
                    s.name AS site_name, e.name AS issuer_entity, e.kind AS issuer_kind, p.name AS parent_name
               FROM instrument i
               LEFT JOIN site s ON s.slug = i.site_slug
               LEFT JOIN entity e ON e.id = i.issuer_entity_id
               LEFT JOIN entity p ON p.id = e.parent_id
              WHERE (%(ids)s::text[] IS NULL OR i.id = ANY(%(ids)s)) AND (NOT %(v1)s OR i.in_v1)
              ORDER BY i.id"""
    return conn.execute(sql, {"ids": list(ids) if ids is not None else None, "v1": v1_only}).fetchall()


def all_instrument_ids(conn: Conn) -> list[str]:
    return [r["id"] for r in conn.execute("SELECT id FROM instrument ORDER BY id").fetchall()]
