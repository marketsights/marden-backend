"""What the `cases` loader must never carry into Marden (docs/methods/price-sources.md sections 2-4).

The case research files embed values copied from blocked sources: finra.org TRACE last sales and
G-spreads, Börse Frankfurt fixings, Yahoo chart quotes, and yields / spreads / price moves computed
from them. None of those may reach a fact, price, event or model input. This module removes them
before anything is written:

- ``strip(obj, keys)`` drops the named keys at any depth (each table passes its own key set below).
- ``scrub_text(text)`` drops the archive's own sentences that state a market price, yield or spread
  or name a blocked source ("At the TRACE last sale of 102.88 ... it yields 8.5%, 362 bp over ...").
- ``mentions_blocked(text)`` flags text (for example a quote) that cites a blocked source; such a
  quote is not kept.

Prices themselves (fund marks, dealer quotes) are loaded by the `instruments` and `holdings`
loaders; this loader writes no price at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# Keys that hold a market price, a yield or spread computed from one, or a price move.
PRICE_KEYS = frozenset({
    "price", "price_date", "price_kind", "price_src", "price_label", "price_url", "price_note",
    "px", "pxd", "pxk", "px_date", "px_src", "issue_price",
})
# Legacy model outputs (aiwc/out): the input price and everything computed at that price.
SIM_KEYS = PRICE_KEYS | {"yield_pct", "spread_bp", "spread_after_el_bp",
                         # tenant / sponsor hazard rates implied by legacy (TRACE-derived) bond spreads
                         "h_tenant", "h_sponsor"}
# Model inputs (aiwc/params): the input price, and party spreads read off legacy bond spreads.
PARAM_KEYS = PRICE_KEYS | {"spread_bp"}
# Relative value (q3x/rv.json): price, yield, spread, spread after loss, value vs fair, call.
RV_KEYS = PRICE_KEYS | {"y", "spr", "bench", "sae", "vf", "ex", "call", "med", "ylo"}
# Contagion (q3x/contagion.json): bond prices, yields, durations, spread moves, betas, losses.
CTG_KEYS = PRICE_KEYS | {"y", "D", "bobs", "dy", "p0", "dmv", "beta", "beta_src", "beta_med", "l100",
                         "l300", "anchor_dy", "anchor_n", "mv", "sum", "l100_direct", "l100_linked",
                         "l300_direct", "l300_linked", "dmv_direct", "dmv_linked"}
# OpenAI exposure (q3x/oa.json): prices and market-value moves.
OA_KEYS = PRICE_KEYS | {"p0", "dmv", "oa_dmv"}
# Deal comps (q3x/comp.json): today's traded price and spread move ('now', 'chg').
COMP_KEYS = PRICE_KEYS | {"now", "chg"}
# Operator facilities (aiwcredit.gpu.ops[].fac): legacy marks, yields and spreads.
OPS_FAC_KEYS = PRICE_KEYS | {"y", "sp", "b", "s"}
# Trigger and recovery tables carry the legacy mark of each loan.
TABLE_KEYS = PRICE_KEYS

_BLOCKED = re.compile(
    r"\bTRACE\b|last sale|Frankfurt|B[öo]rse|\bfinra\b|G-spread|Yahoo (?:chart|Finance quote)", re.I)
_PRICE_TEXT = re.compile(
    r"\bTRACE\b|last sale|Frankfurt|B[öo]rse|\bfinra\b|G-spread"
    r"|\b(?:quote|mark|price|fair value|last|sale)\s+of\s+\d{2,4}(?:\.\d+)?"
    r"|\byields?\s+(?:about\s+|near\s+|~)?\d+(?:\.\d+)?\s?%"
    r"|\b\d+\s?bp\s+(?:over|above|below)\b|\bleaving\s+\d+\s?bp|\bthe price (?:already )?(?:pays|covers)"
    r"|\btrad(?:es|ed|ing)\s+(?:at|near|around|below|above|under|<|>)\s*~?\d"
    r"|\bquoted\s+(?:at\s+)?~?\d|\bmarked\s+(?:it\s+)?(?:at\s+)?~?\d|\bpts?\s+(?:below|above)\s+model"
    r"|\bbonds?\s+(?:trade|yield)\w*\s+(?:near|around|at|~)?\s*~?\d", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def strip(obj: Any, keys: Iterable[str]) -> Any:
    """A copy of `obj` without the given keys at any depth."""
    drop = frozenset(keys)
    if isinstance(obj, dict):
        return {k: strip(v, drop) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [strip(v, drop) for v in obj]
    return obj


def mentions_blocked(text: Any) -> bool:
    return isinstance(text, str) and bool(_BLOCKED.search(text))


def scrub_text(text: Any) -> str | None:
    """The text without its sentences that state a market price, yield or spread, or that name a
    blocked source. None when nothing is left."""
    if not isinstance(text, str) or not text.strip():
        return None
    kept = [s for s in _SENTENCE.split(text.strip()) if not _PRICE_TEXT.search(s)]
    out = " ".join(kept).strip()
    return out or None


def scrub_all(obj: Any) -> Any:
    """`scrub_text` on every string inside a list or dict (empty results dropped)."""
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, list):
        return [v for v in (scrub_all(x) for x in obj) if v not in (None, "", [], {})]
    if isinstance(obj, dict):
        return {k: v for k, v in ((k, scrub_all(x)) for k, x in obj.items()) if v not in (None, "", [], {})}
    return obj
