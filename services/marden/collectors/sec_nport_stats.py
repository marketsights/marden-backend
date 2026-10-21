"""Run counts of the `sec_nport` collector: `source_run.meta` and the CLI output."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

MAX_LISTED = 100


@dataclass
class RunStats:
    today: date
    since: date
    until: date
    datasets: list[str]
    periods: list[str] | None = None
    watched_keys: int = 0
    watched_instruments: int = 0
    ambiguous_keys: dict[str, list[str]] = field(default_factory=dict)
    index_days_read: list[str] = field(default_factory=list)
    index_days_done_before: list[str] = field(default_factory=list)
    index_days_missing: list[str] = field(default_factory=list)
    datasets_read: list[str] = field(default_factory=list)
    datasets_done_before: list[str] = field(default_factory=list)
    datasets_missing: list[str] = field(default_factory=list)
    dataset: dict[str, Any] = field(default_factory=dict)
    filings_listed: int = 0
    filings_known: int = 0
    filings_fetched: int = 0
    filings_skipped_no_match: int = 0
    bytes_skipped: int = 0
    filings_reference_only: int = 0
    dataset_candidates_unmatched: int = 0
    filings_matched: int = 0
    matched_by_period: Counter = field(default_factory=Counter)
    documents_new: int = 0
    holdings_new: int = 0
    holdings_seen: int = 0
    holdings_linked: int = 0
    positions_marked: int = 0
    positions_without_mark: int = 0
    facts_new: int = 0
    prices_new: int = 0
    median_groups: int = 0
    median_prices_new: int = 0
    median_facts_new: int = 0
    wide_dispersion: list[str] = field(default_factory=list)
    fx: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    def meta(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k not in ("today", "since", "until", "matched_by_period")}
        out.update(today=self.today.isoformat(), since=self.since.isoformat(), until=self.until.isoformat(),
                   matched_by_period=dict(sorted(self.matched_by_period.items())), errors=self.errors[:MAX_LISTED],
                   wide_dispersion=self.wide_dispersion[:MAX_LISTED])
        return out

    def summary(self) -> str:
        return json.dumps(self.meta(), indent=2, default=str)
