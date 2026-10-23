"""Cushion model: date reading on real archive text, the slip and simulation formulas on tiny cases, and full
runs on the real rows of six V1 sites copied from the local `marden` database (tests/models/cushion_rows.py).

Expectations for the full runs come from the stored facts themselves: Childress's filed outside date
(2027-04), Jupiter's reported lease trigger ('... if 2028 date missed': a year only, tested from Jan 1 to
Dec 31, 2028, with Jul 1, 2028 central), Frontier's missing outside date; nothing is made up.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from cushion_rows import copy_sites

from marden import cli
from marden.core.facts import FactWriter
from marden.core.queue import app
from marden.models import cushion, triggers
from marden.models import cushion_dates as cd
from marden.models.cushion_history import SlipHistory, SlipRecord
from marden.models.cushion_milestones import KEYS, Milestone
from marden.models.cushion_sim import simulate, smoothed_bootstrap
from marden.readmodels.context import business_today

SITES = ["abilene", "childress", "frontier", "jupiter", "lighthouse", "the-barn"]
STATUSES = {"Done", "Denied", "Withdrawn", "Slipped", "Not started", "Early works", "Projected", "At risk"}
DRIVERS = {"Power", "Build", "Delivery", "Permits", "Funding"}
PROJECTION_KEYS = {"p10", "p50", "p90", "outsideDate", "outsideDateAssumed", "outsideDateRange", "cushionMonths",
                   "cushionMonthsRange", "deltaWeek", "oddsOfMissing", "oddsOfMissingRange", "costPerMonthLate",
                   "lateCost"}
YM = re.compile(r"^\d{4}-\d{2}$")


def _label(text: str, *, which: str = "leading") -> str | None:
    exprs = cd.targets(text)
    w = cd.leading(exprs) if which == "leading" else cd.earliest(exprs)
    return w.label() if w else None


# ---------------------------------------------------------------- dates (verbatim stored values)
def test_dates_read_as_the_sources_state_them():
    assert _label("2028 (commercial operation planned; County: Phase 1 completion Q3 2028)") == "2028-07..2028-09"
    assert _label("H1 CY2027 (Oracle guidance)") == "2027-01..2027-06"
    assert _label("2026-H2 (target)") == "2026-07..2026-12"
    assert _label("CB-3 2026-07; CB-4 2H26; CB-5 early 2027 (hall by hall)") == "2026-07"
    assert _label("phased H2 2025 - early 2027") == "2025-07..2027-04"
    assert _label("2027-05-01 (60 MW data hall rent target); network hall 10 MW 2027-04-01", which="e") == "2027-04"
    text = "Q3 2027 initial data hall (energization Q1 2027); six halls total"
    kinds = {w.text: cd.clause_kind(text, w) for w in cd.targets(text)}
    assert kinds == {"Q3 2027": "hall", "Q1 2027": "grid"}


def test_statement_and_debt_dates_are_not_targets():
    roles = {w.text: w.role for w in cd.find_dates(
        "First part live early 2027 (Related, Jun-2026); Oracle: customer delivery begins H2 CY2027")}
    assert roles == {"early 2027": "target", "Jun-2026": "said", "H2 CY2027": "target"}
    assert [w.role for w in cd.find_dates("Jun-11-2026 call (DCD): customer delivery begins in H1 calendar 2027")] == [
        "said", "target"]
    assert [w.role for w in cd.find_dates("draws through 2027-06-30; stabilization site-by-site")] == ["other"]
    assert [w.role for w in cd.find_dates("2027 (calendar year, per 10-Q 2026-10-07)")] == ["target", "said"]


# ---------------------------------------------------------------- formulas (tiny cases)
def test_slip_ratio_is_the_move_over_the_time_left_with_a_floor():
    r = SlipRecord("t", "a", "build", date(2026, 1, 1), cd.month_index(2027, 1), date(2026, 6, 1),
                   cd.month_index(2027, 7), False)
    assert r.ratio == pytest.approx(6 / 12)
    late = SlipRecord("t", "a", "build", date(2026, 12, 1), cd.month_index(2027, 1), date(2027, 1, 1),
                      cd.month_index(2027, 4), False)
    assert late.ratio == pytest.approx(3 / 3)  # one month ahead, but the horizon floor is 3 months


def test_bootstrap_is_seeded_and_bounded():
    a = smoothed_bootstrap([0.0, 0.0, 0.3, 1.5], 1000, np.random.default_rng(7))
    b = smoothed_bootstrap([0.0, 0.0, 0.3, 1.5], 1000, np.random.default_rng(7))
    assert np.array_equal(a, b) and a.min() >= -0.9


def test_a_milestone_finishes_no_earlier_than_its_predecessors():
    as_of = date(2026, 10, 1)
    w = lambda y, m: cd.When(cd.month_index(y, m), cd.month_index(y, m), "month", 0, 0, "")  # noqa: E731
    stones = [Milestone(k, applicable=k in ("gas_or_grid", "first_hall", "rent_start")) for k in KEYS]
    by = {m.key: m for m in stones}
    by["gas_or_grid"].planned, by["gas_or_grid"].status = w(2027, 6), "Projected"
    by["first_hall"].planned, by["first_hall"].status, by["first_hall"].preds = w(2027, 1), "Projected", ["gas_or_grid"]
    by["rent_start"].preds = ["first_hall"]
    history = SlipHistory([SlipRecord("t", f"s{i}", "build", as_of, cd.month_index(2027, 1), as_of,
                                      cd.month_index(2027, 1), False) for i in range(5)])  # every plan held
    sim = simulate(stones, as_of, history, "x", seed=1, n=2000)
    assert sim is not None and sim.driver == "Power"
    assert (sim.rent >= sim.finish["gas_or_grid"]).all() and sim.rent.min() >= cd.month_index(2027, 6)


def test_a_year_only_outside_date_is_a_range_with_its_midpoint_central():
    from marden.models.cushion_inputs import Outside, _outside_from
    from marden.models.cushion_output import lease_view, projection_doc
    from marden.models.cushion_sim import SimResult

    row = {"tag": "R", "source_name": "Cp24", "valid_from": date(2026, 9, 24), "source_url": None, "id": 1}
    o = _outside_from("force majeure invoked to defer payment step-up if 2028 date missed", row, "x",
                      trigger_only=True)
    assert isinstance(o, Outside) and o.value == "2028" and o.year_only and o.assumed == "2028-07-01"
    assert (o.month, o.window) == (cd.month_index(2028, 6), (cd.month_index(2027, 12), cd.month_index(2028, 12)))
    month = _outside_from("outside date 2028-11", row, "x", trigger_only=False)
    assert month is not None and not month.year_only and month.assumed is None  # a month: the date as stated

    # 1,000 simulated rent starts spread evenly over 2028: about half miss Jul 1, all miss Jan 1, none Dec 31.
    rent = np.linspace(cd.month_index(2028, 1), cd.month_index(2028, 12) + 0.999, 1000)

    site = SimpleNamespace(as_of=date(2026, 10, 22), outside=o, rent=None)
    sim = SimResult(rent, {}, [], {}, "Build", {}, {})
    doc = projection_doc(site, sim, None)  # type: ignore[arg-type]
    assert doc["oddsOfMissing"]["value"] == pytest.approx(0.5, abs=0.01)
    assert (doc["oddsOfMissingRange"]["best"], doc["oddsOfMissingRange"]["worst"]) == (0.0, 1.0)
    outcomes, position = lease_view(site, sim)  # type: ignore[arg-type]
    assert "we test Jan\u2013Dec 2028" in position["value"] and "0\u2013100% start rent after it" in outcomes[1]["text"]


# ---------------------------------------------------------------- full runs on real rows
@pytest.fixture
def real(committed):
    return copy_sites(SITES)


def _check_types(output: dict) -> None:
    p = output["projection"]
    assert set(p) <= PROJECTION_KEYS and {"p10", "p50", "p90", "outsideDate", "deltaWeek"} <= set(p)
    if p["p50"] is not None:
        assert all(YM.match(p[k]) for k in ("p10", "p50", "p90")) and p["p10"] <= p["p50"] <= p["p90"]
    for key in ("deltaWeek", "cushionMonths", "oddsOfMissing", "costPerMonthLate"):
        if p.get(key) is not None:
            assert p[key]["tag"] == "M" and p[key]["methodVersion"] in ("cushion v0.1", "odds v0", "late cost v0.1")
    assert [m["key"] for m in output["milestones"]] == [k for k in KEYS if k not in output["notApplicable"]]
    for m in output["milestones"]:
        assert m["status"] in STATUSES and all(e["kind"] in {"done", "denied", "slipped"} for e in m["events"])
        if "latestEvidence" in m:
            assert set(m["latestEvidence"]) == {"date", "text", "tag", "sourceName", "sourceUrl"}
    assert output["driver"] in DRIVERS | {None}
    assert isinstance(output["missing"], list)


def test_today_run_matches_the_types_and_the_stored_lease_dates(real, committed):
    result = cushion.run(trigger="test")
    sites = result["sites"]
    assert sorted(sites) == SITES
    for output in sites.values():
        _check_types(output)
    childress = sites["childress"]["projection"]
    # The archive's '2027-04 (approx.)' is not stated by the 8-K it cited: since the 2026-10-22 tag audit it is
    # an R "unverified archive figure", not a filed value.
    assert childress["outsideDate"]["value"] == "2027-04" and childress["outsideDate"]["tag"] == "R"
    y, m = (int(x) for x in childress["p50"].split("-"))
    assert childress["cushionMonths"]["value"] == cd.month_index(2027, 4) - cd.month_index(y, m)
    # Cost of each month late (late cost v0.1): the rent part is the contract's average monthly rent; the interest
    # part is each instrument's amount x all-in rate / 12 (floating: SOFR + the stated spread); the total is their sum.
    parts = {p["kind"]: p for p in childress["lateCost"]["parts"]}
    assert parts["rent"]["monthlyUsd"] == round(9_700_000_000 / (5 * 12))
    for line in parts.get("interest", {}).get("lines", []):  # instruments copied into the test db, if any
        assert line["monthlyUsd"] == round(line["amountUsd"] * line["ratePct"] / 100 / 12)
    assert childress["costPerMonthLate"]["value"] == sum(p["monthlyUsd"] for p in parts.values())
    assert childress["costPerMonthLate"]["tag"] == "M"
    jupiter = sites["jupiter"]
    jp = jupiter["projection"]
    assert jp["outsideDate"]["value"] == "2028"
    # A year only: tested across 2028 (worst case Jan 1, best case Dec 31), the midpoint Jul 1 central.
    assert jp["outsideDateAssumed"] == "2028-07-01"
    assert jp["outsideDateRange"] == {"from": "2028-01-01", "to": "2028-12-31", "central": "2028-07-01"}
    odds, span = jp["oddsOfMissing"]["value"], jp["oddsOfMissingRange"]
    assert 0 <= span["best"] <= odds <= span["worst"] <= 1
    assert (span["bestDate"], span["worstDate"]) == ("2028-12-31", "2028-01-01")
    assert jp["oddsOfMissing"]["methodVersion"] == "odds v0"
    y, m = (int(x) for x in jp["p50"].split("-"))
    p50 = cd.month_index(y, m)
    assert jp["cushionMonths"]["value"] == cd.month_index(2028, 6) - p50  # the deadline is the end of June
    assert jp["cushionMonthsRange"] == {"worst": cd.month_index(2027, 12) - p50, "best": cd.month_index(2028, 12) - p50}
    assert "2028 only" in jupiter["leasePosition"]["value"] and "Jul 1, 2028" in jupiter["leasePosition"]["value"]
    assert "yearOnly" in jupiter["basis"]["outsideDate"]
    assert "outsideDateRange" not in childress and "oddsOfMissingRange" not in childress  # a month is stated
    gas = next(ms for ms in jupiter["milestones"] if ms["key"] == "gas_or_grid")
    assert gas["status"] == "Slipped" and gas["projected"]["p50"] >= "2027-02"
    frontier = sites["frontier"]
    assert frontier["projection"]["cushionMonths"] is None and "oddsOfMissing" not in frontier["projection"]
    assert any(x.startswith("outside_date:") for x in frontier["missing"])
    run = committed.execute("SELECT * FROM model_run WHERE id = %s", (result["run_id"],)).fetchone()
    assert run["ok"] and run["model"] == "cushion" and run["inputs_hash"]
    dirty = {(r["view"], r["key"]) for r in committed.execute("SELECT view, key FROM read_model_dirty").fetchall()}
    assert {("site_schedule", "jupiter"), ("site_detail", "jupiter"), ("completion", "all")} <= dirty


def test_reruns_are_deterministic_and_keep_one_output_per_day(real, committed):
    first = cushion.run(trigger="test")["sites"]["jupiter"]
    second = cushion.run(trigger="test")["sites"]["jupiter"]
    assert first == second
    n = committed.execute("SELECT count(*) AS n FROM model_output WHERE model = 'cushion' AND subject_id = 'jupiter'")
    assert n.fetchone()["n"] == 1


def test_backtests_use_only_what_was_public(real, committed):
    day = date(2026, 9, 1)
    jupiter = cushion.run(day, ["jupiter"], trigger="test")["sites"]["jupiter"]
    assert jupiter["projection"]["outsideDate"]["value"] is None  # the 2028 trigger was reported on Sep 24
    for ms in jupiter["milestones"]:
        if "latestEvidence" in ms:
            assert ms["latestEvidence"]["date"] <= day.isoformat()
        assert all(e["date"] <= day.isoformat() for e in ms["events"])


def test_delta_week_compares_with_the_run_seven_days_earlier(real, committed):
    today = business_today()
    before = cushion.run(today - timedelta(days=7), ["childress"], trigger="test")["sites"]["childress"]
    now = cushion.run(today, ["childress"], trigger="test")["sites"]["childress"]
    c0, c1 = (o["projection"]["cushionMonths"] for o in (before, now))
    expected = c1["value"] - c0["value"] if c0 and c1 and c0["value"] is not None and c1["value"] is not None else None
    assert now["projection"]["deltaWeek"]["value"] == expected
    history = cushion.out.cushion_history(committed, "childress", today)
    assert all(set(p) <= {"date", "months", "event"} for p in history)


def test_published_inputs_mark_the_model_and_run_dirty_clears_them(real, committed):
    with committed.transaction():
        FactWriter(committed).add("site", "jupiter", "rfs_statement",
                                  {"date": "2026-10-21", "text": "Commercial operation planned 2028", "which": "latest",
                                   "project": "test"}, tag="R", source_name="test", valid_from=date(2026, 10, 21),
                                  producer="test:cushion", status="published")
    marks = {(r["model"], r["key"]) for r in committed.execute("SELECT model, key FROM model_dirty").fetchall()}
    assert {("cushion", "jupiter"), ("cushion", "*")} <= marks
    summary = triggers.run_dirty(["cushion"])
    assert summary["cushion"]["sites"] == len(SITES)
    assert committed.execute("SELECT count(*) AS n FROM model_dirty WHERE model = 'cushion'").fetchone()["n"] == 0


def test_eia_history_is_reported_missing_until_two_reports_exist(real, committed):
    from marden.models.cushion_history import eia_records

    assert eia_records(committed, business_today()) == []
    output = cushion.run(trigger="test")["sites"]["jupiter"]
    assert any(x.startswith("slip_history: no EIA-860M planned-vs-actual pairs (1 monthly report(s)")
               for x in output["missing"])
    position = output["leasePosition"]
    assert position["tag"] == "M" and re.search(r"outside date|the lease gives \d{4} only", position["value"])
    assert [o["label"] for o in output["leaseOutcomes"]] == ["Finished before the outside date", "Finished after it"]


def test_eia_planned_vs_a_later_report_is_a_power_slip_record(committed):
    """Two EIA-860M reports of one generator (tiny case): a record once the later report is public."""
    from marden.models.cushion_history import eia_records, month_float

    with committed.transaction():
        writer = FactWriter(committed)
        for day, month in ((date(2026, 3, 31), "2027-01"), (date(2026, 6, 30), "2027-07")):
            writer.add("eia_generator", "99999_T1", "planned_online", month, tag="F", source_name="EIA-860M",
                       valid_from=day, producer="test:cushion", status="published")
    assert eia_records(committed, date(2026, 7, 23)) == []  # the June report is public on Jul 24
    (record,) = eia_records(committed, date(2026, 7, 24))
    assert record.category == "power" and record.said_on == date(2026, 4, 24) and not record.done
    planned = cd.month_index(2027, 1) + 0.5  # a month's midpoint
    assert record.ratio == pytest.approx(6 / (planned - month_float(date(2026, 4, 24))))


def test_command_and_tasks_are_registered():
    parser = cli.build_parser()
    args = parser.parse_args(["model", "cushion", "--as-of", "2026-10-14", "--site", "jupiter"])
    assert args.as_of == date(2026, 10, 14) and args.sites == ["jupiter"]
    assert {"models.cushion_daily", "models.cushion", "models.run_dirty"} <= set(app.tasks)
    assert "cushion" in triggers.registered_models()
