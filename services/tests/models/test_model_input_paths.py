"""Every model input read goes through the point-in-time and licence filters, or is listed here with why not.

The models read facts and events through `marden.models.pit` (public dates, backtests, the licence switch)
and price observations through `marden.models.price_marks` / `track_record_cases.market_series` (fund-mark
lags, the licence switch). This test scans every module under marden/models for SQL that reads `fact`,
`event`, `price_obs` or `holding` directly and fails on any read that is neither one of those filtered
readers nor listed below with its reason, so a new direct read cannot bypass the filters unnoticed
(docs/methods/licensing.md, pricing-v0.1 "Backtesting"). Stored model outputs (`model_output`) are Marden's
own and are read point in time by their as-of day (marden.models.runs), so they are not scanned.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

MODELS = Path(__file__).resolve().parents[2] / "marden" / "models"
READ = re.compile(r"\bFROM\s+(fact|event|price_obs|holding)\b", re.I)

# Readers that apply the licence switch (and the public-date rules) themselves.
FILTERED = {
    ("pit", "fact_rows"), ("pit", "event_rows"),
    ("price_marks", "load"), ("track_record_cases", "market_series"),
}
# Direct reads that are not inputs from a price or licence-flagged source, with the reason.
ALLOWED = {
    ("cushion", "<module>"): "trigger key maps (which sites a plant or obligation feeds); not an input value",
    ("cushion_history", "eia_report_count"): "counts EIA-860M report months, public_date applied (EIA is public)",
    ("gpu_inputs", "_ust"): "U.S. Treasury par yields (public domain), valid_from <= the run day",
    ("mark_gap_inputs", "_lines"): "SEC N-PORT / BDC holding lines; line_public_date applied by the caller",
    ("mark_gap_inputs", "_fund_newest"): "each fund's newest public filing period (line_public_date applied)",
    ("mark_gap_inputs", "_holding_facts"): "currency and mark of SEC holding lines already filtered by period",
    ("mark_gap_inputs", "prices"): "Marden's own daily price rows (price_obs kind 'marden'), as of the run day",
    ("mark_gap", "<module>"): "trigger key maps (instruments a fund holds); not an input value",
    ("mark_gap", "_instrument_ids"): "subject selection (V1 or fund-held instruments); not an input value",
    ("price_run", "_write_price_obs"): "the model's own write path (replaces its 'marden' rows)",
    ("price_run", "_observed_price"): "trigger key map (skips the model's own 'marden' rows); not an input value",
    ("recovery", "_targets"): "subject selection (which instruments to run); not an input value",
}


def _direct_reads() -> dict[tuple[str, str], list[str]]:
    found: dict[tuple[str, str], list[str]] = {}
    for path in sorted(MODELS.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        owner: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    owner.setdefault(id(child), node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and READ.search(node.value):
                key = (path.stem, owner.get(id(node), "<module>"))
                found.setdefault(key, []).extend(m.group(1).lower() for m in READ.finditer(node.value))
    return found


def test_every_direct_input_read_is_filtered_or_listed_with_a_reason():
    reads = _direct_reads()
    assert set(reads) >= FILTERED, f"the filtered readers moved: {sorted(FILTERED - set(reads))}"
    unlisted = {k: v for k, v in reads.items() if k not in FILTERED and k not in ALLOWED}
    assert not unlisted, ("direct fact/event/price_obs/holding reads that bypass marden.models.pit and the licence "
                          f"switch: {unlisted}")
    stale = sorted(set(ALLOWED) - set(reads))
    assert not stale, f"listed reads that no longer exist (remove them): {stale}"


def test_filtered_readers_apply_the_licence_switch():
    for module, function in FILTERED:
        tree = ast.parse((MODELS / f"{module}.py").read_text())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
        body = ast.unparse(fn)
        assert "_licence_clause" in body or "unlicensed_source_names" in body, f"{module}.{function}"
