"""Stream events that carry values (types.ts StreamEvent): a changed site page also sends the site's
`cushion`, a changed instrument page sends a `mark` per holder whose filed mark changed. The web app
applies these to the screen without a request, so they must carry exactly the promised fields."""

from __future__ import annotations

from typing import Any

import pytest

from marden.readmodels import builder, mark_dirty, rebuild_all, rebuild_dirty

SLUG = "stream-test-site"
INST = "STREAM-TEST-LOAN"


def _events(committed, kind: str) -> list[dict[str, Any]]:
    rows = committed.execute("SELECT payload FROM stream_event ORDER BY id").fetchall()
    return [r["payload"] for r in rows if r["payload"]["type"] == kind]


def _cushion(value: float | None, as_of: str = "2026-10-20") -> dict[str, Any]:
    return {"value": value, "tag": "M", "asOf": as_of, "sourceName": "Marden cushion model",
            "methodVersion": "cushion v0.1"}


CHAIN = [{"role": "tenant", "party": "Oracle", "status": "amber", "reason": "Rating watch"}]


@pytest.fixture
def site_detail(committed, take_view):
    """A site_detail builder whose doc the test sets; the real status event is kept."""
    take_view("site_detail")
    state: dict[str, Any] = {"cushion": _cushion(4)}

    def status_event(key, doc):
        return {"type": "status", "siteSlug": key, "chain": doc["chain"]}

    @builder("site_detail", keys=(SLUG,), event=status_event)
    def build(conn, key):
        return {"slug": key, "chain": CHAIN, "cushionMonths": state["cushion"]}

    committed.execute("INSERT INTO site (slug, name) VALUES (%s, 'Stream test site')", (SLUG,))
    return state


def test_a_changed_site_page_sends_status_and_cushion(committed, site_detail):
    stats = rebuild_all("site_detail")
    assert not stats.errors and stats.events == 2
    assert _events(committed, "status") == [{"type": "status", "siteSlug": SLUG, "chain": CHAIN}]
    assert _events(committed, "cushion") == [{"type": "cushion", "siteSlug": SLUG, "cushionMonths": _cushion(4)}]

    rebuild_all("site_detail")  # unchanged doc: nothing sent
    assert len(_events(committed, "cushion")) == 1

    site_detail["cushion"] = _cushion(2, "2026-10-21")
    mark_dirty("site_detail", SLUG)
    rebuild_dirty()
    assert _events(committed, "cushion")[-1]["cushionMonths"] == _cushion(2, "2026-10-21")


@pytest.mark.parametrize("cushion", [None, {"value": None, "tag": "M", "asOf": "2026-10-20"}])
def test_no_cushion_event_without_a_valid_cushion_value(committed, site_detail, cushion):
    site_detail["cushion"] = cushion  # no lease date, or a model value without its method version
    stats = rebuild_all("site_detail")
    assert not stats.errors and stats.events == 1
    assert _events(committed, "cushion") == [] and len(_events(committed, "status")) == 1


def _holder(name: str, mark: float) -> dict[str, Any]:
    return {"name": name, "filing": "N-PORT", "filingUrl": f"https://www.sec.gov/{name}.xml",
            "filingDate": "2026-09-30", "parMm": 25, "lastMark": mark, "gapPts": 0, "navHitMm": 0, "shareOfNav": 0.01}


def test_a_changed_holder_mark_sends_one_mark_event(committed, take_view):
    take_view("instrument_detail")
    holders = {"A Fund": 97.5, "B Fund": 99.0}

    @builder("instrument_detail", keys=(INST,))
    def build(conn, key):
        rows = [_holder(name, mark) for name, mark in holders.items()]
        return {"id": key, "holders": {"count": len(rows), "parMm": 50, "rows": rows}}

    committed.execute(
        "INSERT INTO instrument (id, name, kind, issuer_name) VALUES (%s, 'Stream test loan', 'term_loan', 'Issuer')",
        (INST,),
    )
    rebuild_all("instrument_detail")
    assert _events(committed, "mark") == []  # first build: no screen holds it yet

    holders["B Fund"] = 96.25
    holders["C Fund"] = 98.0  # a holder row that appears is not a changed mark: no screen holds it
    mark_dirty("instrument_detail", INST)
    stats = rebuild_dirty()
    assert not stats.errors
    assert _events(committed, "mark") == [{
        "type": "mark", "instrumentId": INST, "holder": "B Fund",
        "mark": {"value": 96.25, "tag": "F", "asOf": "2026-09-30", "sourceName": "N-PORT",
                 "sourceUrl": "https://www.sec.gov/B Fund.xml"},
    }]
