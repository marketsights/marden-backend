"""A payer's "Debt exposed" counts only debt whose payment chain runs through that payer (payers_exposure.py),
and a payer no agency rates shows its reported scale (payer_scale.py), on real seeded rows.

Regression: every edge on a case carries the case's whole debt (`linked_instruments`), so a compute customer
used to pick up its provider's site debt: OpenAI showed Oracle's leased-campus debt, Anthropic showed the Valor
GPU loans SpaceX's equipment leases service, and Google's backstops of Fluidstack's leases read as the same
debt row as Fluidstack's own. Expectations are computed with SQL independent of the builders.
"""

from __future__ import annotations

from collections import defaultdict

import pytest
from payer_rows import copy_payer_area
from real_rows import copied_rows
from test_payer_views import build, payer_keys

KNOWN = """EXISTS (SELECT 1 FROM fact_current f WHERE f.subject_type = 'obligation' AND f.subject_id = o.id
                     AND f.field = 'edge_type')
           AND EXISTS (SELECT 1 FROM fact f WHERE f.subject_type = 'obligation' AND f.subject_id = o.id
                       AND f.field = 'evidence' AND f.status = 'published')"""


@pytest.fixture
def edges() -> list[str]:
    return copy_payer_area()


def _edge_instruments() -> list[dict]:
    """(edge id, type, from, instrument) for every known edge, from the row and its linked_instruments fact."""
    return copied_rows(
        f"""SELECT o.id, o.type, o.from_entity_id AS owes, i AS instrument
              FROM obligation o, LATERAL (
                SELECT unnest(o.instrument_ids) AS i
                UNION SELECT jsonb_array_elements_text(f.value) FROM fact_current f
                 WHERE f.subject_type = 'obligation' AND f.subject_id = o.id AND f.field = 'linked_instruments'
                   AND jsonb_typeof(f.value) = 'array') x
             WHERE {KNOWN}"""
    )


def _family(entity_id: str) -> set[str]:
    rows = copied_rows(
        "SELECT id FROM entity WHERE id = %(e)s OR parent_id = %(e)s "
        "UNION SELECT parent_id FROM entity WHERE id = %(e)s AND parent_id IS NOT NULL",
        {"e": entity_id},
    )
    return {r["id"] for r in rows}


def test_exposed_debt_runs_through_the_payer(edges):
    rows = {r["slug"]: r for r in build("payers")["rows"]}
    assert set(rows) == payer_keys()
    links = _edge_instruments()
    lessees: dict[str, set[str]] = defaultdict(set)
    for r in links:
        if r["type"] == "Lease":
            lessees[r["instrument"]].add(r["owes"])
    instruments = {r["id"] for r in copied_rows("SELECT id FROM instrument")}
    for slug, row in rows.items():
        family = _family(slug)
        owed = defaultdict(set)
        for r in links:
            if r["owes"] == slug and r["instrument"] in instruments:
                owed[r["instrument"]].add(r["type"])
        counted = {e["instrumentId"]: e["link"] for e in row["exposed"]}
        elsewhere = {d["slug"] for d in row["exposedElsewhere"]}
        for iid, types in owed.items():
            others = lessees.get(iid, set()) - family
            if "Lease" in types:
                assert counted.get(iid) == "Lease", (slug, iid)
            elif "Compute purchase" in types and not others:
                assert counted.get(iid) == "Compute contract", (slug, iid)
            elif "Compute purchase" in types:  # the lessee's rent services it: counted there, named here
                assert iid not in counted and others <= elsewhere, (slug, iid)
            else:
                assert counted.get(iid) == "Guarantee", (slug, iid)  # a backstop, never the payer's own chain
        assert set(counted) <= set(owed), slug
        for d in row["exposedElsewhere"]:
            assert d["instruments"] > 0 and d["slug"] in rows, (slug, d)
    # Two payers never count the same debt as their own unless both owe a lease it sits behind.
    own = {s: {e["instrumentId"] for e in r["exposed"] if e["link"] != "Guarantee"} for s, r in rows.items()}
    for a in rows:
        for b in rows:
            if a < b and own[a] and own[a] == own[b]:
                assert all({a, b} <= lessees.get(i, set()) for i in own[a]), (a, b)


def test_named_payers_no_longer_carry_another_payers_debt(edges):
    rows = {r["slug"]: r for r in build("payers")["rows"]}
    oracle_leased = {r["instrument"] for r in _edge_instruments() if r["owes"] == "oracle" and r["type"] == "Lease"}
    if "openai" in rows and oracle_leased:
        assert not oracle_leased & {e["instrumentId"] for e in rows["openai"]["exposed"]}
        assert "oracle" in {d["slug"] for d in rows["openai"]["exposedElsewhere"]}
    vci = {r["instrument"] for r in _edge_instruments() if r["owes"] == "xai-spacex" and r["type"] == "Lease"}
    if "anthropic" in rows and vci:
        assert not vci & {e["instrumentId"] for e in rows["anthropic"]["exposed"]}
        assert vci <= {e["instrumentId"] for e in rows["xai-spacex"]["exposed"]}
    if "google" in rows and "fluidstack" in rows:
        assert {e["link"] for e in rows["google"]["exposed"]} <= {"Guarantee"}
        assert {e["link"] for e in rows["fluidstack"]["exposed"]} <= {"Lease"}


def test_unrated_payers_show_their_reported_scale(edges):
    rows = {r["slug"]: r for r in build("payers")["rows"]}
    fields = {"revenue": "reported_revenue", "raised": "funding_raised", "valuation": "valuation", "burn": "cash_burn"}
    for slug, row in rows.items():
        entity = copied_rows("SELECT ticker FROM entity WHERE id = %s", (slug,))[0]
        listing = copied_rows(
            "SELECT value FROM fact_current WHERE subject_type = 'entity' AND subject_id = %s AND field = 'listing'",
            (slug,),
        )
        assert row["listed"] == bool(entity["ticker"] or (listing and listing[0]["value"].get("ticker")))
        if row["ratings"]:
            assert row["scale"] == []
            continue
        figures = {f["key"]: f for f in row["scale"]}
        for key, field in fields.items():
            facts = copied_rows(
                "SELECT * FROM fact_current WHERE subject_type = 'entity' AND subject_id = %s AND field = %s",
                (slug, field),
            )
            if not facts:
                assert key not in figures or key == "revenue", (slug, key)
                continue
            fig = figures[key]
            assert fig["amountB"]["value"] == round(facts[0]["value"]["usd"] / 1e9, 3), (slug, key)
            assert fig["basis"] == facts[0]["value"]["basis"]
            assert fig["amountB"]["tag"] == facts[0]["tag"].strip() and fig["amountB"].get("sourceUrl")
