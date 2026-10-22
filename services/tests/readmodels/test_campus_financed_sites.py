"""A campus that finances several sites carries all of them: `financedSites` (types.ts DataCenterRow), most
debt first, with `financed` the first of them (the readers that show one link).

On the real rows (facility_rows.copy_facilities): Galaxy Helios finances Helios Phase I and II, and Applied
Digital's Polaris Forge 1 campus finances Polaris Forge 1 (buildings 2 and 3) and its building 4. Showing only
the larger site hid Helios I's and building 4's debt from the Data centers summary. Each site's debt and chain
status are read here independently of the builder: the sum of the site's instruments' `amount_usd` (in $B, the
instruments that count toward debt: not repaid / proposed / cancelled, not a holder's par) and the newest
`status` model output.
"""

from __future__ import annotations

import pytest
from facility_rows import copy_facilities
from real_rows import copied_rows

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import facilities_campus  # noqa: F401 (registers builders)

CAMPUSES = {"galaxy-helios": {"helios-1", "helios-2"},
            "apld-polaris-forge-1": {"polaris-forge-1", "polaris-forge-1-building-4"}}
NOT_COUNTED = ("repaid", "proposed", "cancelled", "canceled")


def _build(view: str, key: str):
    with building(business_today()), db.transaction() as conn:
        return get_builder(view).build_one(conn, key)


def _approved(campus: str) -> set[str]:
    return {r["site_slug"] for r in copied_rows(
        "SELECT site_slug FROM site_link WHERE campus_id = %s AND status = 'approved'", (campus,))}


def _site_debt_b(slug: str) -> float | None:
    rows = copied_rows(
        """SELECT (a.value #>> '{}')::float8 AS usd FROM instrument i
             JOIN fact_current a ON a.subject_type = 'instrument' AND a.subject_id = i.id AND a.field = 'amount_usd'
             LEFT JOIN fact_current s ON s.subject_type = 'instrument' AND s.subject_id = i.id AND s.field = 'status'
             LEFT JOIN fact_current b ON b.subject_type = 'instrument' AND b.subject_id = i.id
                                     AND b.field = 'amount_basis'
            WHERE i.site_slug = %s AND jsonb_typeof(a.value) = 'number'
              AND coalesce(s.value #>> '{}', '') <> ALL(%s) AND coalesce(b.value #>> '{}', '') <> 'holder_par'""",
        (slug, list(NOT_COUNTED)))
    return sum(r["usd"] for r in rows) / 1e9 if rows else None


def _status(slug: str) -> str | None:
    rows = copied_rows(
        """SELECT output ->> 'status' AS status FROM model_output WHERE model = 'status' AND subject_type = 'site'
             AND subject_id = %s ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""", (slug,))
    return rows[0]["status"] if rows else None


@pytest.mark.parametrize("campus", sorted(CAMPUSES))
def test_every_financed_site_on_the_campus_is_listed(campus):
    copy_facilities()
    if _approved(campus) != CAMPUSES[campus]:
        pytest.skip(f"{campus}'s two site links are not approved in the source database")
    row = _build("dc_row", campus)
    sites = row["financedSites"]
    assert {s["siteSlug"] for s in sites} == CAMPUSES[campus]
    debts = [s["debtB"]["value"] for s in sites]
    assert debts == sorted(debts, reverse=True) and row["financed"] == sites[0]  # most debt first; primary
    for s in sites:
        expected = _site_debt_b(s["siteSlug"])
        assert s["debtB"]["value"] == pytest.approx(expected, abs=0.0005), s["siteSlug"]  # billions, 3 dp
        assert s["chainStatus"] == _status(s["siteSlug"])
    # the campus's financed debt is the sum over both sites, not the larger one alone
    total = sum(_site_debt_b(slug) for slug in CAMPUSES[campus])
    assert sum(debts) == pytest.approx(total, abs=0.001) and sum(debts) > row["financed"]["debtB"]["value"]
    detail = _build("dc_detail", campus)  # DataCenterDetail extends DataCenterRow
    assert detail["financedSites"] == sites and detail["financed"] == row["financed"]


def test_a_campus_with_one_or_no_financed_site():
    campuses = copy_facilities()
    one = next((c for c in sorted(campuses) if len(_approved(c)) == 1), None)
    none = next((c for c in sorted(campuses) if not _approved(c)), None)
    if one is None or none is None:
        pytest.skip("no campus with exactly one (or no) approved site link")
    row = _build("dc_row", one)
    if row is not None:  # US campuses only
        assert len(row["financedSites"]) == 1 and row["financedSites"][0] == row["financed"]
    row = _build("dc_row", none)
    if row is not None:
        assert row["financedSites"] == [] and row["financed"] is None
