"""Company credit inputs (spec 24), read point in time: XBRL financials and agency ratings.

Financials are the SEC XBRL entity facts (collectors/sec_xbrl_parse.FIELDS), each usable 40 days after its
period end (marden.models.pit):
- trailing twelve months of a duration line: the latest 12-month value when the newest period is a fiscal
  year, else fiscal year + year-to-date - prior year-to-date (the standard LTM build), else the latest
  fiscal year (stale; the basis says so);
- debt: `total_debt_usd` where tagged, else `long_term_debt_usd` (current maturities included) plus
  short-term borrowings (commercial paper when no short-term line), at the newest period end;
- lease liabilities: operating + finance lease liabilities (IFRS: the single lease liability);
- EBITDA: operating income + depreciation and amortization (or depreciation + intangible amortization).

Ratings come from the rating facts Marden holds (tag R: the rating text on the company's own notes, and
the tenant rating on the sites it leases): agency, rating, outlook and the last action when the text states
them ("Moody's Baa2 (neg) / S&P BBB- / Fitch BBB", "BBB- (S&P, cut 2026-07-09)"). An agency the text does
not name is not guessed (an unlabelled "BBB" is skipped; a Moody's-scale grade like "Baa2" can only be
Moody's). Notches count from AAA / Aaa = 0; investment grade is notch 9 (BBB- / Baa3) or better.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit
from marden.models.sourced import fact_value, num

SP_SCALE = (
    "AAA",
    "AA+",
    "AA",
    "AA-",
    "A+",
    "A",
    "A-",
    "BBB+",
    "BBB",
    "BBB-",
    "BB+",
    "BB",
    "BB-",
    "B+",
    "B",
    "B-",
    "CCC+",
    "CCC",
    "CCC-",
    "CC",
    "C",
    "D",
)
MOODYS_SCALE = (
    "Aaa",
    "Aa1",
    "Aa2",
    "Aa3",
    "A1",
    "A2",
    "A3",
    "Baa1",
    "Baa2",
    "Baa3",
    "Ba1",
    "Ba2",
    "Ba3",
    "B1",
    "B2",
    "B3",
    "Caa1",
    "Caa2",
    "Caa3",
    "Ca",
    "C",
)
IG_FLOOR = 9  # BBB- / Baa3
AGENCIES = ("S&P", "Moody's", "Fitch")
MONTHS = (12, 9, 6, 3)
DURATION_BASES = (
    "operating_cash_flow",
    "capex",
    "productive_asset_payments",
    "revenue",
    "operating_income",
    "depreciation_amortization",
    "depreciation",
    "amortization_intangibles",
)
INSTANT_FIELDS = (
    "total_debt_usd",
    "long_term_debt_usd",
    "long_term_debt_current_usd",
    "long_term_debt_noncurrent_usd",
    "short_term_borrowings_usd",
    "commercial_paper_usd",
    "debt_current_usd",
    "cash_usd",
    "operating_lease_liability_usd",
    "operating_lease_liability_current_usd",
    "operating_lease_liability_noncurrent_usd",
    "finance_lease_liability_usd",
    "finance_lease_liability_current_usd",
    "finance_lease_liability_noncurrent_usd",
    "lease_liability_usd",
    "remaining_performance_obligation_usd",
    "backlog_rpo_usd",
)
FIN_FIELDS = (*(f"{b}_{n}m_usd" for b in DURATION_BASES for n in MONTHS), *INSTANT_FIELDS)


def notch(rating: str | None) -> int | None:
    if not rating:
        return None
    r = rating.strip()
    if r in SP_SCALE:
        return SP_SCALE.index(r)
    if r in MOODYS_SCALE:
        return MOODYS_SCALE.index(r)
    return None


@dataclass
class Figure:
    """A USD amount computed from filed rows: value, the period it is as of, and the rows it sums."""

    usd: float
    as_of: date
    rows: list[Row]
    basis: str

    def sourced_b(self) -> dict[str, Any]:
        row = max(self.rows, key=lambda r: r["valid_from"])
        out = fact_value(row, round(self.usd / 1e9, 3))
        out["asOf"] = self.as_of.isoformat()
        return out


def _add(*figs: Figure | None, basis: str) -> Figure | None:
    got = [f for f in figs if f is not None]
    if not got:
        return None
    return Figure(sum(f.usd for f in got), min(f.as_of for f in got), [r for f in got for r in f.rows], basis)


@dataclass
class Financials:
    entity_id: str
    day: date
    rows: dict[str, list[Row]] = field(default_factory=dict)

    def _periods(self, base: str) -> dict[tuple[date, int], Row]:
        out: dict[tuple[date, int], Row] = {}
        for n in MONTHS:
            by_end: dict[date, list[Row]] = {}
            for r in self.rows.get(f"{base}_{n}m_usd", []):
                if num(r["value"]) is not None:
                    by_end.setdefault(r["valid_from"], []).append(r)
            for end, rs in by_end.items():
                cur = pit.current_of(rs)
                if cur is not None:
                    out[(end, n)] = cur
        return out

    def ttm(self, base: str) -> Figure | None:
        per = self._periods(base)
        if not per:
            return None
        latest = max(e for e, _ in per)
        n = max(m for e, m in per if e == latest)
        val = lambda key: float(num(per[key]["value"]))  # noqa: E731
        if n == 12:
            return Figure(val((latest, 12)), latest, [per[(latest, 12)]], f"{base}: fiscal year to {latest}")
        fys = [e for e, m in per if m == 12 and latest - timedelta(days=370) < e < latest]
        priors = [e for e, m in per if m == n and abs((latest - e).days - 365) <= 20]
        if fys and priors:
            fy, prior = max(fys), max(priors)
            usd = val((fy, 12)) + val((latest, n)) - val((prior, n))
            return Figure(
                usd,
                latest,
                [per[(fy, 12)], per[(latest, n)], per[(prior, n)]],
                f"{base}: FY {fy} + {n}m to {latest} - {n}m to {prior}",
            )
        fy_all = [e for e, m in per if m == 12]
        if fy_all:
            fy = max(fy_all)
            return Figure(val((fy, 12)), fy, [per[(fy, 12)]], f"{base}: latest fiscal year {fy} (no LTM build)")
        return None

    def instant(self, fld: str, end: date | None = None) -> Figure | None:
        rows = [r for r in self.rows.get(fld, []) if num(r["value"]) is not None]
        if end is not None:
            rows = [r for r in rows if r["valid_from"] == end]
        if not rows:
            return None
        newest = max(r["valid_from"] for r in rows)
        cur = pit.current_of([r for r in rows if r["valid_from"] == newest])
        assert cur is not None
        return Figure(float(num(cur["value"])), newest, [cur], fld)

    def _latest_end(self, *fields: str) -> date | None:
        ends = [r["valid_from"] for f in fields for r in self.rows.get(f, []) if num(r["value"]) is not None]
        return max(ends) if ends else None

    def debt(self) -> Figure | None:
        end = self._latest_end("total_debt_usd", "long_term_debt_usd", "long_term_debt_noncurrent_usd")
        if end is None:
            return None
        if (total := self.instant("total_debt_usd", end)) is not None:
            return total
        ltd = self.instant("long_term_debt_usd", end) or _add(
            self.instant("long_term_debt_noncurrent_usd", end),
            self.instant("long_term_debt_current_usd", end),
            basis="long-term debt, current + noncurrent",
        )
        short = self.instant("short_term_borrowings_usd", end) or self.instant("commercial_paper_usd", end)
        return _add(ltd, short, basis=f"long-term debt{' + short-term borrowings' if short else ''} at {end}")

    def leases(self) -> Figure | None:
        end = self._latest_end(
            "operating_lease_liability_usd", "operating_lease_liability_noncurrent_usd", "lease_liability_usd"
        )
        if end is None:
            return None
        if (ifrs := self.instant("lease_liability_usd", end)) is not None:
            return ifrs
        op = self.instant("operating_lease_liability_usd", end) or _add(
            self.instant("operating_lease_liability_current_usd", end),
            self.instant("operating_lease_liability_noncurrent_usd", end),
            basis="operating lease liability",
        )
        fin = self.instant("finance_lease_liability_usd", end) or _add(
            self.instant("finance_lease_liability_current_usd", end),
            self.instant("finance_lease_liability_noncurrent_usd", end),
            basis="finance lease liability",
        )
        return _add(op, fin, basis=f"operating + finance lease liabilities at {end}")

    def ebitda(self) -> Figure | None:
        oi = self.ttm("operating_income")
        da = self.ttm("depreciation_amortization")
        if da is None:
            da = _add(
                self.ttm("depreciation"),
                self.ttm("amortization_intangibles"),
                basis="depreciation + intangible amortization",
            )
        if oi is None or da is None:
            return None
        return Figure(
            oi.usd + da.usd,
            min(oi.as_of, da.as_of),
            oi.rows + da.rows,
            f"EBITDA = operating income ({oi.basis}) + D&A ({da.basis})",
        )

    def backlog(self) -> Figure | None:
        return self.instant("remaining_performance_obligation_usd") or self.instant("backlog_rpo_usd")


def load_financials(conn: Conn, day: date, entity_ids: list[str]) -> dict[str, Financials]:
    out = {e: Financials(e, day) for e in entity_ids}
    for r in pit.fact_rows(conn, day, "entity", entity_ids, list(FIN_FIELDS)):
        out[r["subject_id"]].rows.setdefault(r["field"], []).append(r)
    return out


# ---------------------------------------------------------------- ratings
_GRADE = (
    r"(?:AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC[+-]?|CC|C|D"
    r"|Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123]|Ca)"
)
_AGENCY = r"(S&P|Moody['\u2019]s|Fitch)"
_LABEL_FIRST = re.compile(_AGENCY + r"\s+(?:CFR\s+)?(" + _GRADE + r")(?![\w+-])(\s*\([^)]*\))?")
_GRADE_FIRST = re.compile(r"(?<![\w+-])(" + _GRADE + r")\s*\(" + _AGENCY + r"([^)]*)\)")
_MOODYS_ONLY = re.compile(
    r"(?<![\w+-])((?:Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123]))(?![\w+-])(\s*\([^)]*\))?"
)
_OUTLOOK = re.compile(r"\b(neg(?:ative)?|pos(?:itive)?|stable|developing)\b", re.I)
_ACTION = re.compile(
    r"\b(cut|downgraded|lowered|upgraded|raised|affirmed)\b[^;/]{0,30}?"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2} [A-Z][a-z]{2,8} \d{4})"
)
OUTLOOKS = {
    "neg": "Negative",
    "negative": "Negative",
    "pos": "Positive",
    "positive": "Positive",
    "stable": "Stable",
    "developing": "Developing",
}


@dataclass
class AgencyRating:
    agency: str
    rating: str
    outlook: str | None
    action: str | None
    row: Row


def _agency(name: str) -> str:
    return "Moody's" if name.lower().startswith("moody") else name


def parse_ratings(text: str, row: Row) -> list[AgencyRating]:
    """Agency ratings a rating text states (issuer ratings first when the text separates them)."""
    text = text.split("Issuer:", 1)[1] if "Issuer:" in text else text
    if "(sf)" in text:
        return []
    out: dict[str, AgencyRating] = {}
    for m in _LABEL_FIRST.finditer(text):
        agency, grade = _agency(m.group(1)), m.group(2)
        if (agency == "Moody's") != (grade in MOODYS_SCALE) or notch(grade) is None:
            continue
        paren = m.group(3) or ""
        ol = _OUTLOOK.search(paren)
        out.setdefault(agency, AgencyRating(agency, grade, OUTLOOKS[ol.group(1).lower()] if ol else None, None, row))
    for m in _GRADE_FIRST.finditer(text):
        grade, agency, rest = m.group(1), _agency(m.group(2)), m.group(3)
        if (agency == "Moody's") != (grade in MOODYS_SCALE) or notch(grade) is None:
            continue
        act = _ACTION.search(rest)
        ol = _OUTLOOK.search(rest)
        prev = out.get(agency)
        out[agency] = AgencyRating(
            agency,
            grade,
            (OUTLOOKS[ol.group(1).lower()] if ol else None) or (prev.outlook if prev else None),
            f"{act.group(1)} {act.group(2)}" if act else None,
            row,
        )
    if "Moody's" not in out:
        for m in _MOODYS_ONLY.finditer(text):
            ol = _OUTLOOK.search(m.group(2) or "")
            out["Moody's"] = AgencyRating(
                "Moody's", m.group(1), OUTLOOKS[ol.group(1).lower()] if ol else None, None, row
            )
            break
    return [out[a] for a in AGENCIES if a in out]


UNSECURED_KINDS = ("bond", "convert", "note")
_NOT_ITS_OWN = re.compile(r"backstop|guarant|^\s*[\w.,&' -]{0,40}\bunrated\b", re.I)


def rating_rows(conn: Conn, day: date, entity_id: str) -> list[Row]:
    """Rating facts about the company itself, newest first: rating texts on its own unsecured notes or
    texts that state the issuer rating ('Issuer: ...'; a secured facility's own rating is not the
    company's), then the tenant rating of the sites it leases (not when the text rates a backstop or says
    the tenant is unrated)."""
    insts = conn.execute("SELECT id, kind FROM instrument WHERE issuer_entity_id = %s", (entity_id,)).fetchall()
    kinds = {r["id"]: r["kind"] for r in insts}
    sites = [
        r["site_slug"]
        for r in conn.execute(
            "SELECT site_slug FROM chain_party WHERE entity_id = %s AND role = 'tenant'", (entity_id,)
        ).fetchall()
    ]
    rows = [
        r
        for r in (pit.fact_rows(conn, day, "instrument", list(kinds), ["rating"]) if kinds else [])
        if isinstance(r["value"], str) and (kinds.get(r["subject_id"]) in UNSECURED_KINDS or "Issuer:" in r["value"])
    ]
    rows += [
        r
        for r in (pit.fact_rows(conn, day, "site", sites, ["tenant_rating"]) if sites else [])
        if isinstance(r["value"], str) and not _NOT_ITS_OWN.search(r["value"])
    ]
    return sorted(rows, key=lambda r: (r["valid_from"], r["subject_type"] == "instrument"), reverse=True)


def ratings(conn: Conn, day: date, entity_id: str) -> dict[str, AgencyRating]:
    """agency -> the rating most of the newest texts naming that agency state (outlook and last action from
    any text that states them for that grade, newest first)."""
    parsed: dict[str, list[AgencyRating]] = {}
    for r in rating_rows(conn, day, entity_id):
        if isinstance(r["value"], str):
            for ar in parse_ratings(r["value"], r):
                parsed.setdefault(ar.agency, []).append(ar)
    out: dict[str, AgencyRating] = {}
    for agency, items in parsed.items():
        newest = max(i.row["valid_from"] for i in items)
        grade = Counter(i.rating for i in items if i.row["valid_from"] == newest).most_common(1)[0][0]
        same = [i for i in items if i.rating == grade]
        best = same[0]
        outlook = next((i.outlook for i in same if i.outlook), None)
        action = next((i.action for i in same if i.action), None)
        out[agency] = AgencyRating(agency, grade, outlook, action, best.row)
    return out


def lowest_notch(rs: dict[str, AgencyRating]) -> int | None:
    got = [n for r in rs.values() if (n := notch(r.rating)) is not None]
    return max(got) if got else None
