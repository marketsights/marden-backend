"""Materiality (00-global "Materiality and alerts") on the archive's own events, plus tiny cases for the
projection and cushion thresholds."""

from __future__ import annotations

import os
import re

import pytest

from marden.core import config
from marden.models.materiality import RULE_CODES, is_material, rule_code_table
from marden.seed import common

needs_seed = pytest.mark.skipif(not os.environ.get("MARDEN_SEED_DIR"), reason="MARDEN_SEED_DIR is not set")


@pytest.fixture(scope="module")
def seed():
    config.reload_settings()
    common.seed_dir.cache_clear()
    return common.load_json


def test_projection_and_cushion_thresholds():
    event = {"title": "milestone filing"}
    assert not is_material(event, {"p50": "2027-06"}, {"p50": "2027-08"})
    slip = is_material(event, {"p50": "2027-06"}, {"p50": "2027-09"})
    assert slip and slip.codes == ["M1"] and slip.severity == "amber" and slip.alert_types == ["milestone"]
    negative = is_material(event, {"cushion_months": 1}, {"cushion_months": -1})
    assert negative.codes == ["M2"] and negative.severity == "red"
    assert not is_material(event, {"cushion_months": -2}, {"cushion_months": -3})  # already negative
    assert {"M1", "M2", "M3", "M4", "M5", "T2", "L2"} <= {row["code"] for row in rule_code_table()}
    assert all(sev in ("amber", "red") for _, _, sev in RULE_CODES.values())


@needs_seed
def test_archive_events(seed):
    events = seed("aiwc/state/events.json")
    # Headlines that report the notice itself ('files', 'invokes', 'issues', '... notice') come with a
    # tenant notice; commentary ('... warning lands as AI power gets scarce') does not by itself.
    jupiter_fm = [e for e in events if e["case"] == "JUPITER" and "force majeure" in e["t"].lower()
                  and re.search(r"\b(files|invok\w*|issues|notice)\b", e["t"], re.I)]
    assert len(jupiter_fm) >= 5
    for e in jupiter_fm:
        assert "M4" in is_material({"title": e["t"], "kind": e["kind"]}).codes, e["t"]
    # The archive's routine items (sev 0, no rule label) are not material by these rules either.
    routine = [e for e in events if e.get("sev") == 0 and not e.get("cls")]
    flagged = [e["t"] for e in routine if is_material({"title": e["t"], "kind": e["kind"]})]
    assert len(flagged) <= len(routine) * 0.02, flagged
    # Docket procedure ('Request for Waiver of ... Period', motions) is not a lender action or a power denial.
    items = seed("dockets/out/FERC_CP26-80.json")["items"]
    waivers = [i["t"] for i in items if "Waiver" in i["t"]]
    assert waivers and not any(is_material({"title": t, "kind": "docket"}) for t in waivers)


@needs_seed
def test_power_feed_withdrawals(seed):
    """power.json queue items that withdraw a generator are power withdrawals (M3)."""
    feed = seed("power.json")["feed"]
    withdrawn = [f for f in feed if str(f.get("t") or f.get("title") or "").upper().startswith("WITHDRAWN")]
    for f in withdrawn:
        text = f.get("t") or f.get("title")
        assert "M3" in is_material({"title": text, "kind": "queue"}).codes, text


@needs_seed
def test_scheduled_catalysts_are_not_events(seed):
    """q3x/cat.json lists scheduled steps ('CoreWeave Q3 10-Q: leverage covenant recomputed: SEC deadline for
    a large accelerated filer ...'); a step that has not happened is not material by its text."""
    rows = seed("aiwc/q3x/cat.json")["rows"]
    titles = [f"{r['ev']}: {r['evl']}" if r.get("evl") else r["ev"] for r in rows if r.get("ev")]
    filer = [t for t in titles if "accelerated filer" in t]
    assert filer
    assert not any(is_material({"title": t, "kind": "catalyst"}) for t in titles)
    # the same words as a happened event: an SEC filer status is not a lender's acceleration
    assert not any("M5" in is_material({"title": t, "kind": "news"}).codes for t in filer)
    # a catalyst is material only through the states around it
    assert is_material({"title": filer[0], "kind": "catalyst"}, {"cushion_months": 2}, {"cushion_months": -1})


@needs_seed
def test_8k_item_captions_are_not_contract_cuts(seed):
    """The archive's 8-K extracts: 'Item 1.02 Termination of a Material Definitive Agreement' (usually with
    'Item 1.01 Entry into ...': a refinancing) is a filing caption, not a tenant notice."""
    extracts = [r for r in seed("aiwc/q3x/fac/dl/k8_extract.json") if "1.02" in (r.get("items") or "")]
    captions = []
    for r in extracts:
        found = re.findall(r"Item 1\.0[12]\.?\s+((?:Entry into|Termination of) an? Material Definitive Agreement)",
                           r.get("txt") or "")
        if found:
            captions.append("8-K: " + "; ".join(dict.fromkeys(found)))
    assert captions
    assert not any("M4" in is_material({"title": c, "kind": "filing"}).codes for c in captions)
