"""The `sites` seed loader against a slice of the real archive (MARDEN_SEED_DIR).

Each test loads a few real cases into the throwaway test database (committed, truncated after
the test) and checks rows, facts, tags, sources and idempotency. No sample data is made up: every
expected value below is read from the archive files themselves.
"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

from marden.core import config
from marden.seed import common

pytestmark = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")

CASES = ["JUPITER", "VANTAGE_TX", "VANTAGE_WI", "APLD_PF1", "APLD_PF1B4", "IREN_CHILDRESS"]


@pytest.fixture(scope="module")
def seed():
    config.reload_settings()
    common.seed_dir.cache_clear()
    return common.load_json


@pytest.fixture
def loaded(seed, committed):
    from marden.seed.sites import load

    return load(CASES)


def _counts(c) -> dict[str, int]:
    one = lambda sql: c.execute(sql).fetchone()["n"]  # noqa: E731
    return {
        "site": one("SELECT count(*) AS n FROM site"),
        "chain_party": one("SELECT count(*) AS n FROM chain_party"),
        "obligation": one("SELECT count(*) AS n FROM obligation"),
        "entity": one("SELECT count(*) AS n FROM entity"),
        "fact": one("SELECT count(*) AS n FROM fact WHERE producer = 'seed:sites'"),
        "document": one("SELECT count(*) AS n FROM document"),
    }


def test_second_run_writes_nothing_new(loaded, committed):
    from marden.seed.sites import load

    first = _counts(committed)
    again = load(CASES)
    assert _counts(committed) == first
    assert again["facts_new"] == 0 and again["facts_seen"] == loaded["facts_seen"]
    runs = committed.execute("SELECT ok, items_new FROM source_run ORDER BY id").fetchall()
    assert [r["ok"] for r in runs] == [True, True] and runs[1]["items_new"] == 0


def test_site_rows_follow_the_archive(loaded, committed, seed):
    titles = seed("aiwc/memo/titles.json")
    dossier = seed("aiwc/cases/JUPITER.json")
    point = next(s for s in seed("aiwc/q3x/site.json")["sites"] if s["case"] == "JUPITER")
    row = committed.execute(
        "SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM site WHERE slug = 'jupiter'"
    ).fetchone()
    assert row["name"] == titles["JUPITER"][0] and row["legacy_group"] == "JUPITER" and row["in_v1"]
    assert (row["state"], row["county"]) == (dossier["site"]["state"], dossier["site"]["county"])
    assert (round(row["lat"], 5), round(row["lon"], 5)) == (point["lat"], point["lon"])
    assert any("Red Chiles" in n for n in row["tracking_names"])
    nd = seed("aiwc/cases/APLD_PF1.json")["site"]["state"]  # 'ND' is North Dakota, not 'n/d'
    assert committed.execute(
        "SELECT value FROM fact WHERE subject_id = 'polaris-forge-1' AND field = 'state'"
    ).fetchone()["value"] == nd
    # Building 4 shares the Polaris Forge 1 campus point; its coordinates fact says so with tag M.
    shared = committed.execute(
        "SELECT tag, value FROM fact WHERE subject_id = 'polaris-forge-1-building-4' AND field = 'coordinates'"
    ).fetchone()
    assert shared["tag"] == "M" and shared["value"]["campus_key"] == "apld_polaris_forge_1"


def test_site_facts_carry_source_tag_and_document(loaded, committed, seed):
    cap = seed("aiwc/cases/JUPITER.json")["capacity"]
    fact = committed.execute(
        """SELECT f.*, d.url AS doc_url FROM fact f JOIN document d ON d.id = f.document_id
           WHERE subject_type = 'site' AND subject_id = 'jupiter' AND field = 'gross_mw'
             AND d.url LIKE '%%aiwc/cases/JUPITER.json'"""
    ).fetchone()
    assert fact["value"] == cap["gross_mw"] and fact["unit"] == "MW" and fact["quote"] == cap["quote"]
    assert fact["source_url"] == cap["src"][0] and fact["tag"] == common.tag_for_url(cap["src"][0])
    assert fact["valid_from"] == date.fromisoformat(seed("aiwc/cases/JUPITER.json")["asof"])
    ctr = next(r for r in seed("aiwc/q3x/ctr.json")["rows"] if r["id"] == "IREN_CHILDRESS")
    outside = committed.execute(
        "SELECT value FROM fact WHERE subject_id = 'childress' AND field = 'outside_date' AND quote = %s",
        (ctr["quote"],),
    ).fetchone()
    assert outside["value"] == ctr["outside"]
    # Jupiter's lease has no outside date and no term in any file: nothing is written (n/d).
    assert not committed.execute(
        "SELECT 1 FROM fact WHERE subject_id = 'jupiter' AND field IN ('outside_date', 'lease_term_years')"
    ).fetchall()


def test_every_fact_follows_the_seed_rules(loaded, committed):
    rows = committed.execute(
        """SELECT f.tag, f.method_version, f.status, f.valid_from, f.source_name, d.url
           FROM fact f LEFT JOIN document d ON d.id = f.document_id WHERE f.producer = 'seed:sites'"""
    ).fetchall()
    assert rows
    for r in rows:
        assert r["url"] and r["url"].startswith("seed://marden_data_2026-10-20/")
        assert r["status"] == "published" and r["source_name"]
        assert (r["tag"] == "M") == bool(r["method_version"])
        assert r["valid_from"] <= date(2026, 10, 21)
    fields = {r["field"] for r in committed.execute("SELECT DISTINCT field FROM fact")}
    assert not {f for f in fields if "price" in f or f.startswith(("px", "trace"))}
    assert committed.execute("SELECT count(*) AS n FROM price_obs").fetchone()["n"] == 0


def test_chain_links_use_short_names_and_entities(loaded, committed, seed):
    short = seed("aiwc/memo/shortnames.json")["JUPITER"]
    links = {r["role"]: r for r in committed.execute("SELECT * FROM chain_party WHERE site_slug = 'jupiter'")}
    assert links["tenant"]["label"] == short[1] and links["tenant"]["entity_id"] == "oracle"
    assert links["developer"]["label"] == short[0]
    assert links["end_user"]["entity_id"] == "openai"
    memo = seed("aiwc/memo/JUPITER.json")["chain"]
    parties = committed.execute(
        "SELECT value FROM fact WHERE subject_id = 'jupiter' AND field = 'chain_parties'"
    ).fetchone()["value"]
    assert len(parties) == sum(1 for r in memo if r["basis"] != "presumed")


def test_obligations_are_deduplicated_across_sources(loaded, committed, seed):
    chain = json.loads(
        (common.seed_dir() / "parallax-credit-marks.html").read_text(encoding="utf-8")
        .split('<script id="aichain" type="application/json">', 1)[1].split("</script>", 1)[0]
    )
    vantage = [e for e in chain["edges"] if e["sheet"] == "VANTAGE_ORCL" and e["tenant"] == "oracle"]
    assert len(vantage) == 2  # Frontier and Port Washington: one Oracle -> Vantage lease edge
    edges = committed.execute(
        "SELECT * FROM obligation WHERE from_entity_id = 'oracle' AND type = 'Lease' AND to_entity_id = %s",
        ("vantage-frontier-port-washington",),
    ).fetchall()
    assert len(edges) == 1 and edges[0]["site_slug"] is None
    sites = committed.execute(
        """SELECT value FROM fact WHERE subject_type = 'obligation' AND subject_id = %s AND field = 'linked_sites'
           AND tag <> 'M'""",
        (edges[0]["id"],),
    ).fetchall()
    assert {tuple(r["value"]) for r in sites} == {("frontier", "lighthouse")}
    ids = committed.execute("SELECT id FROM obligation").fetchall()
    assert len({r["id"] for r in ids}) == len(ids)


def test_contract_terms_on_the_obligation(loaded, committed, seed):
    ctr = next(r for r in seed("aiwc/q3x/ctr.json")["rows"] if r["id"] == "IREN_CHILDRESS")
    facts = {
        r["field"]: r
        for r in committed.execute(
            """SELECT * FROM fact WHERE subject_type = 'obligation'
               AND subject_id = 'microsoft--iren-ltd--compute-purchase' AND document_id IN
               (SELECT id FROM document WHERE url LIKE '%%aiwc/q3x/ctr.json')"""
        )
    }
    assert facts["total_usd"]["value"] == ctr["tcv_m"] * 1_000_000 and facts["total_usd"]["unit"] == "USD"
    assert facts["term_years"]["value"] == ctr["term"]
    assert facts["status"]["tag"] == "M" and facts["status"]["value"] == "Started"


def test_edge_map_keywords_match_the_archive(seed):
    from marden.seed.sites_edges import EDGE_MAP

    html = (common.seed_dir() / "parallax-credit-marks.html").read_text(encoding="utf-8")
    block = html.split('<script id="aichain" type="application/json">', 1)[1].split("</script>", 1)[0]
    edges = {(e["sheet"], e["cp"]): e for e in json.loads(block)["edges"]}
    cases = {s["case"] for s in json.loads((EDGE_MAP.parent / "site_map.json").read_text())["sites"]}
    for link in json.loads(EDGE_MAP.read_text())["edges"]:
        assert link["case"] in cases
        assert link["keyword"].lower() in edges[(link["sheet"], link["cp"])]["project"].lower()


def test_power_source_and_party_splits_on_real_text(seed):
    from marden.seed.sites_entities import split_parties
    from marden.seed.sites_rows import power_source

    projects = seed("aiwc/gpu/site_power.json")["projects"]
    got = {c: power_source(projects[c]["site_power"].get("supply"), projects[c]["site_power"].get(
        "onsite_generation"), projects[c]["site_power"].get("contract")) for c in ("JUPITER", "ABILENE", "APLD_PF1")}
    assert got == {"JUPITER": "On-site gas", "ABILENE": "Hybrid", "APLD_PF1": "Grid"}
    lender = next(r for r in seed("aiwc/memo/VANTAGE_TX.json")["chain"] if r["role"] == "Lender")
    assert split_parties(lender["name"], "Lender") == [p.strip() for p in lender["name"].split(",")]
