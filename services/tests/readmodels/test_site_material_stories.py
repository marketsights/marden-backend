"""Material events, one per development (views/sites_stories.py), and what the debt card's numbers are
(views/sites_debt_basis.py), on real rows copied from the local `marden` database (site_rows).

Expectations are computed from the copied rows independently of the builders: every material record
appears exactly once (as a row or one of a row's sources), a row is dated by its earliest report, the
Jupiter force-majeure coverage of late September and October is one event with every outlet as a source,
and real headlines about different developments stay apart. The debt basis line is recomputed from the
dealer quote and the Marden price; the 55c recovery is labelled an assumption when the recovery model has
no number.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

import pytest
from real_rows import copied_rows
from site_rows import copy_sites

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building, day_end
from marden.readmodels.registry import get_builder
from marden.readmodels.views import sites_debt, sites_detail  # noqa: F401 (registers builders)
from marden.readmodels.views import sites_stories as stories
from marden.readmodels.views.sites_tabs import rule_words

MINUS = "\u2212"


@pytest.fixture
def sites() -> list[str]:
    return copy_sites()


def build(view: str, key: str) -> Any:
    b = get_builder(view)
    assert b is not None, view
    with building(business_today()), db.transaction() as conn:
        return b.build_one(conn, key)


def material_records(slug: str) -> list[dict[str, Any]]:
    """The site's material records as the copied rows hold them: approved material events and events the
    archive judged material (materiality_note), published and known by the build date."""
    with building(business_today()):
        cutoff = day_end()
    notes = {(r["value"]["event_date"], r["value"]["title"]): r["value"].get("rule") for r in copied_rows(
        "SELECT value FROM fact WHERE subject_type = 'site' AND subject_id = %s AND field = 'materiality_note' "
        "AND status = 'published' AND value->>'judgment' = 'material'", (slug,))}
    rows = copied_rows(
        "SELECT * FROM event WHERE subject_type = 'site' AND subject_id = %s AND status = 'published' "
        "AND learned_at < %s AND event_date <= %s ORDER BY event_date, id", (slug, cutoff, business_today()))
    out = []
    for r in rows:
        key = (r["event_date"].isoformat(), r["title"])
        if r["material"] or key in notes:
            out.append({**r, "rule": notes.get(key)})
    return out


def reports_of(row: dict[str, Any]) -> list[dict[str, Any]]:
    return row.get("sources") or [row]


def test_every_material_record_is_shown_once_dated_by_its_earliest_report(sites):
    for slug in sites:
        rows = build("site_detail", slug)["materialEvents"]
        shown = Counter((s["date"], s["text"]) for r in rows for s in reports_of(r))
        expected = Counter((r["event_date"].isoformat(), r["title"]) for r in material_records(slug))
        assert shown == expected, slug
        assert [r["date"] for r in rows] == sorted((r["date"] for r in rows), reverse=True), slug
        for row in rows:
            dates = [s["date"] for s in reports_of(row)]
            assert dates == sorted(dates) and row["date"] == dates[0], (slug, row["text"])
            if "sources" in row:
                assert len(row["sources"]) > 1
                # the summary is the sources' own words: a run taken from one source's headline
                assert any(row["text"] in s["text"] for s in row["sources"]), (slug, row["text"])
                assert all(s["tag"] in ("F", "R", "M") and s["sourceName"] for s in row["sources"])


def test_jupiter_force_majeure_coverage_is_one_event_with_every_source(sites):
    if "jupiter" not in sites:
        pytest.skip("Jupiter is not in the source database")
    records = material_records("jupiter")
    fm = [r for r in records if rule_words(r["rule"]) == "Force majeure" and r["kind"] == "news"]
    if len(fm) < 2:
        pytest.skip("fewer than two force-majeure reports on file")
    # the reports a development holds: each within GAP_DAYS of the previous, all within SPAN_DAYS of the first
    chain = [fm[0]]
    for r in fm[1:]:
        if (r["event_date"] - chain[-1]["event_date"]).days <= stories.GAP_DAYS \
                and (r["event_date"] - chain[0]["event_date"]).days <= stories.SPAN_DAYS:
            chain.append(r)
    rows = build("site_detail", "jupiter")["materialEvents"]
    holding = [r for r in rows if any(s["text"] in {c["title"] for c in chain} for s in reports_of(r))]
    assert len(holding) == 1, [r["text"] for r in holding]
    event = holding[0]
    assert event["date"] == chain[0]["event_date"].isoformat()
    assert sorted(s["text"] for s in event["sources"]) == sorted(c["title"] for c in chain)
    assert "force majeure" in event["text"].lower() and event["severity"] == "red"
    # a report the archive classed as another development (a rating downgrade) stays its own row
    others = [r for r in records if r["rule"] and rule_words(r["rule"]) != "Force majeure" and r["kind"] == "news"]
    for other in others:
        assert other["title"] not in {s["text"] for s in event["sources"]}


def _report(row: dict[str, Any]) -> stories.Report:
    """A report with the archive's materiality rule for it, as the builder reads it."""
    note = copied_rows("SELECT value->>'rule' AS rule FROM fact WHERE subject_type = 'site' AND subject_id = %s "
                       "AND field = 'materiality_note' AND status = 'published' AND value->>'title' = %s "
                       "AND value->>'event_date' = %s LIMIT 1",
                       (row["subject_id"], row["title"], row["event_date"].isoformat()))
    rule = rule_words(row.get("rule_code") or (note[0]["rule"] if note else None))
    return stories.Report(row, rule, stories.terms(row["title"]), stories.identifiers(row["title"]))


def _titled(slug: str, like: str) -> dict[str, Any] | None:
    rows = copied_rows("SELECT * FROM event WHERE subject_type = 'site' AND subject_id = %s AND status = 'published' "
                       "AND title ILIKE %s ORDER BY event_date, id LIMIT 1", (slug, like))
    return rows[0] if rows else None


@pytest.mark.parametrize(("slug", "a", "b", "same"), [
    # the same Oracle force-majeure notice, two outlets' own headlines
    ("jupiter", "Oracle Issues Force Majeure Notice for Project Jupiter%", "Project Jupiter Oracle Force Majeure%",
     True),
    # one headline syndicated by two outlets
    ("jupiter", "%force majeure%warning lands%TheStreet", "%force majeure%warning lands%Yahoo Finance", True),
    # different developments that share words ('data center', 'waiting', 'Wisconsin', a letter's wording)
    ("jupiter", "ChatGPT%Data Center Is Waiting on Gas%", "Oracle Invoked Force Majeure on a Data Center%", False),
    ("lighthouse", "ATC refiles Wisconsin transmission%", "Oracle plans to buy nuclear power%Wisconsin%", False),
    ("lighthouse", "Letter informing%07/24/2026 filing is deficient%", "Letter informing%08/17/2026 filing%", False),
    ("jupiter", "Order Denying Motion for Reconsideration%", "New Mexico Environment Department%Response Pursuant%",
     False),
])
def test_real_headlines_join_only_when_they_report_the_same_development(sites, slug, a, b, same):
    if slug not in sites:
        pytest.skip(f"{slug} is not in the source database")
    left, right = _titled(slug, a), _titled(slug, b)
    if left is None or right is None:
        pytest.skip("the headlines are not in the source database")
    with building(business_today()), db.transaction() as conn:
        site = conn.execute("SELECT * FROM site WHERE slug = %s", (slug,)).fetchone()
        identity = stories.site_identity(conn, site)
        corp = stories.corpus(conn, left["kind"])
    assert stories.same_development(_report(left), _report(right), identity, corp) is same


def test_debt_basis_says_what_each_number_is(sites):
    if "jupiter" not in sites:
        pytest.skip("Jupiter is not in the source database")
    exposed = build("site_detail", "jupiter")["debtExposed"]
    basis = exposed["basis"]
    assert build("site_tab:debt", "jupiter")["basis"] == basis  # one basis for the card and the Debt tab
    iid = exposed["instrumentId"]
    assert basis["instrument"] == f"{exposed['name']} ({iid})"
    quote = copied_rows("SELECT * FROM price_obs WHERE instrument_id = %s AND kind = 'dealer' AND obs_date <= %s "
                        "ORDER BY obs_date DESC, id DESC LIMIT 1", (iid, business_today()))
    model = exposed["mardenPrice"]["value"]
    if quote and model is not None:
        q = basis["quote"]
        price, when = float(quote[0]["price"]), quote[0]["obs_date"]
        assert q["price"]["value"] == round(price, 4) and q["price"]["asOf"] == when.isoformat()
        assert q["price"]["tag"] == quote[0]["tag"] and q["price"].get("sourceUrl") == quote[0]["source_url"]
        assert q["bid"]["value"] is None and q["ask"]["value"] is None  # no record states a bid or an ask
        gap = round(model - price, 1)
        sign = MINUS if gap < 0 else "+" if gap > 0 else ""
        assert basis["vsQuote"] == (f"Model value: {model:.1f}; {sign}{abs(gap):.1f} points vs dealer quote "
                                    f"({when:%b} {when.day})")
        if q["mid"]["value"] is not None:
            lo, hi = (float(x) for x in q["range"]["value"].split("\u2013"))
            assert abs((lo + hi) / 2 - price) < 0.05
    facts = {r["field"]: r for r in copied_rows(
        "SELECT * FROM fact_current WHERE subject_type = 'instrument' AND subject_id = %s", (iid,))}
    if not ({"amount_basis", "commitment_usd", "drawn_usd"} & facts.keys()):
        assert basis["size"]["label"] == "Facility size as reported"  # no record says committed or drawn
    amount = facts.get("amount_usd")
    if amount and basis["size"]["reported"]["tag"] != amount["tag"]:
        # the amount is quoted from the research record that states it, with that record's source
        assert f"{amount['value'] / 1e9:,.0f}" in basis["size"]["reported"]["value"]
    filed = [r["value"]["text"] for r in copied_rows(
        "SELECT value FROM fact WHERE subject_type = 'site' AND subject_id = 'jupiter' AND status = 'published' "
        "AND field = 'structure_term' AND source_url = %s ORDER BY id", (amount["source_url"] if amount else "",))]
    assert (basis["size"]["tranches"]["value"] or None) == (" ".join(filed) or None)
    rec = copied_rows("SELECT output FROM model_output WHERE model = 'recovery' AND subject_id = %s AND as_of <= %s "
                      "ORDER BY as_of DESC, created_at DESC LIMIT 1", (iid, business_today()))
    price_out = copied_rows("SELECT output FROM model_output WHERE model = 'price' AND subject_id = %s "
                            "AND as_of <= %s ORDER BY as_of DESC, created_at DESC LIMIT 1", (iid, business_today()))
    derived = rec and (rec[0]["output"].get("recovery") or {}).get("value") is not None
    cents = price_out[0]["output"].get("recovery") if price_out else None
    if not derived and isinstance(cents, int | float):
        r = basis["recovery"]
        assert r["basis"] == "assumption"
        assert r["headline"].startswith(f"Assumption, not derived: {round(cents * 100)}c on ")
        assert r["method"]["sourceUrl"].endswith("pricing-v0.1.md") and r["assumptions"]
        assert any(a.startswith("Not derived from this site: ") for a in r["assumptions"])


def test_short_date_is_the_cards_date():
    from marden.readmodels.views.sites_debt_basis import short_date
    assert short_date(date(2026, 9, 18)) == "Sep 18"
