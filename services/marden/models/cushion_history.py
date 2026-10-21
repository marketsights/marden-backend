"""Slip history for the cushion model: how far planned dates moved, as a share of the time left.

One record per project: a plan stated on day t0 for month T0, and what a later source said on day t1 (a
revised plan T1, a reaffirmation, or the month it was done). Its slip ratio is
(T1 - T0) / max(T0 - t0, MIN_HORIZON months): 0 when the date held, 0.5 when a plan 12 months out moved
6 months, negative when it was pulled in. A pending milestone planned P months ahead is then simulated
at P + ratio x max(P - today, MIN_HORIZON), with the ratio drawn from these records (cushion_sim). The
horizon floor keeps a plan stated weeks before its date from turning a one-month move into a huge ratio.

Sources, all point in time (marden.models.pit):
- EIA-860M (subject_type 'eia_generator', collectors/eia_860m): each generator's first planned_online and
  its later operating_month (done) or latest planned_online, dated by each report's public date. The
  collector writes changes only, so a generator re-reported unchanged adds no record (slips are
  over-represented until unchanged reports are recorded); a single stored report yields no record.
  Category 'power' (generation in service).
- Marden's research archive: the dated ready-for-service statements per project (`rfs_statement`
  {project, which: prior | latest, text}): the earliest 'prior' statement with a date ahead of it
  against the last 'latest' statement day (its headline date ahead, else 'on track / no change' = the
  plan held, else a delivery). Grid and shell clauses are not read (a statement's 'energization Q1 2027'
  is not its hall date). One record per project text, so two sites sharing a lease add it once.
  Category 'build'. Where a site has no such pair, `original_energization` (when its text names the day
  the plan was given) against `expected_energization`.

Records of the site being projected are left out of its own pool (its current plan already reflects its
own slips). A category with fewer than MIN_RECORDS records uses the pooled records of every category.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import fmean

from marden.core.db import Conn, Row
from marden.models import cushion_dates as cd
from marden.models.cushion_inputs import text_of
from marden.models.cushion_milestones import NOT_DONE, done_when
from marden.models.pit import fact_rows, public_date

MIN_RECORDS = 5
MIN_POOLED = 3
MIN_HORIZON = 3.0
STRONG_REAFFIRM = re.compile(r"no change|unchanged|no slippage", re.I)
WEAK_REAFFIRM = re.compile(r"on track|on schedule|remains? on (?:our )?(?:planned )?schedule", re.I)


def month_float(day: date) -> float:
    return cd.month_of(day) + (day.day - 1) / 31


@dataclass(frozen=True)
class SlipRecord:
    source: str
    subject: str
    category: str
    said_on: date
    planned: float
    revised_on: date
    revised: float
    done: bool
    project: str = ""

    @property
    def ratio(self) -> float:
        return (self.revised - self.planned) / max(self.planned - month_float(self.said_on), MIN_HORIZON)

    def doc(self) -> dict[str, object]:
        return {"source": self.source, "subject": self.subject, "project": self.project, "category": self.category,
                "saidOn": self.said_on.isoformat(), "planned": cd.ym(int(self.planned)),
                "revisedOn": self.revised_on.isoformat(), "revised": cd.ym(int(self.revised)),
                "done": self.done, "ratio": round(self.ratio, 4)}


def _ym_value(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    w = cd.leading(cd.targets(value))
    return w.mid if w is not None else None


def eia_records(conn: Conn, as_of: date) -> list[SlipRecord]:
    rows = fact_rows(conn, as_of, "eia_generator", None, ("planned_online", "operating_month"))
    by_gen: dict[str, dict[str, list[Row]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_gen[r["subject_id"]][r["field"]].append(r)
    out: list[SlipRecord] = []
    for gen, fields in by_gen.items():
        planned = sorted(fields.get("planned_online", []), key=lambda r: r["valid_from"])
        if not planned:
            continue
        first = planned[0]
        t0, p0 = public_date(first), _ym_value(first["value"])
        if p0 is None:
            continue
        done = sorted(fields.get("operating_month", []), key=lambda r: r["valid_from"])
        last, is_done = (done[0], True) if done else (planned[-1], False)
        t1, p1 = public_date(last), _ym_value(last["value"])
        if p1 is None or t1 <= t0:
            continue
        out.append(SlipRecord("eia_860m", gen, "power", t0, p0, t1, p1, is_done, gen))
    return out


def read_statement(row: Row, as_of: date) -> tuple[str | None, cd.When | None]:
    """('target', the headline hall / delivery date still ahead of the statement), ('reaffirm', None) for
    'no change' or a dateless 'on track', ('done', when) for a delivery, else (None, None)."""
    text = text_of(row)
    said = cd.month_of(row["valid_from"])
    if STRONG_REAFFIRM.search(text):
        return "reaffirm", None
    ahead = [w for w in cd.targets(text) if w.hi >= said and cd.clause_kind(text, w) in ("hall", "rent")]
    if ahead:
        return "target", cd.leading(ahead)
    if WEAK_REAFFIRM.search(text):
        return "reaffirm", None
    if not NOT_DONE.search(text):
        when = done_when(text, as_of, fallback=row["valid_from"])
        if when is not None:
            return "done", when
    return None, None


def _project_record(slug: str, project: str, rows: list[Row], as_of: date) -> SlipRecord | None:
    priors: list[tuple[date, cd.When]] = []
    latest: dict[date, list[tuple[str, cd.When | None]]] = defaultdict(list)
    for r in rows:
        kind, when = read_statement(r, as_of)
        which = (r["value"] or {}).get("which") if isinstance(r["value"], dict) else None
        if which == "latest":
            if kind is not None:
                latest[r["valid_from"]].append((kind, when))
        elif kind == "target" and when is not None:
            priors.append((r["valid_from"], when))
    if not priors or not latest:
        return None
    t0, w0 = min(priors, key=lambda p: (p[0], p[1].lo))
    t1 = max(latest)
    if t1 <= t0:
        return None
    said = latest[t1]
    ahead = [w.mid for k, w in said if k == "target" and w is not None]
    if ahead:
        return SlipRecord("archive:rfs_statement", slug, "build", t0, w0.mid, t1, fmean(ahead), False, project)
    if any(k == "reaffirm" for k, _ in said):
        return SlipRecord("archive:rfs_statement", slug, "build", t0, w0.mid, t1, w0.mid, False, project)
    done = [w for k, w in said if k == "done" and w is not None]
    return SlipRecord("archive:rfs_statement", slug, "build", t0, w0.mid, t1, done[0].lo + 0.5, True, project)


def archive_records(conn: Conn, as_of: date) -> list[SlipRecord]:
    rows = fact_rows(conn, as_of, "site", None,
                     ("rfs_statement", "original_energization", "expected_energization"))
    by_site: dict[str, dict[str, list[Row]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_site[r["subject_id"]][r["field"]].append(r)
    out: list[SlipRecord] = []
    seen_projects: set[str] = set()
    for slug, fields in sorted(by_site.items()):
        projects: dict[str, list[Row]] = defaultdict(list)
        for r in fields.get("rfs_statement", []):
            name = (r["value"] or {}).get("project") if isinstance(r["value"], dict) else None
            projects[str(name or slug)].append(r)
        found = False
        for project, rows_ in sorted(projects.items()):
            if project in seen_projects:
                continue
            record = _project_record(slug, project, rows_, as_of)
            if record is not None:
                out.append(record)
                seen_projects.add(project)
                found = True
        if not found:
            record = _energization_record(slug, fields, as_of)
            if record is not None:
                out.append(record)
    return out


def _energization_record(slug: str, fields: dict[str, list[Row]], as_of: date) -> SlipRecord | None:
    orig = max(fields.get("original_energization", []), key=lambda r: r["valid_from"], default=None)
    exp = max(fields.get("expected_energization", []), key=lambda r: r["valid_from"], default=None)
    if orig is None or exp is None:
        return None
    said = cd.statement_dates(text_of(orig))
    w0, w1 = cd.leading(cd.targets(text_of(orig))), cd.leading(cd.targets(text_of(exp)))
    if not said or w0 is None or w1 is None:
        return None
    t0 = cd.month_start(said[0].lo)
    if t0 >= exp["valid_from"] or w0.hi < said[0].lo:
        return None
    return SlipRecord("archive:energization", slug, "build", t0, w0.mid, exp["valid_from"], w1.mid, False,
                      "original vs expected energization")


@dataclass
class SlipHistory:
    records: list[SlipRecord]
    eia_reports: int = 0  # EIA-860M monthly reports public by the run date

    def pool(self, category: str, exclude_site: str) -> tuple[list[float], str]:
        """Slip ratios for a milestone category, without the projected site's own records."""
        own = [r for r in self.records if r.subject != exclude_site]
        same = [r.ratio for r in own if r.category == category]
        if len(same) >= MIN_RECORDS:
            return same, category
        pooled = [r.ratio for r in own]
        if len(pooled) >= MIN_POOLED:
            return pooled, "pooled"
        return [], "none"

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for r in self.records:
            out[r.source] += 1
            out[f"category:{r.category}"] += 1
        return dict(sorted(out.items()))


def eia_report_count(conn: Conn, as_of: date) -> int:
    rows = conn.execute(
        """SELECT DISTINCT valid_from, source_name, subject_type, producer FROM fact
            WHERE subject_type = 'eia_generator' AND status IN ('published', 'superseded') AND valid_from <= %s""",
        (as_of,)).fetchall()
    return len({r["valid_from"] for r in rows if public_date(r) <= as_of})


def load_history(conn: Conn, as_of: date) -> SlipHistory:
    return SlipHistory(eia_records(conn, as_of) + archive_records(conn, as_of), eia_report_count(conn, as_of))
