"""Spreads at issue are computed in code, never taken from the legacy archive's own arithmetic (rule 7).

Tiny cases for the formula, then the real issue comps copied from the local `marden` database: every comp
whose stored spread is the archive's computation (tag M) is recomputed from its deal terms and the U.S.
Treasury curve of its issue day, and the tier fit uses only stated (F/R) or recomputed spreads.
"""

from __future__ import annotations

from datetime import date

import pytest
from price_rows import copy_price_inputs

from marden.core import db
from marden.models import price_tiers
from marden.models.issue_spreads import bullet_yield, issue_spread
from marden.models.price_curve import load_curves
from marden.models.price_terms import load_comps


class Flat:
    def __init__(self, pct: float) -> None:
        self.pct = pct

    def par_yield(self, years: float) -> float:
        return self.pct


def test_bullet_yield_and_spread_rules():
    assert bullet_yield(6.0, 100.0, 5) == pytest.approx(6.0, abs=1e-6)
    assert bullet_yield(6.75, 98.0, 5) == pytest.approx(7.23, abs=0.01)  # APLD ComputeCo 2: 6.75% at 98
    assert issue_spread(250, "F", rate_text="SOFR + 2.50%", coupon_pct=None, issue_price=None, tenor_years=4,
                        curve=None) == (250.0, "stated at issue")
    got, how = issue_spread(225, "M", rate_text="6.581% fixed (T+225 reported)", coupon_pct=6.581,
                            issue_price=100, tenor_years=23.6, curve=Flat(4.5))
    assert got == 225.0 and "stated in the deal terms" in how
    got, how = issue_spread(999, "M", rate_text="7.00% fixed", coupon_pct=7.0, issue_price=100, tenor_years=5,
                            curve=Flat(4.16))
    assert got == pytest.approx(284, abs=0.5) and "(computed)" in how  # never the stored 999
    assert issue_spread(350, "M", rate_text="", coupon_pct=None, issue_price=None, tenor_years=5,
                        curve=Flat(4.0))[0] is None


def test_real_comps_use_no_archive_computed_spread():
    copy_price_inputs(["EX-JUPITER-190"])  # copies every issue comp and the Treasury curve
    day = date(2026, 10, 21)
    with db.transaction() as conn:
        comps = load_comps(conn, day)
        book = load_curves(conn, day, since=price_tiers.SAMPLE_START)
        fit = price_tiers.fit(conn, day, comps, book)
    archive = {c["id"]: c for c in comps if c.get("issue_spread_tag") == "M"}
    if not archive:
        pytest.skip("no archive-computed issue spreads in the source database")
    used = {d.id: d for d in fit.deals}
    recomputed = [d for d in used.values() if d.id in archive]
    assert recomputed, "the archive's fixed-rate comps should be recomputed, not dropped"
    for d in recomputed:
        assert "(computed)" in d.basis or "stated in the deal terms" in d.basis, d
        if "(computed)" in d.basis:  # the code reproduces the legacy arithmetic on Marden's own curve
            assert d.spread_bp == pytest.approx(archive[d.id]["issue_spread_bp"], abs=3), d.id
