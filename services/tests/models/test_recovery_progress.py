"""Recovery v0.2: the share finished is estimated when no source states it (capex, schedule, imagery), the
unfinished works carry a haircut, and a funded project bond's unspent proceeds count as collateral."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from marden.models.recovery_progress import Estimate, _parse_start
from marden.models.recovery_shell import UNFINISHED_HAIRCUT, Claim, Comps, SiteFacts, shell_recovery


def _fact(fld: str, value: object, tag: str = "R") -> dict:
    return {"id": 1, "field": fld, "value": value, "tag": tag, "valid_from": date(2026, 1, 1),
            "learned_at": datetime(2026, 1, 1, tzinfo=UTC), "subject_type": "site", "subject_id": "s"}


COMPS = Comps([("a", 10e6), ("b", 12e6), ("c", 14e6)])


def test_a_stated_construction_start_is_read_from_the_records():
    assert _parse_start("Construction began ~Sep/Q4 2025; Oracle says on track") == date(2025, 9, 1)
    assert _parse_start("construction started in Q2 2026") == date(2026, 4, 1)
    assert _parse_start("Foundations under way") is None


def test_an_estimated_share_values_the_unfinished_works_with_a_haircut_and_counts_unspent_proceeds():
    site = SiteFacts("s", {"gross_mw": [_fact("gross_mw", 1000)], "delivered_mw": [_fact("delivered_mw", 0)]})
    claims = [Claim("BOND", 1, 10e9, "amount_usd", funded=True)]
    nd = shell_recovery(site, COMPS, claims, "BOND")
    assert nd.base is None and any("completion of the works" in m for m in nd.missing)  # v0.1 behaviour
    est = Estimate(0.3, "estimated", [{"key": "schedule", "share": 0.3, "label": "schedule", "detail": "x"}])
    got = shell_recovery(site, COMPS, claims, "BOND", progress=est)
    works = 1000 * 12e6 * 0.3 * (1 - UNFINISHED_HAIRCUT)
    unspent = 10e9 * (1 - 0.3)
    assert got.base == pytest.approx(min(1.0, (works + unspent) / 10e9))
    assert got.basis["completionEstimated"] is True and got.basis["completionEstimators"][0]["key"] == "schedule"
    assert got.basis["unspentProceedsUsd"] == pytest.approx(unspent)
    loan = [Claim("LOAN", 1, 10e9, "amount_usd", funded=False)]  # a construction loan draws as it builds
    assert shell_recovery(site, COMPS, loan, "LOAN", progress=est).base == pytest.approx(works / 10e9)


def test_delivered_capacity_is_valued_without_the_haircut_and_without_unspent_proceeds():
    site = SiteFacts("s", {"gross_mw": [_fact("gross_mw", 1000)], "delivered_mw": [_fact("delivered_mw", 250)]})
    claims = [Claim("BOND", 1, 10e9, "amount_usd", funded=True)]
    got = shell_recovery(site, COMPS, claims, "BOND")
    assert got.basis["completionBasis"] == "delivered_mw / capacity"
    assert got.base == pytest.approx(1000 * 12e6 * 0.25 / 10e9) and got.basis["unspentProceedsUsd"] == 0.0
