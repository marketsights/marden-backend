"""Opposition tab: a published opposition step still to come is listed and sets the next hearing (spec 04:
"KPI strip: ... next hearing"). The alert engine sends such an event as news the day it is approved and links
it to this tab, so the tab must show it.

Real rows: NMED's Notice of Hearing on Motions to Dismiss in AQB 26-57 (P), Yucca Growth Infrastructure's air
permit NSR 10883 for the Project Jupiter microgrid (filed 2026-10-16; hearing "8:30AM MDT on Monday, October 26,
2026"), as the review queue published it on 2026-10-21 (ET), and the Oracle release of 2026-10-15 already on
the tab.
"""

from __future__ import annotations

from datetime import date

from marden.core import db
from marden.core.records import EventWriter
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import sites_tabs  # noqa: F401 (registers the builders)

NOH_URL = ("https://www.env.nm.gov/opf/wp-content/uploads/sites/13/2026/07/"
           "2026-10-16-AQB-26-57-NOH-on-MTDs-filed.pdf")
HEARING = ("Opposition event: hearing hearing on the pending Motions to Dismiss will commence promptly at 8:30AM "
           "MDT on Monday, October 26, 2026 New Mexico Environment Department, Office of Public Facilitation")
RELEASE = ("Project Jupiter Microgrid Cuts Permitted Emissions by 35%, Including Greenhouse Gases, in Response to "
           "NMED Review and Public Comment - Oracle")


def _event(conn, day: str, title: str, tag: str, source: str, url: str, learned: str) -> int:
    eid = EventWriter(conn).add("site", "jupiter", day, "opposition", title, tag=tag, source_name=source,
                                source_url=url, status="published").id
    conn.execute("UPDATE event SET learned_at = %s WHERE id = %s", (learned, eid))
    return eid


def _tab(as_of: date) -> dict:
    b = get_builder("site_tab:opposition")
    assert b is not None
    with building(as_of), db.transaction() as conn:
        return b.build_one(conn, "jupiter")


def test_an_upcoming_hearing_is_listed_and_is_the_next_hearing():
    with db.transaction() as conn:
        conn.execute("INSERT INTO site (slug, name) VALUES ('jupiter', 'Project Jupiter (Stargate New Mexico)')")
        _event(conn, "2026-10-15", RELEASE, "R", "Google News", "https://news.google.com/", "2026-10-21 12:00-04")
        _event(conn, "2026-10-26", HEARING, "F", "www.env.nm.gov", NOH_URL, "2026-10-21 23:38-04")
    tab = _tab(date(2026, 10, 21))
    dates = [i["date"] for i in tab["items"]]
    assert dates == ["2026-10-26", "2026-10-15"]  # newest first: the step to come on top
    hearing = tab["items"][0]
    assert hearing["where"] == "www.env.nm.gov" and hearing["status"] == "n/d"  # filed: the body is the source
    assert hearing["evidence"]["sourceUrl"] == NOH_URL
    kpi = tab["nextHearing"]
    assert kpi["value"] == {"value": "2026-10-26", "tag": "F", "asOf": "2026-10-21", "sourceName": "www.env.nm.gov",
                            "sourceUrl": NOH_URL}
    assert kpi["note"].startswith("Opposition event: hearing") and len(kpi["note"]) <= 91


def test_a_hearing_marden_learned_later_is_not_on_an_earlier_days_tab():
    with db.transaction() as conn:
        conn.execute("INSERT INTO site (slug, name) VALUES ('jupiter', 'Project Jupiter (Stargate New Mexico)')")
        _event(conn, "2026-10-26", HEARING, "F", "www.env.nm.gov", NOH_URL, "2026-10-21 23:38-04")
    tab = _tab(date(2026, 10, 20))
    assert tab["items"] == [] and tab["nextHearing"]["value"]["value"] is None


def test_a_hearing_once_held_is_listed_but_no_longer_next():
    with db.transaction() as conn:
        conn.execute("INSERT INTO site (slug, name) VALUES ('jupiter', 'Project Jupiter (Stargate New Mexico)')")
        _event(conn, "2026-10-26", HEARING, "F", "www.env.nm.gov", NOH_URL, "2026-10-21 23:38-04")
    tab = _tab(date(2026, 10, 27))
    assert [i["date"] for i in tab["items"]] == ["2026-10-26"]
    assert tab["nextHearing"]["value"]["value"] is None
