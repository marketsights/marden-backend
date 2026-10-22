"""Event impact model (docs/methods/event-impact-v0.1.md): which days change the price engine's signal counts (a
restatement changes nothing), the kinds a day changes, the trades around an event, and the read model's words:
which priced change a site record stands for, its "Effect on debt" line, and the reason when there is none."""

from __future__ import annotations

from datetime import date

from marden.models.event_impact_changes import CompletionChange, day_change, without
from marden.models.event_impact_market import TradeDay, around, market_move
from marden.models.price_signals import Signal
from marden.readmodels.views.debt_effects_text import (
    NOT_READ,
    effect_of,
    effect_phrase,
    match,
    other_reason,
    pct_change,
    reading_kinds,
    signed,
)

SEP24, SEP26 = date(2026, 9, 24), date(2026, 9, 26)


def _sig(kind: str, day: date, count: int = 1, url: str = "u1", event: bool = False) -> Signal:
    return Signal(kind, day, count, event, f"{kind} text", "Bloombergtax", url)


def test_day_change_counts_only_new_information():
    sigs = [_sig("permit_denial", SEP24, 2), _sig("power_delay", SEP24), _sig("force_majeure", SEP24, url="u2")]
    ch = day_change(sigs, SEP24)
    assert ch is not None and ch.before == (0, 0) and ch.after == (2, 2)
    assert ch.kinds == ["permit_denial", "power_delay", "force_majeure"]
    restated = [*sigs, _sig("permit_denial", date(2026, 10, 17), 2, url="u3")]
    assert day_change(restated, date(2026, 10, 17)) is None  # the archive restating the two denials
    assert [s.kind for s in without(sigs, SEP24, ["force_majeure"])] == ["permit_denial", "power_delay"]


def test_market_move_leaves_out_the_event_day():
    days = [TradeDay(date(2026, 9, d), px, 1, "F", "FINRA TRACE (finra.org)", None)
            for d, px in ((23, 87.411), (24, 86.536), (25, 86.588))]
    before, after = around(days, SEP24)
    assert (before.day.day, after.day.day) == (23, 25)
    m = market_move("EX-ORCL-239", "Oracle 5.20% 2035", days, SEP24)
    assert m["changePts"] == round(86.588 - 87.411, 3) and m["why"] is None
    assert market_move("x", "x", [], SEP24)["why"] == "No trades on record: it does not trade on TRACE."
    assert market_move("x", "x", days[:1], SEP24)["why"] == "No trade in the 14 days after Sep 24."


def _effect(iid: str, pts: float, without_px: float, with_px: float, y: float | None = 0.64) -> dict:
    return {"instrumentId": iid, "with": with_px, "without": without_px, "changePts": pts, "measure": "YTW",
            "yieldChangePct": y, "stored": with_px}


SIGNALS = {
    "key": "signals:2026-09-24", "kind": "signals", "date": "2026-09-24", "kinds": ["permit_denial", "force_majeure"],
    "label": "Power and permit signals counted by the price model: permit denials 0 → 2",
    "records": [{"date": "2026-09-24", "text": "Oracle sent force majeure notice", "kind": "force_majeure",
                 "sourceName": "Cp24", "sourceUrl": "https://cp24.example/fm", "tag": "R"}],
    "effects": [_effect("J", -2.403, 94.762, 92.359)],
    "byKind": {"force_majeure": [_effect("J", -0.834, 93.193, 92.359)]},
}
COMPLETION = {"key": "completion:2026-10-21", "kind": "completion", "date": "2026-10-21", "records": [],
              "label": "Projected completion moved from Aug 2028 to Jan 2029 (Marden cushion model)",
              "effects": [_effect("J", -1.438, 87.302, 85.864)]}


def test_a_record_stands_for_the_change_it_is_one_of():
    row = {"date": "2026-09-24", "text": "anything", "sourceUrl": "https://cp24.example/fm"}
    hits, why = match(row, [SIGNALS, COMPLETION])
    assert why is None and [(h[0]["key"], h[1]) for h in hits] == [("signals:2026-09-24", "force_majeure")]
    assert effect_of(*hits[0], "J")["changePts"] == -0.834


def test_a_report_two_days_later_reads_as_the_same_signal():
    row = {"date": "2026-09-26", "text": "Oracle files force majeure on 2.45GW New Mexico Stargate data center"}
    assert "force_majeure" in reading_kinds(row["text"])
    hits, _ = match(row, [SIGNALS])
    assert [(h[0]["key"], h[1]) for h in hits] == [("signals:2026-09-24", "force_majeure")]


def test_a_cushion_row_is_the_completion_change_of_its_day():
    row = {"date": "2026-10-21", "text": "First hall energized: 2028", "changed": "Cushion +4 → \u22121 months"}
    hits, _ = match(row, [SIGNALS, COMPLETION])
    assert hits[0][0]["key"] == "completion:2026-10-21"
    assert effect_phrase("Project Jupiter Construction loans", effect_of(hits[0][0], None, "J")) == \
        "Project Jupiter Construction loans \u22121.4 pts (87.3 → 85.9), YTW +0.6%"
    _, why = match({**row, "date": "2026-10-14"}, [COMPLETION])
    assert why == "The projected completion the price model reads did not change on this day."


def test_reasons_when_a_record_changed_nothing():
    later = {"date": "2026-10-20", "text": "Oracle force majeure notice to Blue Owl reported again"}
    assert match(later, [SIGNALS])[1].startswith("Already priced in: the price model counted this force-majeure notice "
                                                 "on Sep 24 (Cp24)")
    early = {"date": "2026-08-01", "text": "Oracle invokes force majeure"}
    assert match(early, [SIGNALS])[1].startswith("Not priced on this day: the price model first counted a "
                                                 "force-majeure notice on Sep 24")
    assert other_reason({"link": "End user", "ruleHit": "E1", "text": "OpenAI cash burn"}).startswith("Status only")
    assert other_reason({"link": "Lenders", "ruleHit": "L2", "text": "Marden price 87.7"}).startswith(
        "This row is the Marden price itself")
    assert other_reason({"text": "Notice of appearance of counsel"}).startswith("Procedural filing")
    assert other_reason({"text": "County approves road paving"}) == NOT_READ


def test_number_words():
    assert signed(-1.438) == "\u22121.4" and signed(0.31) == "+0.3" and signed(-0.004) == "0.0"
    assert pct_change(0.635) == "+0.6%" and pct_change(-0.012) == "\u22120.01%"


def test_change_labels_are_plain_words():
    sigs = [_sig("permit_denial", SEP24, 2), _sig("power_delay", SEP24), _sig("force_majeure", SEP24, url="u2")]
    assert day_change(sigs, SEP24).label() == ("Reported: 2 permit denials, a power delay of 3+ months and a "
                                               "force-majeure notice citing power")
    first = CompletionChange(date(2026, 8, 17), None, date(2027, 4, 30))
    moved = CompletionChange(date(2026, 10, 21), date(2028, 8, 31), date(2029, 1, 31))
    assert first.label() == "First projected completion: Apr 2027"
    assert moved.label() == "Projected completion moved from Aug 2028 to Jan 2029"
