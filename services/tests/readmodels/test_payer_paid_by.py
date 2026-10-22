"""The payer page's "Who pays" rail (payers_paid_by.py) on real seeded rows (payer_rows.copy_payer_area).

The owner's screenshot of Oracle's page showed "OpenAI, end user on 5 of 7 sites · Strained" with one press
line. Each party above a payer now carries the rule its worst link tripped (from the status model's own
output), the payer's sites it is end user of, what it owes the payer on the obligation map, and its dated
funding, commitment and rating statements, newest first, each with its tag and source. Expectations come
from the copied rows with SQL independent of the builder.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from payer_rows import copy_payer_area
from real_rows import copied_rows

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.deps import targets
from marden.readmodels.registry import get_builder
from marden.readmodels.views import payers  # noqa: F401 (registers the builder)

LINK_TO_STATUS = {"red": "Broken", "amber": "Strained", "green": "Holding"}
RANK = ["Broken", "Strained", "Holding"]


@pytest.fixture
def edges() -> list[str]:
    return copy_payer_area()


def payer(key: str) -> dict[str, Any]:
    b = get_builder("payer")
    assert b is not None
    with building(business_today()), db.transaction() as conn:
        doc = b.build_one(conn, key)
    assert doc is not None, key
    return doc


def links(entity_id: str, roles: tuple[str, ...] | None) -> list[dict[str, Any]]:
    """The party's own links in each of its sites' latest status output."""
    out = []
    for party in copied_rows("SELECT * FROM chain_party WHERE entity_id = %s", (entity_id,)):
        if roles and party["role"] not in roles:
            continue
        row = copied_rows(
            "SELECT output FROM model_output WHERE model = 'status' AND subject_type = 'site' AND subject_id = %s "
            "ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1",
            (party["site_slug"],),
        )
        for link in (row[0]["output"].get("chain") or []) if row else []:
            if link.get("role") == party["role"]:
                out.append(link)
    return out


def funding(entity_id: str) -> list[dict[str, Any]]:
    return copied_rows(
        """SELECT * FROM fact WHERE subject_type = 'entity' AND subject_id = %s AND field = 'funding_fact'
             AND status = 'published' AND (valid_to IS NULL OR valid_to > CURRENT_DATE)""",
        (entity_id,),
    )


def oracle_end_user_sites(end_user: str) -> set[str]:
    rows = copied_rows(
        """SELECT eu.site_slug FROM chain_party eu JOIN chain_party t ON t.site_slug = eu.site_slug AND t.role = 'tenant'
            WHERE eu.role = 'end_user' AND eu.entity_id = %s AND t.entity_id = 'oracle'""",
        (end_user,),
    )
    return {r["site_slug"] for r in rows}


def test_who_pays_oracle_says_why_and_how_exposed(edges):
    doc = payer("oracle")
    above = {p["name"]: p for p in doc["paidBy"]}
    users = {r["entity_id"] for r in copied_rows(
        """SELECT DISTINCT eu.entity_id FROM chain_party eu JOIN chain_party t ON t.site_slug = eu.site_slug
            WHERE t.entity_id = 'oracle' AND t.role = 'tenant' AND eu.role = 'end_user' AND eu.entity_id IS NOT NULL""")}
    assert users, "no end user above Oracle in the copied chains"
    page_sites = {s["slug"] for s in doc["sites"]}
    for uid in users:
        name = copied_rows("SELECT name FROM entity WHERE id = %s", (uid,))[0]["name"]
        mine = links(uid, ("end_user",))
        statuses = [LINK_TO_STATUS[x["status"]] for x in mine if x.get("status") in LINK_TO_STATUS]
        if not statuses:
            assert name not in above  # no judged link: not listed
            continue
        party = above[name]
        worst = min(statuses, key=RANK.index)
        assert party["status"] == worst
        # How exposed Oracle is: the sites of its own page where this party is the end user.
        assert set(party["endUserOn"]) == oracle_end_user_sites(uid) & page_sites
        assert party["slug"] == uid  # an end user is a payer: its own page exists
        # Why: the rule of the worst link, as the status model stated it, and the statement that tripped it.
        if worst == "Holding":
            assert party["ruleCode"] is None and party["rule"] is None
            assert not any(f["tripped"] for f in party["facts"])
            continue
        tripping = [x for x in mine if LINK_TO_STATUS.get(x.get("status")) == worst and x.get("fact") == party["note"]]
        assert tripping, (name, party["note"])
        assert party["ruleCode"] == tripping[0]["ruleCode"]
        assert tripping[0]["reason"].startswith(f"{party['ruleCode']} {party['rule']}")
        assert sum(f["tripped"] for f in party["facts"]) == 1
        # Its funding and commitment statements, each with the row's own tag, source and date.
        rows = funding(uid)
        by_label = {f["label"]: f["fact"] for f in party["facts"]}
        for row in rows:
            got = by_label[row["value"]["label"]]
            assert got["tag"] == row["tag"].strip() and got["asOf"] == row["valid_from"].isoformat()
            assert got["sourceName"] == row["source_name"] and str(row["value"]["value"]) in got["value"]
        dates = [date.fromisoformat(f["fact"]["asOf"]) for f in party["facts"]]
        assert dates == sorted(dates, reverse=True)  # newest first
        assert all(f["fact"]["value"] and f["fact"]["tag"] in "FRM" and f["fact"]["asOf"] for f in party["facts"])


def test_openai_on_oracle_page_is_strained_by_its_funding_need(edges):
    """The case in the owner's screenshot, while the status model's output still trips E1 on OpenAI."""
    mine = links("openai", ("end_user",))
    if not any(x.get("ruleCode") == "E1" for x in mine):
        pytest.skip("the status model does not trip E1 on OpenAI in the copied outputs")
    party = next(p for p in payer("oracle")["paidBy"] if p["slug"] == "openai")
    assert (party["status"], party["ruleCode"], party["rule"]) == ("Strained", "E1", "End user's plan needs new funding")
    tripped = next(f for f in party["facts"] if f["tripped"])
    assert tripped["label"] in {r["value"]["label"] for r in funding("openai")}
    labels = [f["label"] for f in party["facts"]]
    assert "Rating" in labels  # OpenAI's rating statement (unrated) is shown with its source
    edge = copied_rows(
        """SELECT o.id FROM obligation o WHERE o.from_entity_id = 'openai' AND o.to_entity_id = 'oracle'
             AND o.type = 'Compute purchase'
             AND EXISTS (SELECT 1 FROM fact_current f WHERE f.subject_type = 'obligation' AND f.subject_id = o.id
                           AND f.field = 'edge_type')
             AND EXISTS (SELECT 1 FROM fact f WHERE f.subject_type = 'obligation' AND f.subject_id = o.id
                           AND f.field = 'evidence' AND f.status = 'published')"""
    )
    assert party["owes"] == (["Compute contract"] if edge else [])


def test_holding_parties_name_no_rule(edges):
    for key in ("coreweave", "oracle", "microsoft"):
        for party in payer(key)["paidBy"]:
            if party["status"] == "Holding":
                assert party["ruleCode"] is None and party["rule"] is None
                assert not any(f["tripped"] for f in party["facts"])
            for item in party["facts"]:
                assert set(item) == {"label", "fact", "tripped"}


def test_a_new_rating_statement_reaches_the_payers_the_party_pays(edges):
    """paidBy shows each party's rating statement: a `tenant_rating` on a site where it is tenant."""
    rows = copied_rows(
        """SELECT r.site_slug, t.entity_id AS payee FROM chain_party r
             JOIN chain_party eu ON eu.entity_id = r.entity_id AND eu.role = 'end_user'
             JOIN chain_party t ON t.site_slug = eu.site_slug AND t.role = 'tenant'
            WHERE r.role = 'tenant' AND t.entity_id IS NOT NULL AND t.entity_id <> r.entity_id"""
    )
    if not rows:
        pytest.skip("no party is tenant on one site and end user on another in the copied chains")
    with db.transaction() as conn:
        for row in rows:
            assert ("payer", row["payee"]) in targets("site", row["site_slug"], "tenant_rating", conn=conn)
