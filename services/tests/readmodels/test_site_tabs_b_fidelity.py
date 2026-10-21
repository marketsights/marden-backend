"""Site detail Material events, Debt and holders, Lease terms and Buildings tabs as the reference
screens show them (spec 04 / 16; screens 04e-04h), on real seeded rows copied from the local `marden`
database (site_rows).

- Material events: every change of a link's status between two `status` runs is a row (link, rule
  codes, 'Green → amber'), every material cushion move is a row (M1 / M2, 'Cushion +4 → −1 months'),
  and records are one row per story, later press coverage of the same rule counted on the first report.
- Debt: the recovery card shows the recovery model's waterfall inputs; the stack's equity is a row of
  the instruments table (first loss).
- Lease: 'Force majeure accepted' is one of the outcomes; a lease with no filed term says it is not public.
- Buildings: the totals are the header's count, from the campus facts while no building record exists.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import pytest
from real_rows import copied_rows
from site_rows import copy_sites

from marden.core import db
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import sites as _sites  # noqa: F401 (registers builders)
from marden.readmodels.views import sites_debt, sites_detail, sites_power, sites_tabs  # noqa: F401
from marden.readmodels.views.sites_tabs import LINK_LABEL, rule_words, signed, story_key

WORD = {"red": "red", "amber": "amber", "green": "green", "none": "no data"}


@pytest.fixture(scope="module")
def sites() -> list[str]:
    return copy_sites()


def build(view: str, key: str) -> Any:
    b = get_builder(view)
    assert b is not None, view
    with building(business_today()), db.transaction() as conn:
        return b.build_one(conn, key)


def outputs(model: str, subject_type: str, slug: str) -> list[dict[str, Any]]:
    """The last output per day since Jan 1 of the build year, oldest first (as output_history)."""
    start = date(business_today().year, 1, 1)
    return copied_rows(
        """SELECT DISTINCT ON (as_of) as_of, output FROM model_output WHERE model = %s AND subject_type = %s
             AND subject_id = %s AND as_of BETWEEN %s AND %s ORDER BY as_of, created_at DESC, id DESC""",
        (model, subject_type, slug, start, business_today()),
    )


def test_rule_words_and_signed_months():
    assert rule_words("sev 3: Force majeure") == "Force majeure"
    assert rule_words("order denying") == "Order denying"
    assert rule_words(None) is None
    assert signed(4) == "+4" and signed(-1) == "−1" and signed(0) == "0"


def test_every_status_change_is_a_row_with_its_rule_and_change(sites):
    seen = 0
    for slug in sites:
        log = build("site_tab:events", slug)["events"]
        history = outputs("status", "site", slug)
        for prev, cur in zip(history, history[1:], strict=False):
            before = {link["role"]: link for link in prev["output"].get("chain") or []}
            for link in cur["output"].get("chain") or []:
                was, now = (before.get(link["role"]) or {}).get("status"), link.get("status")
                if link["role"] not in LINK_LABEL or was not in WORD or now not in WORD or was == now:
                    continue
                seen += 1
                prefix = f"{WORD[was].capitalize()} → {WORD[now]}"
                rows = [e for e in log if e["link"] == LINK_LABEL[link["role"]] and (e["changed"] or "").startswith(prefix)
                        and cur["as_of"].isoformat() >= e["date"] > prev["as_of"].isoformat()
                        or e["link"] == LINK_LABEL[link["role"]] and e["date"] == cur["as_of"].isoformat()
                        and (e["changed"] or "").startswith(prefix)]
                assert rows, (slug, link["role"], prefix, cur["as_of"])
                codes = re.findall(r"(?:^|[.;]\s+)([EDTLM][12])\b", link.get("reason") or "")
                assert rows[0]["ruleHit"] == (", ".join(dict.fromkeys(codes)) or None), (slug, rows[0])
                assert rows[0]["severity"] == now
                fact = link.get("fact")
                if fact and fact.get("value"):
                    assert rows[0]["tag"] == fact["tag"] and rows[0]["sourceName"] == (fact.get("sourceName") or "")
    if copied_rows("SELECT 1 FROM model_output WHERE model = 'status' LIMIT 1"):
        assert seen > 0
    # Jupiter's tenant broke the chain: the row names T2 and the chain's own change.
    if "jupiter" in sites:
        broke = [e for e in build("site_tab:events", "jupiter")["events"] if "chain Strained → Broken" in
                 (e["changed"] or "")]
        assert len(broke) <= 1
        if broke:
            assert broke[0]["severity"] == "red" and broke[0]["link"] == "Tenant" and "T2" in broke[0]["ruleHit"]


def test_material_cushion_moves_are_rows(sites):
    for slug in sites:
        log = build("site_tab:events", slug)["events"]
        rows = [e for e in log if (e["changed"] or "").startswith("Cushion ")]
        expected = []
        previous = None
        for out in outputs("cushion", "site", slug):
            months = ((out["output"].get("projection") or {}).get("cushionMonths") or {}).get("value")
            if not isinstance(months, int | float):
                continue
            if previous is not None and months != previous and (months - previous <= -3 or months < 0 <= previous):
                expected.append((out["as_of"].isoformat(), f"Cushion {signed(previous)} → {signed(months)} months"))
            previous = months
        assert sorted((r["date"], r["changed"]) for r in rows) == sorted(expected), slug
        for r in rows:
            assert r["link"] == "Schedule" and set(r["ruleHit"].split(", ")) <= {"M1", "M2"}
            assert r["tag"] in ("F", "R", "M") and r["text"]


def test_records_are_one_row_per_story_with_later_coverage_counted(sites):
    for slug in sites:
        tab = build("site_tab:events", slug)
        log = tab["events"]
        assert [e["date"] for e in log] == sorted((e["date"] for e in log), reverse=True), slug
        records = [e for e in log if not (e["changed"] or "")]
        keys = [story_key(e["text"]) for e in records]
        assert len(keys) == len(set(keys)), slug
        news = sorted((e for e in records if e["link"] != "Permits and power" and e["ruleHit"]
                       and "Google News" in e["sourceName"]), key=lambda e: e["date"])
        for a, b in zip(news, news[1:], strict=False):
            if a["ruleHit"] == b["ruleHit"]:
                assert date.fromisoformat(a["date"]) + timedelta(days=30) < date.fromisoformat(b["date"]), (slug, a, b)
        for e in log:
            assert not re.match(r"^sev\s*\d", e["ruleHit"] or ""), (slug, e["ruleHit"])
            assert e.get("reports", 1) >= 1
        assert build("site_detail", slug)["tabCounts"]["events"] == len(log), slug


def test_recovery_card_reads_the_recovery_model(sites):
    for slug in sites:
        tab = build("site_tab:debt", slug)
        if tab is None:
            continue
        for item in tab["recovery"]["inputs"]:
            assert item["value"]["value"] is not None or item.get("pending"), (slug, item)
            assert item.get("pending") != "recovery model" or item["value"]["value"] is None
    if "jupiter" not in sites:
        return
    main = build("site_detail", "jupiter")["debtExposed"]
    out = copied_rows("SELECT output FROM model_output WHERE model = 'recovery' AND subject_id = %s "
                      "AND as_of <= %s ORDER BY as_of DESC, created_at DESC LIMIT 1",
                      (main["instrumentId"], business_today()))
    if not out or out[0]["output"].get("method") != "powered_shell":
        pytest.skip("no powered-shell recovery output for Jupiter's loan")
    basis = out[0]["output"]["basis"]
    inputs = {i["label"]: i["value"] for i in build("site_tab:debt", "jupiter")["recovery"]["inputs"]}
    if isinstance(basis.get("compMedianUsdPerMw"), int | float):
        assert inputs["Value per MW of comparable sites"]["value"].startswith(
            f"${basis['compMedianUsdPerMw'] / 1e6:,.1f}mm per gross MW, median of {len(basis['comps'])} sites")
    if isinstance(basis.get("seniorClaimsB"), int | float):
        assert inputs["Claims ahead of the loan"]["value"].endswith(f"${basis['pariClaimsB']:,.1f}B at the same rank")
        assert inputs["Claims ahead of the loan"]["tag"] == "M"


def test_equity_of_the_stack_is_a_first_loss_row(sites):
    for slug in sites:
        tab = build("site_tab:debt", slug)
        if tab is None:
            continue
        equity = [p for p in tab["capitalStack"] if p["kind"] == "equity"]
        rows = [i for i in tab["instruments"] if i["recovery"] == "first loss"]
        assert [(r["name"], r["sizeB"]) for r in rows] == [(p["label"], p["amountB"]) for p in equity], slug
        for r in rows:
            assert r["instrumentId"] is None and r["quote"] is None and r["mardenPrice"] is None


def test_lease_outcomes_name_force_majeure_and_say_when_the_lease_is_not_public(sites):
    for slug in sites:
        tab = build("site_tab:lease", slug)
        if tab is None:
            continue
        fm = next(t for t in tab["terms"] if t["key"] == "force_majeure")
        outcome = tab["outcomes"][-1]
        assert outcome["label"] == "Force majeure accepted"
        if fm["value"]["value"] is None:
            assert "value" not in outcome and "[from the lease]" in outcome["text"]
        else:
            assert outcome["value"] == fm["value"]
        filed = any(t["value"]["value"] is not None and t["value"]["tag"] == "F" for t in tab["terms"])
        assert ("note" in tab) is (not filed), slug


def test_buildings_totals_are_the_header_count(sites):
    for slug in sites:
        tab = build("site_tab:buildings", slug)
        if tab is None:
            continue
        header = build("site_detail", slug)["buildings"]
        summary = tab.get("summary")
        if header is None:
            assert summary is None, slug
            continue
        assert summary["planned"]["value"]["value"] == header["value"]["total"], slug
        assert summary["energized"]["value"]["value"] == header["value"]["built"], slug
        assert summary["progress"]["value"]["value"] is None
    if "jupiter" in sites and not copied_rows("SELECT 1 FROM building WHERE campus_id = 'project-jupiter' LIMIT 1"):
        fact = copied_rows("SELECT value, source_name FROM fact_current WHERE subject_type = 'campus' "
                           "AND subject_id = 'project-jupiter' AND field = 'building_count'")
        planned = build("site_tab:buildings", "jupiter")["summary"]["planned"]
        if fact:
            assert planned["value"]["value"] == fact[0]["value"]
            assert planned["value"]["tag"] == "R"
