"""Instrument builders (instruments, instrument_detail, curve, gpu_collateral) on real seeded rows.

Rows are copied from the local `marden` database (real_rows.copy_instruments): the named V1 subjects
Jupiter, The Barn, Frontier, Abilene, CoreWeave DDTL 3.0 and 5.0, CoreWeave 9% 2031 and an Oracle note,
plus one non-V1 instrument. Expectations are computed from the copied rows, so the tests follow the
data as collectors add to it. Model outputs are used when the source database has them; until the
models run, the tests check that the documents show n/d (null), never a placeholder.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest
from real_rows import FUND_FILINGS, copied_rows, copy_instruments
from ts_shapes import assert_matches_types

from marden.readmodels import business_today, mark_dirty_for, rebuild_all
from marden.readmodels.views import curve, gpu_collateral, instrument_detail, instrument_text, instruments  # noqa: F401 (builders)
from marden.readmodels.views.instrument_text import DASH

JUPITER, BARN, FRONTIER, ABILENE = "EX-JUPITER-190", "EX-SALINE-308", "EX-VANTAGE_ORCL-366", "EX-ABILENE-000"
DDTL3, DDTL5, CRWV31, ORACLE = "EX-CRWV-094", "EX-CRWV-096", "EX-CRWV-102", "EX-ORCL-239"
NOT_V1 = "EX-VANTAGE_ORCL-367"
SEEDED = (JUPITER, BARN, FRONTIER, ABILENE, DDTL3, DDTL5, CRWV31, ORACLE, NOT_V1)
ROW_KEYS = {"id", "name", "type", "sizeB", "matures", "mardenPrice", "yieldPct", "ytw", "ytm", "change1w", "markGap",
            "chainStatus"}
DETAIL_KEYS = ROW_KEYS | {
    "facts",
    "fundMarks",
    "nextMarksPublic",
    "recovery",
    "priceHistory",
    "yields",
    "holders",
    "stress",
    "terms",
    "payers",
    "marketCheck",
}
ND_ROW = ("chainStatus",)
ND_DETAIL = ("chainStatus", "fundMarks", "nextMarksPublic")


@pytest.fixture
def seeded() -> list[str]:
    return copy_instruments(SEEDED)


def _doc(view: str, key: str, as_of=None) -> Any:
    rows = copied_rows(
        "SELECT doc FROM read_model WHERE view = %s AND key = %s AND as_of = %s", (view, key, as_of or business_today())
    )
    return rows[0]["doc"] if rows else None


def _fact(instrument_id: str, field: str) -> dict | None:
    rows = copied_rows(
        "SELECT * FROM fact_current WHERE subject_type = 'instrument' AND subject_id = %s AND field = %s",
        (instrument_id, field),
    )
    return rows[0] if rows else None


def _output(model: str, subject_id: str) -> dict | None:
    rows = copied_rows(
        "SELECT * FROM model_output WHERE model = %s AND subject_id = %s ORDER BY as_of DESC, id DESC LIMIT 1",
        (model, subject_id),
    )
    return rows[0] if rows else None


def _need(ids: list[str], *wanted: str) -> None:
    missing = [w for w in wanted if w not in ids]
    if missing:
        pytest.skip(f"not in the source database: {missing}")


def test_instruments_lists_v1_rows_from_facts(seeded):
    _need(seeded, JUPITER, DDTL3, ORACLE)
    assert rebuild_all("instruments").built == 1
    rows = {r["id"]: r for r in _doc("instruments", "all")}
    v1 = {r["id"] for r in copied_rows("SELECT id FROM instrument WHERE in_v1")}
    assert set(rows) == v1 and NOT_V1 not in rows
    assert all(set(r) == ROW_KEYS for r in rows.values())

    jupiter, size = rows[JUPITER], _fact(JUPITER, "amount_usd")
    assert jupiter["sizeB"]["value"] == round(float(size["value"]) / 1e9, 4)
    assert (jupiter["sizeB"]["tag"], jupiter["sizeB"].get("sourceUrl")) == (size["tag"], size["source_url"])
    assert jupiter["matures"] == _fact(JUPITER, "maturity")["value"]
    assert jupiter["type"] == "Construction loan" and rows[DDTL3]["type"] == "GPU delayed-draw facility"
    assert rows[ORACLE]["type"].startswith("Tenant credit")
    assert not any("," in r["type"] for r in rows.values())  # the API reads ?type=a,b as a list
    assert rows[DDTL3]["name"] == "CoreWeave DDTL 3.0"
    if _output("price", JUPITER) is None and not copied_rows(
        "SELECT 1 FROM price_obs WHERE instrument_id = %s AND kind = 'marden'", (JUPITER,)
    ):
        assert jupiter["mardenPrice"]["value"] is None and jupiter["mardenPrice"]["tag"] == "M"
        assert jupiter["change1w"]["value"] is None
    if _output("mark_gap", JUPITER) is None and not copied_rows(
        "SELECT 1 FROM holding WHERE instrument_id = %s", (JUPITER,)
    ):
        assert jupiter["markGap"] == "no fund"
    status = _output("status", "jupiter")
    assert jupiter["chainStatus"] == (status["output"]["status"] if status else None)
    order = [r["id"] for r in _doc("instruments", "all")]
    assert order.index(JUPITER) < order.index(DDTL3) < order.index(ORACLE)  # spec 00a segment order


def test_detail_terms_dealer_quote_recovery_and_payers(seeded):
    _need(seeded, JUPITER)
    rebuild_all("instrument_detail")
    doc = _doc("instrument_detail", JUPITER)
    assert set(doc) == DETAIL_KEYS
    coupon = _fact(JUPITER, "coupon")
    assert doc["terms"]["Coupon"] == {
        "value": coupon["value"],
        "tag": coupon["tag"],
        "asOf": coupon["valid_from"].isoformat(),
        "sourceUrl": coupon["source_url"],
        "sourceName": coupon["source_name"],
    }
    assert doc["terms"]["Size"]["value"] == "$18.0B" and "$18.0B" in doc["facts"]
    quote = copied_rows(
        """SELECT * FROM price_obs WHERE instrument_id = %s AND kind = 'dealer'
                           ORDER BY obs_date DESC, id DESC LIMIT 1""",
        (JUPITER,),
    )
    if quote:
        q = quote[0]
        assert doc["marketCheck"]["Dealer quote"] == {
            "value": f"{float(q['price']):.1f}",
            "tag": q["tag"],
            "asOf": q["obs_date"].isoformat(),
            "sourceUrl": q["source_url"],
            "sourceName": q["source_name"],
        }
    rec = _fact(JUPITER, "recovery_project")
    if (
        _output("recovery", JUPITER) is None
        and _output("price", JUPITER) is None
        and rec
        and not _fact(JUPITER, "recovery_waterfall")
    ):
        v = rec["value"]
        assert doc["recovery"]["value"] == f"{v['base_pct']}c ({v['low_pct']}{DASH}{v['high_pct']}c)"
        assert (doc["recovery"]["tag"], doc["recovery"]["methodVersion"]) == ("M", rec["method_version"])
    share = _fact(JUPITER, "end_user_share")
    if share:
        assert doc["payers"] == [
            {
                "name": share["value"]["end_user"],
                "share": {
                    "value": share["value"]["share"],
                    "tag": "M",
                    "asOf": share["valid_from"].isoformat(),
                    "sourceName": share["source_name"],
                    "methodVersion": share["method_version"],
                },
            }
        ]
    if not copied_rows("SELECT 1 FROM price_obs WHERE instrument_id = %s AND kind = 'marden'", (JUPITER,)):
        assert doc["priceHistory"] == [] and instrument_detail.price_event(JUPITER, doc) is None
    if _output("stress", JUPITER) is None:
        assert doc["stress"] == []


@pytest.mark.parametrize("instrument_id", [BARN, ORACLE, CRWV31])
def test_detail_holders_and_fund_marks_from_filings(seeded, instrument_id):
    _need(seeded, instrument_id)
    rebuild_all("instrument_detail")
    doc = _doc("instrument_detail", instrument_id)
    lines = copied_rows(
        """WITH newest AS (SELECT fund_entity_id, max(period_end) AS p FROM holding
                            WHERE filing_type = ANY(%(f)s) GROUP BY fund_entity_id)
           SELECT h.* FROM holding h JOIN newest n ON n.fund_entity_id = h.fund_entity_id AND n.p = h.period_end
            WHERE h.instrument_id = %(id)s AND h.filing_type = ANY(%(f)s)""",
        {"id": instrument_id, "f": FUND_FILINGS},
    )
    if _output("mark_gap", instrument_id) is None:
        par = sum(float(r["par"]) for r in lines if r["par"] is not None)
        assert doc["holders"] == {
            "count": len({r["fund_entity_id"] for r in lines}),
            "parMm": round(par / 1e6, 3),
            "rows": [],
        }
        marked = [r for r in lines if r["mark_price"] is not None]
        if not marked:
            assert doc["fundMarks"] is None
        else:
            funds: dict = {}
            for r in marked:
                funds.setdefault(r["period_end"], set()).add(r["fund_entity_id"])
            when = max([d for d, f in funds.items() if len(f) >= 2] or funds)
            prices = [float(r["mark_price"]) for r in marked if r["period_end"] == when]
            assert doc["fundMarks"]["date"] == when.isoformat()
            assert (doc["fundMarks"]["low"], doc["fundMarks"]["high"]) == (round(min(prices), 4), round(max(prices), 4))
        assert doc["nextMarksPublic"] is None and doc["markGap"] is None
    medians = copied_rows(
        """SELECT * FROM price_obs WHERE instrument_id = %s AND kind = 'fund_mark' AND tag = 'F'
              AND source_name LIKE '%% median of %%' ORDER BY obs_date DESC""",
        (instrument_id,),
    )
    if medians:
        latest = [m for m in medians if m["obs_date"] == medians[0]["obs_date"]]
        assert doc["marketCheck"]["Fund marks (median)"]["value"] in {f"{float(m['price']):.1f}" for m in latest}
        assert doc["marketCheck"]["Fund marks (median)"]["tag"] == "F"


def test_fund_marks_span_every_holders_last_mark(seeded):
    """Funds' last marks = the range of the marks the mark gap averages (spec 07 Rules), not only the
    newest filing date's: the KPI strip, the chart and the holders table read the same funds."""
    _need(seeded, CRWV31)
    out = _output("mark_gap", CRWV31)
    rows = [r for r in (instrument_detail._holder(h) for h in instruments.gap_rows(out))] if out else []
    rows = [r for r in rows if r is not None]
    if not rows:
        pytest.skip("no mark_gap holder rows for CoreWeave 9% 2031 in the source database")
    rebuild_all("instrument_detail")
    doc = _doc("instrument_detail", CRWV31)
    marks = [r["lastMark"] for r in rows]
    assert (doc["fundMarks"]["low"], doc["fundMarks"]["high"]) == (round(min(marks), 4), round(max(marks), 4))
    assert doc["fundMarks"]["date"] == max(r["filingDate"] for r in rows)
    assert doc["fundMarks"]["filing"] == " and ".join(sorted({r["filing"] for r in rows}))
    # The par-weighted average mark behind the gap lies inside the range shown.
    gap, price = doc["markGap"], doc["mardenPrice"]["value"]
    if isinstance(gap, dict) and gap["value"] is not None and price is not None:
        assert doc["fundMarks"]["low"] - 1e-6 <= price - gap["value"] <= doc["fundMarks"]["high"] + 1e-6


def test_facts_line_and_coupon_carry_the_filed_format(seeded):
    _need(seeded, CRWV31)
    rebuild_all("instrument_detail")
    doc = _doc("instrument_detail", CRWV31)
    is_144a, coupon, kind = _fact(CRWV31, "is_144a"), _fact(CRWV31, "coupon"), _fact(CRWV31, "coupon_type")
    assert ("144A" in doc["facts"].split(" · ")) == bool(is_144a and is_144a["value"] is True)
    if coupon and kind and kind["value"] == "fixed" and "fixed" not in coupon["value"]:
        assert doc["terms"]["Coupon"]["value"] == f"{coupon['value']} fixed"
        assert f"{coupon['value']} fixed" in doc["facts"]
    maturity = _fact(CRWV31, "maturity")
    if maturity and re.fullmatch(r"\d{4}-\d{2}", maturity["value"]):
        words = instrument_text.date_words(maturity["value"])
        assert words != maturity["value"] and f"matures {words}" in doc["facts"]
        assert doc["terms"]["Maturity"]["value"] == words


def test_market_check_carries_the_traded_yield_three_months_back(seeded):
    """KPI strip: "Yield + level at a prior date" (spec 07) from the instrument's own TRACE history."""
    _need(seeded, CRWV31)
    rebuild_all("instrument_detail")
    doc = _doc("instrument_detail", CRWV31)
    rows = copied_rows(
        """SELECT * FROM price_obs WHERE instrument_id = %s AND kind = ANY(%s) AND yield_pct IS NOT NULL
            AND obs_date <= %s ORDER BY obs_date, obs_at NULLS FIRST, id""",
        (CRWV31, list(instruments.OBSERVED_KINDS), business_today()),
    )
    prior = [r for r in rows if rows and r["obs_date"] <= rows[-1]["obs_date"] - timedelta(days=91)]
    if not prior:
        assert instrument_detail.YIELD_BEFORE not in doc["marketCheck"]
        return
    got = doc["marketCheck"][instrument_detail.YIELD_BEFORE]
    assert got["value"] == f"{float(prior[-1]['yield_pct']):.1f}%"
    assert (got["asOf"], got["tag"]) == (prior[-1]["obs_date"].isoformat(), prior[-1]["tag"].strip())
    assert got["asOf"] < doc["yieldPct"]["asOf"]


def _worst_link(entity_id: str) -> str | None:
    """The entity's worst link across the chains it is a party of, from the copied status outputs."""
    rank = {"Broken": 0, "Strained": 1, "Holding": 2}
    link_to_status = {"red": "Broken", "amber": "Strained", "green": "Holding"}
    worst: str | None = None
    parties = copied_rows("SELECT site_slug, role FROM chain_party WHERE entity_id = %s", (entity_id,))
    for party in parties:
        out = copied_rows(
            """SELECT output FROM model_output WHERE model = 'status' AND subject_type = 'site' AND subject_id = %s
                AND as_of <= %s ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
            (party["site_slug"], business_today()),
        )
        for link in (out[0]["output"].get("chain") or []) if out else []:
            status = link_to_status.get(link.get("status"))
            if link.get("role") == party["role"] and status and (worst is None or rank[status] < rank[worst]):
                worst = status
    return worst


@pytest.mark.parametrize("instrument_id", [CRWV31, ORACLE])
def test_unsited_note_takes_its_issuers_link_status(seeded, instrument_id):
    """A corporate note finances no site: its Chain is its issuer's own worst link across the chains it
    is a party of (the status model's outputs, as the obligation map shows the company)."""
    _need(seeded, instrument_id)
    issuer = copied_rows("SELECT issuer_entity_id, site_slug FROM instrument WHERE id = %s", (instrument_id,))[0]
    assert issuer["site_slug"] is None
    worst = _worst_link(issuer["issuer_entity_id"])
    rebuild_all("instruments")
    rebuild_all("instrument_detail")
    row = next(r for r in _doc("instruments", "all") if r["id"] == instrument_id)
    assert row["chainStatus"] == worst
    assert _doc("instrument_detail", instrument_id)["chainStatus"] == worst


def test_spv_facility_takes_its_parents_link_status(seeded):
    """CoreWeave DDTL 5.0 is borrowed by a CoreWeave financing SPV that is no chain party itself and
    finances no site: it sits on its parent's chains, so its Chain is CoreWeave's own worst link."""
    _need(seeded, DDTL5)
    inst = copied_rows(
        """SELECT i.site_slug, i.issuer_entity_id, e.parent_id FROM instrument i
             JOIN entity e ON e.id = i.issuer_entity_id WHERE i.id = %s""",
        (DDTL5,),
    )[0]
    assert inst["site_slug"] is None and inst["parent_id"]
    expected = _worst_link(inst["issuer_entity_id"]) or _worst_link(inst["parent_id"])
    rebuild_all("instruments")
    rebuild_all("instrument_detail")
    row = next(r for r in _doc("instruments", "all") if r["id"] == DDTL5)
    assert row["chainStatus"] == expected
    assert _doc("instrument_detail", DDTL5)["chainStatus"] == expected


def _cds_input(instrument_id: str, keys: tuple[str, ...]) -> dict | None:
    out = _output("price", instrument_id)
    inputs = out["output"].get("inputs") if out and isinstance(out["output"], dict) else None
    items = {i["key"]: i for i in inputs or [] if isinstance(i, dict) and i.get("value") is not None}
    return next((items[k] for k in keys if k in items), None)


@pytest.mark.parametrize("instrument_id", [DDTL5, JUPITER, CRWV31])
def test_market_check_leads_with_the_named_cds_and_its_default_odds(seeded, instrument_id):
    """Market check (spec 07): issuer CDS, default odds implied by it and tenant CDS, named for the
    company (CoreWeave for a CoreWeave SPV's DDTL, Oracle as Jupiter's tenant), from the price model's
    bond-implied inputs; prices one decimal, yields one decimal with % (spec 00 Formats)."""
    _need(seeded, instrument_id)
    rebuild_all("instrument_detail")
    doc = _doc("instrument_detail", instrument_id)
    names = {r["id"]: r["name"] for r in copied_rows("SELECT id, name FROM entity")}
    expected: list[tuple[str, str]] = []
    issuer_keys, tenant_keys = instrument_detail.CDS_INPUTS
    for prefix, keys in (("", issuer_keys), ("", ("odds",)), ("Tenant ", tenant_keys)):
        if keys == ("odds",):
            odds = _cds_input(instrument_id, instrument_detail.ODDS_INPUTS)
            if odds:
                years = re.search(r"\((\d+(?:\.\d+)?)y", odds["label"])
                text = f"{float(odds['value']) * 100:.1f}%" + (f" over {years.group(1)} years" if years else "")
                expected.append(("Default odds implied by CDS", text))
            continue
        item = _cds_input(instrument_id, keys)
        if item:
            who = item["label"].removesuffix(" bond-implied CDS")
            expected.append((f"{prefix}{names.get(who, who)} CDS (bond-implied)", f"{float(item['value']):,.0f}bp"))
    if not expected:
        pytest.skip(f"no CDS input in the price model output for {instrument_id}")
    # JSONB keeps no key order (the screen orders the rows): check the rows, not their order.
    got = {k: v for k, v in doc["marketCheck"].items() if "CDS" in k}
    assert {k: v["value"] for k, v in got.items()} == {k: v for k, v in expected if "CDS" in k}
    if any(k.startswith("Default odds") for k, _ in expected):
        odds_key = "Default odds implied by CDS"
        assert doc["marketCheck"][odds_key]["value"] == dict(expected)[odds_key]
    assert all(v["tag"] == "M" and v["methodVersion"] for k, v in doc["marketCheck"].items() if "CDS" in k)
    assert "Issuer CDS (bond-implied)" not in doc["marketCheck"]
    for key, value in doc["marketCheck"].items():
        if key in ("Dealer quote", "Last trade", "Exchange quote", "Quote", "Fund marks (median)"):
            assert re.fullmatch(r"-?\d+\.\d( \(reported yield -?\d+\.\d%\))?", value["value"]), (key, value)
    if doc["yieldPct"] is not None:
        assert re.fullmatch(r"-?\d+\.\d%", doc["yieldPct"]["value"])


def test_date_words():
    assert instrument_text.date_words("2031-02") == "Feb 2031"
    assert instrument_text.date_words("2030-04-15") == "Apr 15, 2030"
    assert instrument_text.date_words("2030") == "2030"
    assert instrument_text.date_words("various") == "various"


def test_gpu_collateral_from_the_collateral_record(seeded):
    _need(seeded, DDTL3)
    stats = rebuild_all("gpu_collateral")
    with_record = {
        r["subject_id"]
        for r in copied_rows(
            "SELECT subject_id FROM fact_current WHERE subject_type = 'instrument' AND field = 'gpu_collateral'"
        )
    }
    assert stats.built == len(with_record) and _doc("gpu_collateral", JUPITER) is None
    doc, row = _doc("gpu_collateral", DDTL3), _fact(DDTL3, "gpu_collateral")
    record = row["value"]
    if _output("gpu_collateral", DDTL3) is None:
        assert doc["model"] == _fact(DDTL3, "gpu_model")["value"]
        assert (
            doc["units"]["value"] == record["units"]["base"] and doc["units"]["methodVersion"] == row["method_version"]
        )
        assert doc["ltvToday"]["value"] == round(record["ltv_pct"] / 100, 4)
        assert doc["valueTodayB"]["value"] == round(record["chips_value_conservative_usd"] / 1e9, 4)
        drawn = _fact(DDTL3, "drawn_usd")
        if drawn and float(drawn["value"]) == record["balance_usd"]:
            assert doc["balanceB"]["tag"] == "F" and doc["balanceB"]["sourceUrl"] == drawn["source_url"]
        first = record["ltv_by_quarter"][0]
        month = int(first["month"][5:7])
        assert doc["ltvByQuarter"][0] == {
            "quarter": f"Q{(month - 1) // 3 + 1} '{first['month'][2:4]}",
            "ltv": round(first["balance_usd"] / first["chips_conservative_usd"], 4),
        }
        rec = _fact(DDTL3, "recovery_gpu")["value"]
        lo, hi = sorted((rec["recovery_conservative_pct"], rec["recovery_moderate_pct"]))
        assert doc["recoveryIfCustomerWalks"]["value"] == (f"{lo}c" if lo == hi else f"{lo}{DASH}{hi}c")
    if _output("gpu_deployment", DDTL3) is None and _output("gpu_deployment", "all") is None:
        assert "deployment" not in doc


def test_curve_waits_for_the_curve_model(seeded):
    rebuild_all("curve")
    out = _output("curve", "all")
    assert _doc("curve", "all") == (curve.curve_tiers(out["output"]) if out else [])


def test_backfilled_day_shows_only_what_was_known(seeded):
    _need(seeded, JUPITER)
    learned = min(
        r["d"]
        for r in copied_rows(
            """SELECT min(learned_at)::date AS d FROM fact WHERE subject_id = %(id)s
           UNION ALL SELECT min(learned_at)::date FROM price_obs WHERE instrument_id = %(id)s""",
            {"id": JUPITER},
        )
        if r["d"] is not None
    )
    before = min(learned, business_today()) - timedelta(days=1)
    rebuild_all("instrument_detail", as_of=before)
    rebuild_all("instrument_detail")
    past, today = _doc("instrument_detail", JUPITER, before), _doc("instrument_detail", JUPITER)
    assert past["sizeB"]["value"] is None and past["terms"] == {}

    # Model outputs dated by that day (a price-model backtest run reads only what was public then) may
    # show; nothing learned later does.
    def known_then(v: dict) -> bool:
        return v["value"] is None or (v["tag"] == "M" and v["asOf"] <= before.isoformat())

    assert all(known_then(v) for v in past["marketCheck"].values())
    assert past["holders"]["count"] == 0 and known_then(past["recovery"])
    assert today["sizeB"]["value"] is not None and today["terms"]


def test_model_outputs_on_sites_and_all_refresh_instrument_views(seeded):
    _need(seeded, JUPITER, DDTL3)
    site = mark_dirty_for("site", "jupiter", "@model:status")
    assert ("instruments", "all") in site and ("instrument_detail", JUPITER) in site
    line = copied_rows("SELECT id, fund_entity_id FROM holding WHERE instrument_id = %s LIMIT 1", (BARN,))
    if line:
        held = {
            r["instrument_id"]
            for r in copied_rows(
                "SELECT DISTINCT instrument_id FROM holding WHERE fund_entity_id = %s AND instrument_id IS NOT NULL",
                (line[0]["fund_entity_id"],),
            )
        }
        marked = {k for v, k in mark_dirty_for("holding", str(line[0]["id"]), "@holding") if v == "instrument_detail"}
        assert held <= marked and BARN in marked
    everything = mark_dirty_for("series", "all", "@model")
    assert ("curve", "all") in everything and ("gpu_collateral", DDTL3) in everything


def test_detail_yields_follow_the_yield_model_and_every_price_point(seeded):
    _need(seeded, FRONTIER)
    out = _output("yield", FRONTIER)
    rebuild_all("instrument_detail")
    rebuild_all("instruments")
    doc = _doc("instrument_detail", FRONTIER)
    row = next(r for r in _doc("instruments", "all") if r["id"] == FRONTIER)
    assert row["ytw"] == doc["yields"]["ytw"] and row["ytm"] == doc["yields"]["ytm"]  # the list agrees with the page
    if out is None:  # the model has not run on the source database: n/d with the reason, never a placeholder
        assert doc["yields"]["ytw"]["value"] is None and doc["yields"]["why"]
        assert all(p["ytm"] is None and p["ytw"] is None for p in doc["priceHistory"])
        return
    body = out["output"]
    assert doc["yields"]["ytw"]["value"] == body["ytw"]["value"] and doc["yields"]["ytw"]["tag"] == "M"
    assert doc["yields"]["ytw"]["methodVersion"] == "yield v0.1"
    by_day = {p["date"]: p for p in body["points"]}
    for p in doc["priceHistory"]:
        got = by_day.get(p["date"])
        if got is not None and abs(got["price"] - p["price"]) < 0.0005:
            assert (p["ytm"], p["ytw"]) == (got["ytm"], got["ytw"])
        else:  # a price re-run since the yields were worked: no stale yield on it
            assert p["ytm"] is None and p["ytw"] is None


# ---------------------------------------------------------------- shapes (handoff/api/types.ts)
def test_instruments_shape(seeded):
    rebuild_all("instruments")
    assert_matches_types("InstrumentRow", [_doc("instruments", "all")], nd=ND_ROW, element=True)


def test_instrument_detail_shape(seeded):
    rebuild_all("instrument_detail")
    docs = [_doc("instrument_detail", i) for i in seeded]
    assert all(set(d) == DETAIL_KEYS for d in docs)
    assert_matches_types("InstrumentDetail", docs, nd=ND_DETAIL)


def test_curve_shape(seeded):
    rebuild_all("curve")
    assert_matches_types("CurveTier", [_doc("curve", "all")], element=True)


def test_gpu_collateral_shape(seeded):
    rebuild_all("gpu_collateral")
    docs = [d for d in (_doc("gpu_collateral", i) for i in seeded) if d is not None]
    if not docs:
        pytest.skip("no GPU-backed instrument among the seeded rows")
    assert_matches_types("GpuCollateral", docs)
