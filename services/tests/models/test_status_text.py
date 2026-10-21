"""The status model's text and date rules, on statements read from the archive (MARDEN_SEED_DIR), plus a few
tiny cases for the date arithmetic itself."""

from __future__ import annotations

import os

import pytest

from marden.core import config
from marden.models import status_dates as dates
from marden.models import status_text as tx
from marden.seed import common

needs_seed = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")


@pytest.fixture(scope="module")
def seed():
    config.reload_settings()
    common.seed_dir.cache_clear()
    return common.load_json


def _project(seed, name: str, needle: str = "") -> dict:
    return next(p for p in seed("aiwc/aiw_proj.json") if p["project"] == name
                and needle in (p["rfs_latest"] or {}).get("text", "") + (p["rfs_prior"] or {}).get("text", ""))


# ---------------------------------------------------------------- dates: the arithmetic (tiny cases)
def test_periods_count_as_their_last_month():
    assert [dates.first_period(t).month for t in ("Q4 2026", "H1 CY2027", "mid-2026", "early 2027", "2028")] == [
        "2026-12", "2027-06", "2026-06", "2027-03", "2028-12"]
    assert dates.slip_months(dates.first_period("Q4 2026"), dates.first_period("2028")) == 24
    # coarser and still containing the earlier date: precision lost, not time
    assert dates.slip_months(dates.first_period("middle of calendar year 2027"),
                             dates.first_period("calendar year 2027")) == 0
    assert dates.slip_months(dates.first_period("April 2027"), dates.first_period("1H 2027")) == 0


# ---------------------------------------------------------------- dates: archive statements
@needs_seed
def test_statement_dates_and_outside_estimates_are_not_targets(seed):
    jupiter = seed("aiwc/cases/JUPITER.json")["capacity"]
    # 'Initial operations Q4 2026 (County timeline); Oracle Jun-2026: customer delivery begins H1 CY2027'
    assert dates.first_period(jupiter["original_completion"]).month == "2026-12"
    assert dates.first_period(jupiter["scheduled_completion"]).month == "2028-12"
    abilene = _project(seed, "Stargate Abilene (flagship) - Phases 1-2")
    before, after = (dates.first_period(abilene[k]["text"]) for k in ("rfs_prior", "rfs_latest"))
    assert (before.month, after.month, dates.slip_months(before, after)) == ("2026-06", "2026-12", 6)
    assert "Epoch" in abilene["rfs_latest"]["text"] and "Epoch" not in dates.clean(abilene["rfs_latest"]["text"])
    barn = _project(seed, 'Saline Township campus ("The Barn")')
    assert dates.first_period(barn["rfs_prior"]["text"]) is None  # 'Apr-24-2026 Related Digital: ...' is a label
    hyperion = _project(seed, "Hyperion")
    assert dates.first_period(hyperion["rfs_latest"]["text"]) is None  # 'No change reported; ... build-out 2036'


@needs_seed
def test_stated_slips(seed):
    changes = _project(seed, "Project Jupiter (Stargate New Mexico)", "force majeure")["changes"]
    pipeline = next(c["what"] for c in changes if c["what"].startswith("Fuel supply slip"))
    assert dates.stated_slip(pipeline) == 6  # 'gas pipeline delayed ~6 months from Sep-2026 to Feb-1-2027'


# ---------------------------------------------------------------- ratings
@needs_seed
def test_ratings_read_the_party_not_its_backstop(seed):
    rating = lambda case: seed(f"aiwc/cases/{case}.json")["parties"]["tenant_rating"]  # noqa: E731
    oracle = tx.parse_rating(rating("JUPITER"))
    assert (oracle.grade, oracle.at_or_below_bbb_minus) == ("BBB-", True)
    meta = tx.parse_rating(rating("BEIGNET"))  # 'Meta AA- (S&P) / Aa3 (Moody's); bond A+ (S&P only)'
    assert (meta.grade, meta.at_or_below_bbb_minus) == ("AA-", False)
    fluidstack = tx.parse_rating(rating("HUT_RB"))  # 'Fluidstack unrated; guarantor Google AA+/Aa2'
    assert fluidstack.grade is None and not fluidstack.at_or_below_bbb_minus
    cut = next(e["w"] for e in seed("aiwc/cases/JUPITER.json")["risks"]["tenant"]["evidence"] if "BBB-" in e["w"])
    assert tx.parse_rating(cut, ["Oracle"]).grade == "BBB-"  # 'S&P cut Oracle to BBB- (one notch above junk)'
    assert tx.parse_rating(cut, ["Microsoft"]) is None


# ---------------------------------------------------------------- event and statement rules
@needs_seed
def test_rules_on_archive_statements(seed):
    changes = _project(seed, "Project Jupiter (Stargate New Mexico)", "force majeure")["changes"]
    notice = next(c["what"] for c in changes if "force majeure notice" in c["what"])
    assert tx.force_majeure(notice) is not None
    lease = seed("aiwc/cases/JUPITER.json")["lease"]
    lease_text = " ".join(str(v) for row in (lease if isinstance(lease, list) else [lease]) for v in row.values())
    assert tx.contract_cut(lease_text) is None  # 'Oracle cannot terminate the lease under any circumstances'
    pipeline = next(c["what"] for c in changes if "rejected" in c["what"] or "denial" in c["what"])
    assert tx.power_denied(pipeline) is not None
    debt = seed("aiwc/cases/JUPITER.json")["debt"]
    notes = " ".join(str(row.get("note") or "") for row in debt)
    assert tx.syndication_stalled(notes) is not None
    titles = [e["t"] for e in seed("aiwc/state/events.json")]
    assert not any(tx.default_notice(t) for t in titles if "default insurance" in t)  # CDS news, not a notice
    # Procedural rulings in the dockets are not power denials (a denied motion, a confidentiality ruling).
    procedural = [i["t"] for key in ("NMED_AQB_26_57", "WIPSC_137-CE-229", "WIPSC_6630-TE-113")
                  for i in seed(f"dockets/out/{key}.json")["items"]
                  if "Order Denying Motion" in i["t"] or "DENY CONFIDENTIAL" in i["t"]]
    assert procedural and not any(tx.power_denied(t) for t in procedural)
