"""Cost of each month late (marden.models.late_cost): construction interest from stated terms, floating
over SOFR plus the stated spread, rent only while rent start is ahead, and a specific reason when n/d."""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import count

import pytest

from marden.models import late_cost as lc

_ids = count(1)


def row(field: str, value, tag: str = "F", day: str = "2026-06-30") -> dict:
    return {"id": next(_ids), "field": field, "value": value, "tag": tag, "source_name": "SEC EDGAR",
            "source_url": "https://www.sec.gov/x", "valid_from": date.fromisoformat(day), "subject_id": "i",
            "learned_at": datetime(2026, 10, 21, tzinfo=UTC)}


def facts(**fields) -> dict[str, list[dict]]:
    return {k: [row(k, v)] for k, v in fields.items()}


INST = {"id": "EX-1", "name": "Senior secured notes due 2030"}
SOFR = row("value", 3.88, day="2026-10-20")


def test_fixed_coupon_bond_interest_is_amount_times_rate_over_twelve():
    bond = facts(amount_usd=1_733_000_000, coupon_type="fixed", coupon_rate_pct=7.125)
    line, why = lc.interest_line(INST, bond, SOFR)
    assert why is None and line is not None
    assert line.monthly == pytest.approx(1_733_000_000 * 0.07125 / 12)
    assert line.rate_basis == "fixed 7.125%"
    assert line.amount_basis == "amount"


def test_floating_loan_uses_drawn_amount_and_sofr_plus_the_stated_spread():
    f = facts(amount_usd=413_000_000, drawn_usd=413_000_000, commitment_usd=1_545_000_000, coupon_type="floating",
              coupon_spread_bp=225)
    line, _ = lc.interest_line(INST, f, SOFR)
    assert line is not None
    assert line.rate_pct == pytest.approx(6.13)
    assert line.monthly == pytest.approx(413_000_000 * 0.0613 / 12)
    assert "SOFR 3.88%" in line.rate_basis and "+ 225 bp" in line.rate_basis
    assert line.amount_basis == "drawn of $1.54B committed"
    assert SOFR in line.rows  # the SOFR fixing is a cited input


def test_an_instrument_without_a_stated_rate_or_amount_is_left_out_with_the_reason():
    def why(**f):
        return lc.interest_line(INST, facts(**f), SOFR)[1]

    assert why(amount_usd=2_300_000_000) == "Senior secured notes due 2030: rate not public"
    assert why(coupon_rate_pct=7.0).endswith("amount not public")
    assert why(amount_usd=1e9, status="repaid", coupon_rate_pct=7.0).endswith("repaid")
    no_sofr = lc.interest_line(INST, facts(amount_usd=1e9, coupon_type="floating", coupon_spread_bp=250), None)
    assert no_sofr[1].endswith("no SOFR fixing stored by the as-of day")


def test_assemble_adds_rent_only_while_rent_start_is_ahead():
    line, _ = lc.interest_line(INST, facts(amount_usd=1_200_000_000, coupon_rate_pct=6.0), SOFR)
    debt = {"lines": [line], "excluded": [], "bearers": [], "sofr": SOFR, "instruments": 1}
    out = lc.assemble(debt, rent_monthly=10_000_000, rent_done=False)
    assert out["interest"] == pytest.approx(6_000_000)
    assert out["rent"] == 10_000_000 and out["total"] == pytest.approx(16_000_000) and out["why"] is None
    done = lc.assemble(debt, rent_monthly=10_000_000, rent_done=True)
    assert done["total"] is None and done["why"].startswith("Rent has started")


def test_no_figure_says_why():
    debt = {"lines": [], "excluded": ["Phase 1 construction loan: rate not public"], "bearers": [], "sofr": SOFR,
            "instruments": 1}
    out = lc.assemble(debt, rent_monthly=None, rent_done=False)
    assert out["total"] is None
    assert out["why"] == ("No instrument funding the site states both an amount and a rate, and no rent is disclosed "
                          "(Phase 1 construction loan: rate not public)")


def test_who_bears_it_comes_only_from_stated_structure_terms():
    rows = [row("structure_term", {"key": "Completion support", "short": "Cipher uncapped completion guarantee"}),
            row("structure_term", {"key": "Reserves", "short": "$210.0m DSRA"}),
            row("structure_term", {"key": "Termination rights", "text": "Late delivery costs $20,000 a day"}),
            row("structure_term", {"key": "Collateral", "text": "First-priority liens"})]
    keys = [b["key"] for b in lc.bearer_lines({"structure_term": rows})]
    assert keys == ["Completion support", "Reserves", "Termination rights"]
    assert lc.bearer_lines({}) == []
