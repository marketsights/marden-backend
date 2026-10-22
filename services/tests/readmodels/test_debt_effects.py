"""Read model `site_debt_effects` (docs/methods/event-impact-v0.1.md): every record on a site's Material events and
Evidence tabs gets its effect on the debt's value, from the `event_impact` model's output; a record that changed
nothing says why.

Rows: the real Oracle force-majeure report of 2026-09-26 (Dealroom via Google News) and a procedural NMED docket
filing on Project Jupiter, with the event impact model's real output for 2026-09-24 (the price model counted the
force-majeure notice that day: the Jupiter construction loans 93.193 without it, 92.359 with it, YTW +0.29%).
"""

from __future__ import annotations

from datetime import date

from marden.core import db
from marden.core.records import EventWriter
from marden.models import runs
from marden.readmodels.context import building
from marden.readmodels.views.debt_effects import site_effects
from marden.readmodels.views.debt_effects_text import NOT_READ

FM = "Oracle files force majeure on 2.45GW New Mexico Stargate data center - Dealroom"
DOCKET = "Notice of Appearance of Counsel for Mr. Maxwell"
LOAN = "EX-JUPITER-190"


def _effect(pts: float, without: float, with_: float, y: float) -> dict:
    return {"instrumentId": LOAN, "with": with_, "without": without, "changePts": pts, "stored": None, "measure": "YTW",
            "yieldWith": None, "yieldWithout": None, "yieldChangePct": y, "dmWithBp": None, "dmWithoutBp": None,
            "why": None}


OUTPUT = {
    "instruments": [LOAN], "related": [], "countedDays": {"force_majeure": ["2026-09-24"]}, "missing": [],
    "changes": [{
        "key": "signals:2026-09-24", "kind": "signals", "date": "2026-09-24", "kinds": ["force_majeure"],
        "label": "Reported: a force-majeure notice citing power", "why": None, "market": [],
        "records": [{"date": "2026-09-24", "text": "Oracle sent force majeure notice to Blue Owl unit citing "
                     "power-supply delays", "tag": "R", "sourceName": "Cp24", "sourceUrl": "https://cp24.example/fm",
                     "kind": "force_majeure", "kindWords": "force-majeure notice citing power"}],
        "effects": [_effect(-2.403, 94.762, 92.359, 0.943)],
        "byKind": {"force_majeure": [_effect(-0.834, 93.193, 92.359, 0.29)]},
    }],
}


def _setup(conn) -> None:
    conn.execute("INSERT INTO site (slug, name) VALUES ('jupiter', 'Project Jupiter (Stargate New Mexico)')")
    conn.execute("""INSERT INTO instrument (id, name, kind, issuer_name, site_slug, in_v1)
                    VALUES (%s, 'Construction loans', 'construction_loan', 'Red Chiles A-D, LLC', 'jupiter', true)""",
                 (LOAN,))
    for day, title, kind, sev in (("2026-09-26", FM, "news", "red"), ("2026-10-02", DOCKET, "docket", None)):
        eid = EventWriter(conn).add("site", "jupiter", day, kind, title, tag="R", source_name="Google News",
                                    source_url=f"https://example.test/{day}", status="published").id
        conn.execute("UPDATE event SET severity = %s, learned_at = %s WHERE id = %s", (sev, f"{day} 12:00-04", eid))
    run_id = runs.start_run("event_impact", "0.1", trigger="test", conn=conn)
    runs.write_output(conn, run_id, "event_impact", "0.1", "site", "jupiter", date(2026, 10, 22), OUTPUT)


def test_records_get_their_effect_on_the_debt_or_the_reason_there_is_none():
    with db.transaction() as conn:
        _setup(conn)
    with building(date(2026, 10, 22)), db.transaction() as conn:
        doc = site_effects(conn, "jupiter")
    fm = doc["effects"][f"2026-09-26|{FM}"]
    assert fm["changed"] is True and fm["effectiveDate"] == "2026-09-24"
    assert fm["line"] == ("Effect on debt: Project Jupiter Construction loans \u22120.8 pts (93.2 \u2192 92.4), "
                          "YTW +0.3%")
    eff = fm["effects"][0]
    assert eff["change"]["tag"] == "M" and eff["change"]["value"] == -0.834
    assert fm["inputs"][0]["sourceName"] == "Cp24"
    docket = doc["effects"].get(f"2026-10-02|{DOCKET}", doc["default"])
    assert docket["changed"] is False and docket["line"] == "No change to debt value"
    assert docket["reason"].startswith("Procedural filing") or docket["reason"] == NOT_READ


def test_a_site_without_the_model_says_not_computed_yet():
    with db.transaction() as conn:
        conn.execute("INSERT INTO site (slug, name) VALUES ('jupiter', 'Project Jupiter (Stargate New Mexico)')")
    with building(date(2026, 10, 22)), db.transaction() as conn:
        doc = site_effects(conn, "jupiter")
    assert doc["default"]["reason"].startswith("Not computed yet")
