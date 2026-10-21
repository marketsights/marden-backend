"""The `cases` seed loader against a slice of the real archive (MARDEN_SEED_DIR).

The slice: the JUPITER, APLD_PF1 and CRWV_DDTL3 cases (a construction loan with a press dealer
quote, a 144A project bond whose legacy price is a TRACE last sale, and a GPU-backed DDTL), their
sites from the real `sites` loader and their instruments from the real `instruments` loader. The
quarantine rules are checked on real archive text. Every expected value is read from the archive
files themselves; nothing is made up.
"""

from __future__ import annotations

import os
import re
from datetime import date

import pytest

from marden.core import config
from marden.seed import common

pytestmark = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")

CASES = ["JUPITER", "APLD_PF1", "CRWV_DDTL3"]
INSTRUMENTS = {"EX-JUPITER-190", "EX-APLD-010", "EX-CRWV-094", "EX-CRWV-091", "EX-CRWV-098"}
BLOCKED = re.compile(r"\bTRACE\b|FINRA|[Ll]ast sale|B[öo]rse Frankfurt")


@pytest.fixture(scope="module")
def seed():
    config.reload_settings()
    common.seed_dir.cache_clear()
    return common.load_json


@pytest.fixture
def loaded(seed, committed):
    from marden.seed.cases import load
    from marden.seed.instruments import load as load_instruments
    from marden.seed.sites import load as load_sites

    load_sites(CASES)
    load_instruments(only=INSTRUMENTS)
    prices_before = committed.execute("SELECT count(*) AS n FROM price_obs").fetchone()["n"]
    stats = load(CASES)
    stats["prices_before"] = prices_before
    return stats


def _facts(c, st, sid, field):
    return c.execute(
        "SELECT * FROM fact WHERE producer = 'seed:cases' AND subject_type = %s AND subject_id = %s AND field = %s",
        (st, sid, field)).fetchall()


def _counts(c) -> dict[str, int]:
    one = lambda sql: c.execute(sql).fetchone()["n"]  # noqa: E731
    return {"fact": one("SELECT count(*) AS n FROM fact WHERE producer = 'seed:cases'"),
            "model_output": one("SELECT count(*) AS n FROM model_output"),
            "model_run": one("SELECT count(*) AS n FROM model_run"),
            "event": one("SELECT count(*) AS n FROM event"), "entity": one("SELECT count(*) AS n FROM entity"),
            "document": one("SELECT count(*) AS n FROM document"),
            "price_obs": one("SELECT count(*) AS n FROM price_obs")}


# ---------------------------------------------------------------- quarantine rules on real text


def test_scrub_removes_trace_sentences_from_real_verdicts(seed):
    from marden.seed.cases_quarantine import drop_blocked, scrub_text

    verdict = next(c for c in seed("aiwcredit.json")["cases"] if c["id"] == "APLD_PF1")["verdict"]
    assert "TRACE last sale" in verdict
    clean = scrub_text(verdict)
    assert clean and "TRACE" not in clean and " bp " not in clean and "yields" not in clean
    assert clean.startswith("The model puts the odds of a loss on this debt at 5% over its life")
    oa_rating = next(r["rating"] for r in seed("aiwc/q3x/oa.json")["rows"] if "FINRA" in (r.get("rating") or ""))
    assert drop_blocked({"rating": oa_rating, "share": 0.22}) == {"share": 0.22}


# ---------------------------------------------------------------- loads into the test database


def test_second_run_writes_nothing_new_and_no_price(loaded, committed):
    from marden.seed.cases import load

    first = _counts(committed)
    assert first["price_obs"] == loaded["prices_before"]  # the loader writes no price at all
    again = load(CASES)
    assert _counts(committed) == first
    assert again["facts_new"] == 0 and again["model_outputs_new"] == 0 and again["events_new"] == 0
    assert again["facts_seen"] == loaded["facts_seen"] and loaded["facts_new"] > 0
    runs = committed.execute(
        "SELECT ok, meta->>'loader' AS loader, items_new FROM source_run ORDER BY id").fetchall()
    cases_runs = [r for r in runs if r["loader"] == "cases"]
    assert [r["ok"] for r in cases_runs] == [True, True] and cases_runs[1]["items_new"] == 0
    leaks = committed.execute(
        "SELECT field, value::text AS v, quote FROM fact WHERE producer = 'seed:cases'").fetchall()
    assert not [r for r in leaks if BLOCKED.search(r["v"]) or BLOCKED.search(r["quote"] or "")]
    outputs = committed.execute("SELECT output::text AS o FROM model_output").fetchall()
    assert not [o for o in outputs if BLOCKED.search(o["o"]) or re.search(r'"(px|price|yield_pct|spread_bp)"', o["o"])]


def test_legacy_model_output_is_not_a_price(loaded, committed, seed):
    out, params = seed("aiwc/out/APLD_PF1.json"), seed("aiwc/params/APLD_PF1.json")
    assert params["debt"]["price_kind"] == "TRACE last sale"
    rows = committed.execute(
        """SELECT * FROM model_output WHERE model = 'legacy_credit_sim'
           AND subject_id IN ('polaris-forge-1', 'EX-APLD-010')""").fetchall()
    assert {r["subject_type"] for r in rows} == {"site", "instrument"}
    for r in rows:
        assert r["version"] == "legacy@2026-10-20" and r["as_of"] == date(2026, 10, 17)
        o = r["output"]
        assert o["outputs"]["p_loss"] == out["p_loss"] and o["outputs"]["px_200"] == out["px_200"]
        assert "price" not in o["outputs"] and "yield_pct" not in o["outputs"] and "spread_bp" not in o["outputs"]
        assert "price" not in o["inputs"]["debt"] and "spread_bp" not in o["inputs"].get("tenant", {})
        assert {"out.price", "out.yield_pct", "params.debt.price"} <= set(o["quarantined"])
        assert all(i["basis"] != "market" for i in o["buildup"]["inputs"])


def test_memo_structure_terms_and_dossier_evidence_keep_their_sources(loaded, committed, seed):
    memo = seed("aiwc/memo/JUPITER.json")
    terms = {f["value"]["key"]: f for f in _facts(committed, "site", "jupiter", "structure_term")}
    assert "Fund mark" not in terms  # a fund mark is a price: the holdings loader owns it
    for s in memo["security"]:
        if s["basis"] == "disclosed" and s.get("q") and s["k"] != "Fund mark":
            f = terms[s["k"]]
            assert f["tag"] == "F" and f["source_url"] == s["src"] and f["quote"] == " ".join(s["q"].split())
    not_disclosed = [s["k"] for s in memo["security"] if s["basis"] == "not disclosed"]
    open_q = _facts(committed, "site", "jupiter", "open_questions")[0]["value"]
    assert len(open_q) == len(memo["unknowns"]) + len(not_disclosed)
    dossier = seed("aiwc/cases/JUPITER.json")
    evidence = _facts(committed, "site", "jupiter", "risk_evidence")
    want = [e for r in dossier["risks"].values() for e in r.get("evidence") or []]
    assert len(evidence) == len(want)
    ferc = [f for f in evidence if "elibrary.ferc.gov" in (f["source_url"] or "")]
    assert ferc and all(f["tag"] == "F" and f["valid_from"] == date.fromisoformat(f["value"]["date"]) for f in ferc)
    catalysts = committed.execute(
        """SELECT event_date, kind FROM event
           WHERE subject_type = 'site' AND subject_id = 'jupiter' AND kind = 'catalyst'""").fetchall()
    dated = {i["next"]["date"] for i in memo["issues"] if (i.get("next") or {}).get("date")}
    assert {c["event_date"].isoformat() for c in catalysts} == dated


def test_gpu_collateral_triggers_and_comps(loaded, committed, seed):
    uw = next(r for r in seed("aiwc/q3x/underwater.json")["loans"] if r["ids"] == ["EX-CRWV-094"])
    col = _facts(committed, "instrument", "EX-CRWV-094", "gpu_collateral")[0]
    assert col["tag"] == "M" and col["value"]["ltv_pct"] == uw["ltv"] and col["value"]["chip"] == uw["chip"]
    trg = seed("aiwc/q3x/trg.json")
    rows = [r for r in trg["rows"] if "EX-CRWV-094" in r["ids"]]
    tests = _facts(committed, "instrument", "EX-CRWV-094", "covenant_test")
    assert sorted(f["quote"] for f in tests) == sorted(" ".join(r["q"]["quote"].split()) for r in rows)
    terms = next(t for t in trg["terms"] if t["fac"] == "CoreWeave DDTL 3.0")
    assert len(_facts(committed, "instrument", "EX-CRWV-094", "agreement_term")) == len(terms["terms"])
    comps = {r["fid"]: r for r in seed("aiwc/q3x/comp.json")["rows"]}
    floating = next(r for r in comps.values() if r["bench"] == "SOFR" and r["bp"] is not None)
    fixed = next(r for r in comps.values() if r["bench"] == "UST" and r["bp"] is not None)
    for row, tag in ((floating, None), (fixed, "M")):
        f = _facts(committed, "series", f"issue_comp:{row['fid']}", "issue_spread_bp")[0]
        assert f["value"] == row["bp"] and f["valid_from"] == date.fromisoformat(row["close"])
        assert f["tag"] == (tag or common.tag_for_url(row["docs"][0]["u"]))
    traded = next(r for r in comps.values() if r.get("now"))
    terms_fact = _facts(committed, "series", f"issue_comp:{traded['fid']}", "deal_terms")[0]
    assert "now" not in terms_fact["value"] and str(traded["now"]["px"]) not in str(terms_fact["value"])
    licensed = committed.execute(
        "SELECT count(*) AS n FROM fact WHERE producer = 'seed:cases' AND source_url LIKE '%silicondata.com%'"
    ).fetchone()["n"]
    assert licensed == 0
    lists = committed.execute(
        "SELECT count(*) AS n FROM fact WHERE producer = 'seed:cases' AND field = 'usd_per_gpu_hr'").fetchone()["n"]
    assert lists == len(seed("aiwcredit.json")["gpu"]["lists"])
