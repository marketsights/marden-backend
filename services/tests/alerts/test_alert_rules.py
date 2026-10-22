"""The engine's own rules: Marden price moves at each threshold (on the real price outputs of a watched
instrument), cushion bands, fund marks filed after the newest seen, rating actions, and the worker tasks."""

from __future__ import annotations

from datetime import date

import pytest
from alert_rows import copy_sites, rows

from marden.alerts import rules, tasks
from marden.alerts import text as t
from marden.alerts.triggers import extract, model_triggers
from marden.core import db
from marden.core.queue import app

D0, D1 = date(2026, 9, 30), date(2026, 10, 21)


def test_price_moves_on_real_outputs_follow_each_threshold():
    copied = copy_sites(["jupiter"])
    priced = rows("""SELECT DISTINCT subject_id FROM model_output WHERE model = 'price' AND subject_id = ANY(%s)
                     AND output ? 'price' AND jsonb_typeof(output->'price') = 'number'""", (copied["instruments"],))
    if not priced:
        pytest.skip("no Marden price outputs for the copied instruments")
    inst = priced[0]["subject_id"]
    outs = rows("""SELECT DISTINCT ON (as_of) * FROM model_output WHERE model = 'price' AND subject_id = %s
                   ORDER BY as_of, created_at DESC, id DESC""", (inst,))
    first = outs[0]["as_of"]
    with db.connection() as conn:
        found = [f for trig in model_triggers(conn, first, date.today(), models=("price",)) for f in trig.findings
                 if trig.subject_id == inst]
    # independently: for each threshold, walk the prices from the first one and count the crossings
    prices = [(o["as_of"], float(extract(o)["price"])) for o in outs if isinstance(extract(o)["price"], int | float)]
    for pts in rules.THRESHOLDS:
        ref, expected = prices[0][1], []
        for day, price in prices[1:]:
            move = round(price - ref, 1)
            if abs(move) >= pts:
                expected.append((day.isoformat(), move))
                ref = price
        got = [(f.facts["asOf"], f.facts["movePts"]) for f in found if f.threshold_pts == pts]
        assert got == expected, pts
    for f in found:
        assert f.codes == ["PX"] and f.alert_types == ["price_or_mark"] and f.tag == "M"
        assert f.headline.endswith(f"pts to {t.num(f.facts['price'])}")
        assert f.severity == ("amber" if f.facts["movePts"] < 0 else "green")


def test_cushion_bands():
    def run(c0, c1, p0="2028-08", p1="2028-08"):
        return rules.cushion_finding("Site", "site", {"cushion_months": c0, "p50": p0},
                                     {"cushion_months": c1, "p50": p1, "methodVersion": "cushion v0.1"}, D1, D0)

    neg = run(4, -1, "2028-08", "2029-01")
    assert neg.codes == ["M2", "M1"] and neg.severity == "red" and neg.alert_types == ["cushion", "milestone"]
    assert neg.headline == "Site cushion below zero: \u22121 months"
    assert "Cushion 4 \u2192 \u22121 months" in neg.body and "Aug '28 \u2192 Jan '29" in neg.body
    under = run(5, 2)
    assert under.codes == ["C3"] and under.severity == "amber"
    assert under.headline == "Site cushion under 3 months: 2 months"
    assert run(2, 1) is None  # already under 3
    back = run(-2, 4)
    assert back.codes == ["CX"] and back.severity == "green"
    slip = run(None, None, "2026-06", "2026-11")
    assert slip.codes == ["M1"] and slip.headline == "Site projected completion moved +5 months to Nov '26"
    assert "Cushion" not in slip.body.splitlines()[0]  # n/d -> n/d is not a line
    assert run(None, None, "2026-06", "2026-07") is None


def test_marks_filed_after_the_newest_seen():
    before = {"marks": [{"name": "Fund A", "filingDate": "2026-07-31", "lastMark": 90.2}]}
    after = {"marks": [*before["marks"],
                       {"name": "Fund B", "filingDate": "2026-08-29", "lastMark": 96.0, "gapPts": -6.1,
                        "filing": "N-PORT", "filingUrl": "https://www.sec.gov/x"},
                       {"name": "Fund C", "filingDate": "2026-06-30", "lastMark": 91.0}],
             "methodVersion": "mark_gap v0.1"}
    f = rules.mark_finding("Bond", "BOND-1", before, after, D1)
    assert f.headline == "Bond: fund mark filed at 96.0 (Fund B)" and f.severity == "amber"
    assert f.facts["newMarks"] == 1
    assert "Fund B: 96.0 (N-PORT for Aug 29, 2026), 6.1 pts above the Marden price" in f.body
    assert f.sources[0]["url"] == "https://www.sec.gov/x" and f.tag == "F"
    assert rules.mark_finding("Bond", "BOND-1", {"marks": []}, after, D1) is None  # first coverage is not news


def test_rating_action_needs_a_different_prior():
    fact = {"id": 2, "field": "rating", "value": "B1 / B", "tag": "F", "valid_from": D1, "source_name": "S&P",
            "source_url": "https://example.com/r", "quote": None}
    prior = {**fact, "id": 1, "value": "Ba3 / BB-", "valid_from": D0, "source_name": "Moody's"}
    f = rules.rating_finding("Note", "/instruments/N", fact, prior)
    assert f.codes == ["RT"] and f.headline == "Note rating: Ba3 / BB- \u2192 B1 / B" and len(f.sources) == 2
    assert rules.rating_finding("Note", "/instruments/N", fact, None) is None
    assert rules.rating_finding("Note", "/instruments/N", fact, {**prior, "value": "B1 / B"}) is None


def test_text_formats():
    assert t.signed(-2.04) == "\u22122.0" and t.signed(1.96) == "+2.0" and t.signed(0.04) == "0.0"
    assert t.whole(-0.6) == "\u22121" and t.whole(None) == "n/d" and t.month_label("2029-01") == "Jan '29"
    assert t.rule_label("M4") == "M4 Tenant notice" and t.alert_type("M5") == "price_or_mark"
    assert t.alert_type("tenant", "tenant") == "tenant" and t.alert_type("L1") == "price_or_mark"


def test_worker_tasks():
    assert app.tasks["alerts.scan"].queue == "alerts" and app.tasks["alerts.digests"].queue == "alerts"
    periodic = {name: task.cron for (name, _), task in app.periodic_registry.periodic_tasks.items()}
    assert periodic["alerts.scan"] == tasks.SCAN_CRON == "* * * * *"
    assert periodic["alerts.digests"] == tasks.DIGEST_CRON


def test_opposition_events_take_the_collectors_grade():
    event = {"id": 7, "kind": "opposition", "severity": "none", "title": "County hearing on the rezoning", "tag": "R",
             "event_date": D1, "source_name": "Google News", "source_url": "https://example.com/a"}
    info = rules.event_finding(event, "Site", "/sites/site?tab=opposition")
    assert info.codes == ["OP"] and info.severity == "green" and info.alert_types == ["opposition"]
    assert info.headline == "Site: County hearing on the rezoning" and info.ref_url == "/sites/site?tab=opposition"
    red = rules.event_finding({**event, "severity": "red"}, "Site", "/sites/site?tab=opposition")
    assert red.severity == "red" and red.sources[0]["eventId"] == 7
    assert rules.event_finding({**event, "kind": "news"}, "Site", "/sites/site") is None  # not material, not opposition


def test_instrument_names_carry_their_site_or_issuer():
    """Alert headlines start with the subject's name; an instrument's own name repeats across issuers."""
    from marden.alerts.fanout import subject_name

    copied = copy_sites(["jupiter"])
    if not copied["instruments"]:
        pytest.skip("jupiter's instruments are not in the source database")
    with db.connection() as conn:
        site = conn.execute("SELECT name FROM site WHERE slug = 'jupiter'").fetchone()["name"]
        for iid in copied["instruments"]:
            own = conn.execute("SELECT name FROM instrument WHERE id = %s", (iid,)).fetchone()["name"]
            named = subject_name(conn, "instrument", iid)
            assert named == (own if site.lower() in own.lower() else f"{site} · {own}")
        assert subject_name(conn, "instrument", "no-such-instrument") == "no-such-instrument"
