"""Payer and obligation builders (payers, payer, payer_capacity, obligations, obligation_company, leases) on
real seeded rows.

Rows are copied from the local `marden` database (payer_rows.copy_payer_area): every obligation edge with its
facts, the entities, sites, chain parties, instruments, holdings and model outputs. Expectations are computed
from the copied rows with SQL independent of the builders, so the tests follow the data as collectors and
models add to it. Model fields (leverage, funding gap, downgrade band, reconciliation, trigger leases) must
be n/d while their model has not run, and must show its output once it has.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from payer_rows import copy_payer_area
from real_rows import copied_rows
from ts_shapes import assert_matches_types

from marden.core import db
from marden.readmodels import business_today, rebuild_all
from marden.readmodels.context import building
from marden.readmodels.deps import targets
from marden.readmodels.registry import get_builder
from marden.readmodels.views import leases, obligations, payer_capacity, payers  # noqa: F401 (registers builders)

NAMED_PAYERS = ("oracle", "openai", "coreweave", "microsoft", "anthropic", "google", "lambda-inc")
NAMED_EDGES = {  # V1 subjects: Jupiter, Abilene, Frontier, The Barn, CoreWeave DDTL 3.0
    "oracle--project-jupiter--lease": "jupiter",
    "oracle--abilene-stargate--lease": "abilene",
    "oracle--vantage-frontier-port-washington--lease": None,
    "oracle--saline-twp-related-digital--lease": "the-barn",
    "openai--coreweave--compute-purchase": "coreweave-ddtl-3",
}
TYPES = {
    "payers": "PayersPage",
    "payer": "Payer",
    "payer_capacity": "PayerCapacity",
    "obligations": "ObligationGraph",
    "obligation_company": "CompanyObligations",
    "leases": "LeaseMapPage",
}
LINK_TO_STATUS = {"red": "Broken", "amber": "Strained", "green": "Holding"}


@pytest.fixture
def edges() -> list[str]:
    return copy_payer_area()


def build(view: str, key: str = "all", day=None) -> Any:
    b = get_builder(view)
    assert b is not None, view
    with building(day or business_today()), db.transaction() as conn:
        if b.build_many is not None:
            return dict(b.build_many(conn))[key]
        return b.build_one(conn, key)


def payer_keys() -> set[str]:
    rows = copied_rows(
        """SELECT entity_id AS id FROM chain_party WHERE role IN ('tenant', 'end_user') AND entity_id IS NOT NULL
           UNION SELECT o.from_entity_id FROM obligation o JOIN entity e ON e.id = o.from_entity_id
            WHERE o.type IN ('Lease', 'Compute purchase', 'Guarantee or backstop')
              AND e.kind IN ('cloud', 'ai_lab', 'neocloud') AND (e.parent_id IS NULL OR e.parent_id = e.id)
           UNION SELECT o.to_entity_id FROM obligation o JOIN entity e ON e.id = o.to_entity_id
            WHERE o.type IN ('Lease', 'Compute purchase') AND e.kind = 'neocloud'
              AND (e.parent_id IS NULL OR e.parent_id = e.id)"""
    )
    return {r["id"] for r in rows}


def known_edges() -> set[str]:
    rows = copied_rows("""SELECT o.id FROM obligation o WHERE EXISTS (SELECT 1 FROM fact_current f
                            WHERE f.subject_type = 'obligation' AND f.subject_id = o.id AND f.field = 'edge_type')
                          AND EXISTS (SELECT 1 FROM fact f WHERE f.subject_type = 'obligation'
                            AND f.subject_id = o.id AND f.field = 'evidence' AND f.status = 'published')""")
    return {r["id"] for r in rows}


def fact(subject_type: str, subject_id: str, field: str) -> dict | None:
    rows = copied_rows(
        "SELECT * FROM fact_current WHERE subject_type = %s AND subject_id = %s AND field = %s",
        (subject_type, subject_id, field),
    )
    return rows[0] if rows else None


def output(model: str, subject_type: str, subject_id: str) -> dict | None:
    rows = copied_rows(
        "SELECT * FROM model_output WHERE model = %s AND subject_type = %s AND subject_id = %s "
        "ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1",
        (model, subject_type, subject_id),
    )
    return rows[0] if rows else None


def billions(row: dict | None) -> float | None:
    return round(float(row["value"]) / 1e9, 3) if row and isinstance(row["value"], int | float) else None


def test_every_view_rebuilds_every_key(edges):
    companies = {
        r["id"]
        for r in copied_rows("SELECT from_entity_id AS id FROM obligation UNION SELECT to_entity_id FROM obligation")
    }
    expected = {
        "payers": 1,
        "obligations": 1,
        "leases": 1,
        "payer": len(payer_keys()),
        "payer_capacity": len(payer_keys()),
        "obligation_company": len(companies),
    }
    for view, count in expected.items():
        stats = rebuild_all(view)
        assert not stats.errors, (view, stats.errors[:3])
        assert stats.built == count, view
    stored = copied_rows(
        "SELECT view, count(*) AS n FROM read_model WHERE as_of = %s GROUP BY view", (business_today(),)
    )
    assert {r["view"]: r["n"] for r in stored} == expected


@pytest.mark.parametrize("view", list(TYPES))
def test_documents_match_the_response_types(edges, view):
    if view in ("payer", "payer_capacity"):
        keys = [k for k in NAMED_PAYERS if k in payer_keys()]
    elif view == "obligation_company":
        keys = ["oracle", "openai", "coreweave", "project-jupiter", "meta"]
    else:
        keys = ["all"]
    docs = [build(view, key) for key in keys]
    assert all(d is not None for d in docs), view
    assert_matches_types(TYPES[view], docs)


def test_obligation_edges_follow_their_facts(edges):
    graph = build("obligations")
    by_id = {e["id"]: e for e in graph["edges"]}
    assert set(by_id) == known_edges()
    for eid, site in NAMED_EDGES.items():
        if eid in by_id and site:
            assert site in by_id[eid]["linkedSites"], eid
    for edge in graph["edges"]:
        ob = copied_rows("SELECT * FROM obligation WHERE id = %s", (edge["id"],))[0]
        assert (edge["from"], edge["to"], edge["type"]) == (ob["from_entity_id"], ob["to_entity_id"], ob["type"])
        assert edge["totalB"]["value"] == billions(fact("obligation", edge["id"], "total_usd"))
        evidence = copied_rows(
            "SELECT * FROM fact WHERE subject_type = 'obligation' AND subject_id = %s "
            "AND field = 'evidence' AND status = 'published'",
            (edge["id"],),
        )
        assert len(edge["evidence"]) == len(evidence) > 0  # every edge opens its source passage
        status = fact("obligation", edge["id"], "status")
        if status:
            assert edge["status"] == status["value"]
        assert set(ob["instrument_ids"]) <= set(edge["linkedInstruments"])
        modeled = copied_rows(
            "SELECT output FROM model_output WHERE model = 'obligations' AND subject_type = 'obligation' "
            "AND subject_id = %s AND as_of <= %s ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1",
            (edge["id"], business_today()),
        )
        expected = [a for a in modeled[0]["output"].get("annual") or [] if a["amountB"]["value"] is not None] \
            if modeled else []
        assert edge["annual"] == expected  # the obligations model's schedule, as stored (none before it runs)
    total = sum(e["totalB"]["value"] or 0 for e in graph["edges"])
    filed = sum(e["totalB"]["value"] or 0 for e in graph["edges"] if e["totalB"]["tag"] == "F")
    assert graph["kpis"]["totalB"] == round(total, 3)
    assert graph["kpis"]["filedPct"] == (round(100 * filed / total, 1) if total else 0.0)
    for node in graph["nodes"]:
        owes = sum(e["totalB"]["value"] or 0 for e in graph["edges"] if e["from"] == node["id"])
        assert node["owesB"] == round(owes, 3)
        assert node["id"] in {e["from"] for e in graph["edges"]} | {e["to"] for e in graph["edges"]}


def _worst_link(entity_id: str, roles: tuple[str, ...] | None = None) -> str | None:
    worst = None
    for party in copied_rows("SELECT * FROM chain_party WHERE entity_id = %s", (entity_id,)):
        if roles and party["role"] not in roles:
            continue
        out = output("status", "site", party["site_slug"])
        for link in (out or {"output": {}})["output"].get("chain") or []:
            status = LINK_TO_STATUS.get(link.get("status")) if link.get("role") == party["role"] else None
            if status and (
                worst is None
                or ["Broken", "Strained", "Holding"].index(status) < ["Broken", "Strained", "Holding"].index(worst)
            ):
                worst = status
    return worst


def test_company_page_splits_the_edges_and_waits_for_its_model(edges):
    graph = build("obligations")
    for key in ("oracle", "coreweave", "openai", "meta"):
        page = build("obligation_company", key)
        assert {e["id"] for e in page["owes"]} == {e["id"] for e in graph["edges"] if e["from"] == key}
        assert {e["id"] for e in page["isOwed"]} == {e["id"] for e in graph["edges"] if e["to"] == key}
        assert page["node"] == next(n for n in graph["nodes"] if n["id"] == key)
        assert page["node"]["status"] == _worst_link(key)
        out = output("obligation_company", "entity", key)
        if out is None:
            assert page["concentration"] == [] and page["fundingGap"] == [] and "ndReasons" not in page
        else:  # the model's plain reason shows only for a list the page leaves empty
            given = out["output"].get("ndReasons") or {}
            expected = {k: given[k] for k in ("concentration", "fundingGap") if not page[k] and given.get(k)}
            assert page.get("ndReasons", {}) == expected
            assert all(g["gapB"]["value"] is not None for g in page["fundingGap"])  # n/d gap rows left out
        by_year: dict[int, dict[str, float]] = {}
        for e in page["owes"]:  # annual commitments by year and counterparty: the edges' schedules summed
            for a in e["annual"]:
                by_year.setdefault(a["year"], {}).setdefault(e["to"], 0.0)
                by_year[a["year"]][e["to"]] += a["amountB"]["value"]
        assert page["owesByYear"] == [
            {"year": y, "byCounterparty": [{"id": c, "amountB": round(v, 3)} for c, v in sorted(parts.items())]}
            for y, parts in sorted(by_year.items())
        ]
    assert build("obligation_company", "blackrock") is None  # not at either end of an edge


def test_payer_page_totals_match_its_sites(edges):
    for key in ("oracle", "openai", "coreweave"):
        page = build("payer", key)
        rows = page["sites"]
        assert page["kpis"]["siteDebtB"]["value"] == round(sum(r["debtB"] for r in rows), 3)  # spec 08 acceptance
        parties = {
            r["site_slug"]
            for r in copied_rows(
                "SELECT site_slug FROM chain_party WHERE entity_id = %s AND role IN ('tenant', 'end_user')", (key,)
            )
        }
        us = {r["slug"] for r in copied_rows("SELECT slug FROM site WHERE state IS NULL OR state !~ 'Australia'")}
        assert parties & us <= {r["slug"] for r in rows}
        for row in rows:
            status = output("status", "site", row["slug"])
            assert row["status"] == (status["output"]["status"] if status else None)
            cushion = output("cushion", "site", row["slug"])
            months = ((cushion or {"output": {}})["output"].get("projection") or {}).get("cushionMonths") or {}
            assert row["cushionMonths"] == months.get("value")
    oracle = build("payer", "oracle")
    assert {"jupiter", "abilene", "frontier", "the-barn"} <= {r["slug"] for r in oracle["sites"]}
    assert oracle["role"] == "tenant" and build("payer", "openai")["role"] == "end_user"
    notes = copied_rows("""SELECT f.value FROM instrument i JOIN fact_current f ON f.subject_type = 'instrument'
                             AND f.subject_id = i.id AND f.field = 'amount_usd'
                           WHERE i.issuer_entity_id = 'oracle' AND i.kind IN ('bond', 'convert')""")
    assert oracle["kpis"]["notesB"]["value"] == round(sum(r["value"] / 1e9 for r in notes), 3)
    funds = copied_rows("""SELECT count(DISTINCT h.fund_entity_id) AS n FROM holding h JOIN instrument i
                             ON i.id = h.instrument_id WHERE i.issuer_entity_id = 'oracle'""")[0]["n"]
    assert 0 < oracle["kpis"]["noteHolders"]["value"] <= funds and oracle["kpis"]["noteHolders"]["tag"] == "F"
    ratings = copied_rows("""SELECT f.value, f.valid_from FROM chain_party c JOIN fact_current f
                               ON f.subject_type = 'site' AND f.subject_id = c.site_slug AND f.field = 'tenant_rating'
                             WHERE c.entity_id = 'oracle' AND c.role = 'tenant' ORDER BY f.valid_from DESC""")
    if ratings:
        assert oracle["kpis"]["rating"]["value"] in {
            r["value"] for r in ratings if r["valid_from"] == ratings[0]["valid_from"]
        }
    paid = {p["name"]: p for p in oracle["paidBy"]}
    if "OpenAI" in paid:
        assert paid["OpenAI"]["status"] == _worst_link("openai", ("end_user",))
    for key in ("oracle", "coreweave"):  # the 5y CDS is the price engine's bond-implied `cds` output
        cds = output("cds", "entity", key)
        kpi = (oracle if key == "oracle" else build("payer", key))["kpis"]["cds5y"]
        if cds is None or cds["output"].get("spreadBp") is None:
            assert kpi["value"] is None and kpi["tag"] == "M"
        else:
            assert kpi == {
                "value": f"{cds['output']['spreadBp']:.0f}bp",
                "tag": "M",
                "asOf": cds["as_of"].isoformat(),
                "methodVersion": cds["version"],
                "sourceName": cds["output"].get("label") or "Marden bond-implied CDS",
            }
    assert "stressSummary" not in oracle or output("stress", "entity", "oracle") is not None
    assert build("payer", "blackrock") is None  # a fund is not a payer


def test_capacity_shows_filed_cash_flow_and_waits_for_the_model(edges):
    doc = build("payer_capacity", "oracle")
    filed = copied_rows("""SELECT valid_from, value FROM fact_current WHERE subject_type = 'entity'
                             AND subject_id = 'oracle' AND field = 'operating_cash_flow_12m_usd'""")
    years = {y["fiscalYear"]: y for y in doc["years"]}
    for row in filed:
        label = f"FY{row['valid_from'].year}"
        if label in years:
            assert years[label]["ocfB"]["value"] == round(row["value"] / 1e9, 3)
            assert years[label]["ocfB"]["tag"] == "F"
    if output("payer_capacity", "entity", "oracle") is None:
        for key in ("spreadBp", "leaseAdjLeverageToday", "triggerHeadroomTurns", "fundingGap3yB", "downgradeRisk"):
            assert doc[key]["value"] is None and doc[key]["tag"] == "M", key
        assert doc["ratings"] == [] and doc["topCounterparty"] is None
        assert all(y["fundingGapB"]["value"] is None for y in doc["years"])
    out = output("payer_capacity", "entity", "oracle")
    reasons = (out or {"output": {}})["output"].get("ndReasons")
    if isinstance(reasons, dict):  # the model's plain-words n/d reasons pass through as written
        assert doc["ndReasons"] == {k: v for k, v in reasons.items() if k in payer_capacity.ND_REASONS}
        modeled = {y["fiscalYear"]: y for y in out["output"]["years"]}
        for label, year in years.items():  # filed years carry the model's filed leverage too
            if label in modeled:
                assert year["leaseAdjLeverage"]["value"] == modeled[label]["leaseAdjLeverage"]["value"]
    else:
        assert "ndReasons" not in doc
    owed = copied_rows("""SELECT DISTINCT unnest(instrument_ids) AS id FROM obligation WHERE from_entity_id = 'oracle'
                            AND type IN ('Lease', 'Compute purchase', 'Guarantee or backstop')""")
    assert {r["id"] for r in owed} <= {x["instrumentId"] for x in doc["exposed"]}
    for item in doc["exposed"]:
        gap = output("mark_gap", "instrument", item["instrumentId"])
        if gap and isinstance(gap["output"].get("count"), int):
            assert item["holdersCount"] == gap["output"]["count"]
    page = build("payers")
    assert {r["slug"] for r in page["rows"]} == payer_keys()
    risks = [r["downgradeRisk"]["value"] for r in page["rows"]]
    order = {"High": 0, "Watch": 1, "Low": 2}
    assert [order.get(v, 3) for v in risks] == sorted(order.get(v, 3) for v in risks)  # sorted by downgrade risk


def test_rating_terms_in_the_debt_are_quoted(edges):
    rated = copied_rows("""SELECT o.from_entity_id AS payer, f.subject_id AS instrument FROM obligation o
                             JOIN fact f ON f.subject_type = 'instrument' AND f.subject_id = ANY(o.instrument_ids)
                              AND f.field = 'agreement_term' AND f.status = 'published' AND f.value->>'type' ~* 'rating'
                            WHERE o.type IN ('Lease', 'Compute purchase', 'Guarantee or backstop')""")
    keys = payer_keys()
    rated = [r for r in rated if r["payer"] in keys]
    if not rated:
        pytest.skip("no exposed instrument quotes a rating term")
    doc = build("payer_capacity", rated[0]["payer"])
    item = next(x for x in doc["exposed"] if x["instrumentId"] == rated[0]["instrument"])
    assert item["ratingTrigger"]["tag"] == "F" and item["ratingTrigger"]["value"]


def test_lease_map_lists_every_lease_and_waits_for_the_leases_model(edges):
    page = build("leases")
    lease_ids = {r["id"] for r in copied_rows("SELECT id FROM obligation WHERE type IN ('Lease', 'Compute purchase')")}
    assert {r["id"] for r in page["leases"]} == lease_ids & known_edges()
    for lease in page["leases"]:
        tags = {
            r["tag"].strip()
            for r in copied_rows(
                "SELECT tag FROM fact WHERE subject_type = 'obligation' AND subject_id = %s AND field = 'evidence' "
                "AND status = 'published'",
                (lease["id"],),
            )
        }
        assert lease["attribution"] == ("Filed" if "F" in tags else "Reported" if "R" in tags else "Inferred")
        ob = copied_rows("SELECT instrument_ids FROM obligation WHERE id = %s", (lease["id"],))[0]
        assert set(ob["instrument_ids"]) <= {d["instrumentId"] for d in lease["dependentDebt"]}
        assert lease["totalB"]["value"] == billions(fact("obligation", lease["id"], "total_usd"))
    outstanding = [r for r in page["leases"] if r["status"] not in ("Started", "Terminated")]
    assert page["totals"]["attributedB"] == round(sum(r["totalB"]["value"] or 0 for r in outstanding), 3)
    model = copied_rows("SELECT id FROM model_output WHERE model = 'leases' LIMIT 1")
    if not model:
        assert page["reconciliation"] == [] and page["totals"]["disclosedB"] == 0
        assert not any(r["triggerLease"] for r in page["leases"]) and page["totals"]["triggerLeases"] == 0
        assert all(r["priorityScore"]["value"] is None for r in page["leases"])
    named = {r["id"]: r for r in page["leases"] if r["id"] in NAMED_EDGES}
    assert set(named) == set(NAMED_EDGES) & set(r["id"] for r in page["leases"])
    if "openai--coreweave--compute-purchase" in named:
        assert named["openai--coreweave--compute-purchase"]["kind"] == "Compute contract"


def test_backfilled_snapshot_shows_only_what_was_known(edges):
    seed_day = copied_rows("SELECT min(learned_at)::date AS d FROM fact WHERE subject_type = 'obligation'")[0]["d"]
    past = seed_day - timedelta(days=1)
    graph = build("obligations", day=past)
    assert graph["edges"] == [] and graph["kpis"]["totalB"] == 0  # no edge was known before it was learned
    assert build("leases", day=past)["leases"] == []
    assert build("obligation_company", "oracle", day=past) is None
    stats = rebuild_all("obligations", as_of=past)
    assert not stats.errors and stats.events == 0


def test_dependencies_reach_the_payer_views(edges):
    with db.transaction() as conn:
        pairs = targets("obligation", "oracle--project-jupiter--lease", "total_usd", conn=conn)
        assert {
            ("obligations", "all"),
            ("leases", "all"),
            ("payers", "all"),
            ("payer", "oracle"),
            ("payer_capacity", "oracle"),
            ("obligation_company", "oracle"),
            ("obligation_company", "project-jupiter"),
        } <= pairs
        inst = copied_rows("SELECT instrument_ids[1] AS id FROM obligation WHERE id = 'oracle--project-jupiter--lease'")
        if inst and inst[0]["id"]:
            assert {("payer_capacity", "oracle"), ("leases", "all")} <= targets(
                "instrument", inst[0]["id"], "amount_usd", conn=conn
            )
        note = copied_rows(
            "SELECT h.id FROM holding h JOIN instrument i ON i.id = h.instrument_id "
            "WHERE i.issuer_entity_id = 'oracle' AND i.kind = 'bond' LIMIT 1"
        )
        if note:
            assert ("payer", "oracle") in targets("holding", str(note[0]["id"]), "@holding", conn=conn)
        assert {("obligations", "all"), ("obligation_company", "oracle"), ("payer", "oracle")} <= targets(
            "site", "jupiter", "@model:status", conn=conn
        )
        assert ("leases", "all") in targets("entity", "oracle", "@model:payer_capacity", conn=conn)
