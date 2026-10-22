"""A site link approved on the review queue counts from the day it was approved, in business time.

Snapshots are dated in US Eastern; the database session is UTC. A link approved in the Eastern evening
(after midnight UTC) is known on that Eastern day, and one approved after Eastern midnight is not. The
rows are the real Stargate Abilene pair the reviewer approved (site 'abilene', campus 'stargate-abilene').
"""

from __future__ import annotations

from datetime import date

import pytest

from marden.readmodels.context import building
from marden.readmodels.views.facilities_common import approved_sites
from marden.readmodels.views.leases import site_campus
from marden.readmodels.views.sites_links import linked_campuses

DAY = date(2026, 10, 1)  # a past business day (a backfill), so the snapshot date is fixed


def _approve(conn, at: str) -> None:
    conn.execute("INSERT INTO site (slug, name) VALUES ('abilene', 'Stargate Abilene')")
    conn.execute("INSERT INTO campus (id, name) VALUES ('stargate-abilene', 'Stargate Abilene')")
    conn.execute(
        """INSERT INTO site_link (site_slug, campus_id, status, approved_by, approved_at)
           VALUES ('abilene', 'stargate-abilene', 'approved', 'claude-review', %s::timestamptz)""", (at,))


def _seen(conn, day: date) -> tuple[bool, bool, bool]:
    with building(day):
        return (
            [r["slug"] for r in approved_sites(conn, ["stargate-abilene"])] == ["abilene"],
            [r["id"] for r in linked_campuses(conn, "abilene")[0]] == ["stargate-abilene"],
            site_campus(conn, "abilene") == "stargate-abilene",
        )


@pytest.mark.parametrize(("approved_at", "on_day", "next_day"), [
    ("2026-10-02 00:46+00", True, True),   # 20:46 Eastern on 1 Oct: known on 1 Oct
    ("2026-10-02 03:59+00", True, True),   # 23:59 Eastern on 1 Oct
    ("2026-10-02 04:01+00", False, True),  # 00:01 Eastern on 2 Oct: not yet on 1 Oct
])
def test_the_link_counts_from_its_eastern_approval_day(conn, approved_at, on_day, next_day):
    conn.execute("SET TIME ZONE 'UTC'")
    _approve(conn, approved_at)
    assert _seen(conn, DAY) == (on_day,) * 3
    assert _seen(conn, date(2026, 10, 2)) == (next_day,) * 3
