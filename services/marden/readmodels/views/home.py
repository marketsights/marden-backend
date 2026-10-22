"""Read model `home` ('all'): the shared, watchlist-independent pieces of GET /api/home (spec 01 Home).

Home shows what moved on the user's names. The names are per org, so this one document holds the
pieces for every site and instrument, each tagged with the watchlist entries it belongs to (`watch`,
views/home_parts.py), and the API keeps the org's names, sums the headline numbers over them and adds
the unread alerts (apps/api/src/lib/home-doc.ts). Shape: HomeDoc in apps/api/src/contracts/app.ts.

- `under3`: sites whose cushion (model 'cushion', the same value as Sites and Completion) is under 3
  months, with the site's debt, plus one entry per instrument on such a site with its own size (a
  watched loan counts its size; a watched site its whole debt). `cushionKnown`: the entries whose
  site has a cushion value at all, so "none under 3" can be told apart from n/d.
- `statusChanges`: one line per site whose chain status or a link status moved this week (model
  'status' on the build day vs its output of seven days earlier).
- `fundMarksHigh`: instruments whose average filed fund mark is 5+ points above the Marden price
  (model 'mark_gap' markGap <= -5), largest gap first. n/d until the price model has priced them.
- `changes`: HomeChange rows of the week (status, cushion, price moves of 1+ point, filings), each with
  its severity (3 red, 2 amber, 1 other), most severe and newest first.
- `comingUp`: scheduled steps after the build day and the next public fund marks, soonest first.

Values are read, never recomputed: site summaries come from views/sites.site_summary and instrument
rows from views/instruments.instrument_row, so Home shows the numbers the list screens show.
"""

from __future__ import annotations

from datetime import date, timedelta

from marden.core.db import Conn, Row
from marden.readmodels.context import known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.home_parts import (
    coming_up_event,
    cushion_change,
    filing_change,
    fund_marks_due,
    merge_events,
    price_change,
    site_watch,
    status_changes,
    week_events,
)
from marden.readmodels.views.instrument_inputs import day, instrument_identity, num
from marden.readmodels.views.instrument_text import short_name
from marden.readmodels.views.instruments import INSTRUMENT_FACTS, instrument_row, size_b
from marden.readmodels.views.monitor_common import Doc
from marden.readmodels.views.sites import site_summary
from marden.readmodels.views.sites_common import counts_toward_debt, site_events, site_instruments
from marden.readmodels.views.sites_common import site_rows as all_sites
from marden.readmodels.views.sites_evidence import last_evidence

UNDER_MONTHS = 3.0
MARK_GAP_PTS = -5.0  # fund marks 5+ points above the Marden price
WEEK_DAYS = 7


def output_on(conn: Conn, model: str, subject_type: str, subject_id: str, when: date) -> Row | None:
    """The newest output of `model` for the subject dated on or before `when`."""
    return conn.execute(
        """SELECT * FROM model_output WHERE model = %s AND subject_type = %s AND subject_id = %s AND as_of <= %s
            ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
        (model, subject_type, subject_id, when),
    ).fetchone()


class Pieces:
    """The lists of the home document, filled site by site and instrument by instrument."""

    def __init__(self) -> None:
        self.under3: list[Doc] = []
        self.cushion_known: list[str] = []
        self.status_changes: list[Doc] = []
        self.fund_marks_high: list[Doc] = []
        self.changes: list[Doc] = []
        self.coming_up: list[Doc] = []
        self.subjects: dict[tuple[str, str], Doc] = {}


def add_instruments(conn: Conn, pieces: Pieces) -> dict[str, Doc]:
    """Every instrument's row (as the Instruments list shows it): fund marks above Marden, price moves
    and the next public marks. Returns the rows by id."""
    rows: dict[str, Doc] = {}
    for inst in instrument_identity(conn):
        iid = inst["id"]
        row, ctx = instrument_row(conn, inst, known_facts(conn, "instrument", iid, INSTRUMENT_FACTS))
        rows[iid] = row
        watch = [f"instrument:{iid}"]
        pieces.subjects[("instrument", iid)] = {"kind": "instrument", "id": iid, "name": row["name"],
                                                "status": row["chainStatus"], "watch": watch,
                                                "href": f"/instruments/{iid}"}
        gap = row.get("markGap")
        if isinstance(gap, dict) and num(gap.get("value")) is not None and gap["value"] <= MARK_GAP_PTS:
            pieces.fund_marks_high.append({"watch": watch, "name": row["name"], "gapPts": gap})
        if (change := price_change(row, watch)) is not None:
            pieces.changes.append(change)
        if (due := fund_marks_due(row, ctx["gap_out"], watch)) is not None:
            pieces.coming_up.append(due)
    return rows


def add_site(conn: Conn, site: Row, pieces: Pieces, names: dict[str, str]) -> None:
    slug, today = site["slug"], day()
    instruments = site_instruments(conn, slug)
    summary = site_summary(conn, site, instruments=instruments)
    watch = site_watch(slug, [i["id"] for i in instruments])
    pieces.subjects[("site", slug)] = {"kind": "site", "id": slug, "name": summary["name"],
                                       "status": summary["status"], "watch": watch, "href": f"/sites/{slug}"}
    week_ago = today - timedelta(days=WEEK_DAYS)
    cushion = summary.get("cushionMonths")
    months = num(cushion.get("value")) if isinstance(cushion, dict) else None
    if months is not None:
        pieces.cushion_known += watch
        if months < UNDER_MONTHS:
            pieces.under3.append({"watch": [watch[0]], "name": short_name(summary["name"]) or summary["name"],
                                  "debtB": summary["debtB"],
                                  "cushionMonths": cushion})
            for inst in instruments:
                if counts_toward_debt(inst):
                    pieces.under3.append({"watch": [f"instrument:{inst['id']}"], "site": slug,
                                          "name": names.get(inst["id"], inst["name"]), "debtB": size_b(inst["facts"]),
                                          "cushionMonths": cushion})
    line, changes = status_changes(summary, watch, output_on(conn, "status", "site", slug, today),
                                   output_on(conn, "status", "site", slug, week_ago))
    if line is not None:
        pieces.status_changes.append(line)
    pieces.changes += changes
    record = last_evidence(conn, slug, site_events(conn, slug))
    moved = cushion_change(summary, watch, output_on(conn, "cushion", "site", slug, today),
                           output_on(conn, "cushion", "site", slug, week_ago), record)
    if moved is not None:
        pieces.changes.append(moved)


def _soonest(item: Doc) -> tuple[str, str, bool]:
    """Soonest first; a step on a site before its copies on the site's instruments."""
    return item["date"], item["what"], not item["refUrl"].startswith("/sites/")


def home_doc(conn: Conn) -> Doc:
    today = day()
    start = today - timedelta(days=WEEK_DAYS - 1)
    pieces = Pieces()
    rows = add_instruments(conn, pieces)
    names = {iid: row["name"] for iid, row in rows.items()}
    for site in all_sites(conn):
        add_site(conn, site, pieces, names)
    for entry in merge_events(week_events(conn, start), pieces.subjects):
        if entry["event"]["kind"] == "filing":
            pieces.changes.append(filing_change(entry))
        else:
            pieces.coming_up.append(coming_up_event(entry))
    pieces.under3.sort(key=lambda e: (e["cushionMonths"]["value"], "site" in e, e["name"]))
    # Most severe first, then newest, then by name (stable sorts, last key first).
    pieces.changes.sort(key=lambda c: c["name"])
    pieces.changes.sort(key=lambda c: c["record"]["date"], reverse=True)
    pieces.changes.sort(key=lambda c: -c["severity"])
    return {
        "asOf": today.isoformat(),
        "week": {"start": start.isoformat(), "end": today.isoformat()},
        "under3": pieces.under3,
        "cushionKnown": sorted(set(pieces.cushion_known)),
        "statusChanges": sorted(pieces.status_changes, key=lambda s: s["name"]),
        "fundMarksHigh": sorted(pieces.fund_marks_high, key=lambda f: (f["gapPts"]["value"], f["name"])),
        "changes": pieces.changes,
        "comingUp": sorted(pieces.coming_up, key=_soonest),
    }


@builder("home", many=True)
def build_home(conn: Conn):
    yield "all", home_doc(conn)
