"""MARDEN_SHOW_UNLICENSED_PRICES in the read models (docs/methods/licensing.md §3), on real rows.

The rows are copied from the local `marden` database (real_rows.copy_instruments): Oracle 5.2% 2035
(EX-ORCL-239: finra.org TRACE prints, Börse Frankfurt fixings, TRACE security-master facts), CoreWeave
9% 2031 (EX-CRWV-102: TRACE prints and facts) and CoreWeave EX-CRWV-103 (TRACE prints, a dealer quote).
With the switch true the builders serve those values as before; with it false no value, source name or
source link of an unlicensed source is left in any document, and the licensed values stay. A scan of the
builder code checks that every direct `fact` / `price_obs` read applies the switch, as
tests/models/test_model_input_paths.py does for the models.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from real_rows import copied_rows, copy_instruments

from marden.core import config, db, licensing
from marden.core.facts import current_facts, facts_asof
from marden.readmodels import business_today, rebuild_all
from marden.readmodels.context import building, known_fact_rows, known_facts
from marden.readmodels.views import instrument_detail, instruments  # noqa: F401 (builders)

ORACLE, CRWV31, CRWV_DEALER = "EX-ORCL-239", "EX-CRWV-102", "EX-CRWV-103"
SEEDED = (ORACLE, CRWV31, CRWV_DEALER)
HIDDEN = set(licensing.unlicensed_source_names())
READMODELS = Path(__file__).resolve().parents[2] / "marden" / "readmodels"


@pytest.fixture
def seeded() -> list[str]:
    found = copy_instruments(SEEDED)
    rows = copied_rows("SELECT DISTINCT source_name FROM price_obs WHERE source_name = ANY(%s)", (sorted(HIDDEN),))
    if not rows:
        pytest.skip("no unlicensed price rows (TRACE, Börse Frankfurt) in the source database")
    return found


@pytest.fixture
def switch(monkeypatch) -> Iterator[Any]:
    def set_to(show: bool) -> None:
        monkeypatch.setenv("MARDEN_SHOW_UNLICENSED_PRICES", "true" if show else "false")
        config.reload_settings()

    yield set_to
    monkeypatch.delenv("MARDEN_SHOW_UNLICENSED_PRICES", raising=False)
    config.reload_settings()


def _unlicensed_urls() -> set[str]:
    """Every source link the copied unlicensed rows carry (finra.org bond pages, Börse Frankfurt pages)."""
    rows = copied_rows(
        """SELECT source_url FROM price_obs WHERE source_name = ANY(%(h)s) AND source_url IS NOT NULL
           UNION SELECT source_url FROM fact WHERE source_name = ANY(%(h)s) AND source_url IS NOT NULL""",
        {"h": sorted(HIDDEN)},
    )
    return {r["source_url"] for r in rows}


def _strings(doc: Any) -> Iterator[str]:
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _strings(v)
    elif isinstance(doc, str):
        yield doc


def _source_names(doc: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(doc, dict):
        if isinstance(doc.get("sourceName"), str):
            out.add(doc["sourceName"])
        for v in doc.values():
            out |= _source_names(v)
    elif isinstance(doc, list):
        for v in doc:
            out |= _source_names(v)
    return out


def _docs(view: str) -> dict[str, Any]:
    rows = copied_rows("SELECT key, doc FROM read_model WHERE view = %s AND as_of = %s", (view, business_today()))
    return {r["key"]: r["doc"] for r in rows}


def _rebuild() -> dict[str, dict[str, Any]]:
    for view in ("instruments", "instrument_detail"):
        rebuild_all(view)
    return {"instruments": {r["id"]: r for r in _docs("instruments")["all"]}, **_docs("instrument_detail")}


def test_hidden_precedence_picks_what_the_views_pick_on_real_rows(seeded):
    """The filtered reads (core.facts PRECEDENCE) choose exactly the fact_current / fact_asof row when
    nothing they read is hidden."""
    nothing = ["(no such source)"]
    days = [business_today(), business_today() - timedelta(days=2), business_today() - timedelta(days=60)]
    with db.transaction() as conn:
        for iid in seeded:
            current = current_facts(conn, "instrument", iid)
            assert current and {f: r["id"] for f, r in current.items()} == {
                f: r["id"] for f, r in current_facts(conn, "instrument", iid, hidden=nothing).items()}
            for day in days:
                assert {f: r["id"] for f, r in facts_asof(conn, day, "instrument", iid).items()} == {
                    f: r["id"] for f, r in facts_asof(conn, day, "instrument", iid, hidden=nothing).items()}


def test_known_facts_drop_unlicensed_rows_and_fall_back(seeded, switch):
    trace_fields = {r["field"] for r in copied_rows(
        "SELECT field FROM fact_current WHERE subject_type = 'instrument' AND subject_id = %s AND source_name = %s",
        (ORACLE, licensing.FINRA_TRACE))}
    if not trace_fields:
        pytest.skip("no current TRACE security-master fact on the Oracle note")
    licensed = {r["field"]: r["id"] for r in copied_rows(
        """SELECT DISTINCT ON (field) field, id FROM fact WHERE subject_type = 'instrument' AND subject_id = %s
             AND field = ANY(%s) AND status = 'published' AND source_name <> ALL(%s)
           ORDER BY field, (tag = 'M'), valid_from DESC, (CASE tag WHEN 'F' THEN 0 WHEN 'R' THEN 1 ELSE 2 END),
                    learned_at DESC, id DESC""", (ORACLE, sorted(trace_fields), sorted(HIDDEN)))}
    with db.transaction() as conn:
        switch(True)
        shown = known_facts(conn, "instrument", ORACLE)
        assert {f for f, r in shown.items() if r["source_name"] == licensing.FINRA_TRACE} == trace_fields
        switch(False)
        hidden = known_facts(conn, "instrument", ORACLE)
        rows = known_fact_rows(conn, "instrument", ORACLE, sorted(trace_fields))
        with building(business_today() - timedelta(days=1)):  # a backfilled snapshot reads fact_asof
            past = known_facts(conn, "instrument", ORACLE)
    assert not {r["source_name"] for r in hidden.values()} & HIDDEN
    assert {f: hidden[f]["id"] for f in trace_fields if f in hidden} == licensed  # the next licensed value, or n/d
    assert not {r["source_name"] for rs in rows.values() for r in rs} & HIDDEN
    assert not {r["source_name"] for r in past.values()} & HIDDEN
    assert set(shown) - trace_fields <= set(hidden)  # licensed fields are untouched


def test_instrument_views_serve_unlicensed_values_only_while_the_switch_is_true(seeded, switch):
    urls = _unlicensed_urls()
    switch(True)
    shown = _rebuild()
    names = set().union(*(_source_names(d) for d in shown.values()))
    assert names & HIDDEN, "with the switch true the real TRACE / Frankfurt values are served"
    oracle = shown[ORACLE]
    assert oracle["marketCheck"]["Last trade"]["sourceName"] == licensing.FINRA_TRACE
    trade = copied_rows(
        """SELECT price FROM price_obs WHERE instrument_id = %s AND kind = 'trade' AND source_name = %s
           ORDER BY obs_date DESC, obs_at DESC NULLS LAST, id DESC LIMIT 1""", (ORACLE, licensing.FINRA_TRACE))[0]
    assert re.sub(r"[^0-9.]", "", str(oracle["marketCheck"]["Last trade"]["value"])).startswith(
        f"{float(trade['price']):.1f}"[:4])

    switch(False)
    hidden = _rebuild()
    for key, doc in hidden.items():
        assert not _source_names(doc) & HIDDEN, key
        leaked = [s for s in _strings(doc) if s in HIDDEN or any(u in s for u in urls)]
        assert not leaked, f"{key}: {leaked[:3]}"
    # Licensed values stay: SEC EDGAR terms and the filed fund marks are served either way.
    for key in (ORACLE, CRWV31):
        before, after = shown[key], hidden[key]
        assert after["terms"]["Coupon"] == before["terms"]["Coupon"]
        assert after["terms"]["Coupon"]["sourceName"] not in HIDDEN
        assert after["fundMarks"] == before["fundMarks"]
        assert "Last trade" not in after["marketCheck"] and "Exchange quote" not in after["marketCheck"]
    rows = hidden["instruments"]
    assert all(not _source_names(rows[i]) & HIDDEN for i in seeded if i in rows)


def test_dealer_quote_keeps_licensed_sources(seeded, switch):
    """A dealer quote whose source is not flagged is served with the switch either way."""
    quote = copied_rows(
        "SELECT * FROM price_obs WHERE instrument_id = %s AND kind = 'dealer' ORDER BY obs_date DESC, id DESC LIMIT 1",
        (CRWV_DEALER,))
    if not quote or licensing.is_unlicensed(quote[0]["source_name"]):
        pytest.skip("no licensed dealer quote on EX-CRWV-103 in the source database")
    switch(False)
    doc = _rebuild()[CRWV_DEALER]
    assert doc["marketCheck"]["Dealer quote"]["sourceName"] == quote[0]["source_name"]


# ---------------------------------------------------------------- every builder read applies the switch
READ = re.compile(r"\b(FROM|JOIN)\s+(fact|price_obs|fact_current|fact_asof)\b", re.I)
SWITCH = ("hidden_source_names", "_licence_clause")
# Direct reads that never reach a document, with the reason.
NOT_SERVED = {
    ("deps", "*"): "dirty marks: which read models a new value touches (no value is read into a document)",
    ("deps_payers", "*"): "dirty marks for the payer views (no value is read into a document)",
    ("deps_chains", "*"): "dirty marks for the chain cards and lender groups (no value is read into a document)",
}


def _reads() -> dict[tuple[str, str], str]:
    """(module, function) -> that function's source, for every SQL string reading fact or price_obs
    ('<module>' for SQL outside any function)."""
    out: dict[tuple[str, str], str] = {}
    for path in sorted([*READMODELS.glob("*.py"), *(READMODELS / "views").glob("*.py")]):
        tree = ast.parse(path.read_text(), filename=str(path))
        owner: dict[int, ast.FunctionDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    owner.setdefault(id(child), node)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and READ.search(node.value):
                fn = owner.get(id(node))
                out[(path.stem, fn.name if fn else "<module>")] = ast.unparse(fn) if fn else node.value
    return out


def test_every_builder_read_of_facts_or_prices_applies_the_switch():
    reads = _reads()
    expected = {("context", "known_fact_rows"), ("instrument_inputs", "price_rows"), ("sites_common", "dealer_quote")}
    assert expected <= set(reads)
    unfiltered = sorted(k for k, body in reads.items()
                        if (k[0], "*") not in NOT_SERVED and not any(s in body for s in SWITCH))
    assert not unfiltered, f"builder reads of fact/price_obs that ignore MARDEN_SHOW_UNLICENSED_PRICES: {unfiltered}"


def test_the_api_hides_exactly_the_same_source_names():
    """apps/api/src/lib/licensing.ts keeps its own copy of the list (the API reads the same switch)."""
    ts = (Path(__file__).resolve().parents[3] / "apps" / "api" / "src" / "lib" / "licensing.ts").read_text()
    block = re.search(r"UNLICENSED_SOURCE_NAMES: readonly string\[\] = \[(.*?)\];", ts, re.S)
    assert block, "UNLICENSED_SOURCE_NAMES not found in apps/api/src/lib/licensing.ts"
    assert re.findall(r"'([^']+)'", block.group(1)) == licensing.unlicensed_source_names()
