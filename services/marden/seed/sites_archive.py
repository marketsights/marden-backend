"""The archive files the `sites` loader reads, stored raw first and parsed as data only.

Every file is recorded with ``seed_document`` before it is parsed, and its document id travels
with each value taken from it (``Src``). The HTML page is never executed: the loader only takes
the text of its ``<script id="aichain" type="application/json">`` block and parses it as JSON.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from typing import Any

from marden.core.db import Conn
from marden.seed.common import as_of, load_json, seed_dir, seed_document
from marden.seed.sites_entities import Listing
from marden.seed.sites_facts import Src

SITE_MAP = "site_map.json"  # packaged next to this module (curated case -> slug)
HTML = "parallax-credit-marks.html"
_AICHAIN = re.compile(r'<script id="aichain" type="application/json">(.*?)</script>', re.S)


def _html_json_block(text: str) -> dict[str, Any]:
    m = _AICHAIN.search(text)
    if not m:
        raise ValueError(f"{HTML}: no aichain JSON block")
    return json.loads(m.group(1))


@dataclass
class Archive:
    conn: Conn
    sources: dict[str, Src] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    def read(self, relpath: str, *, as_of_value: Any = None) -> Any:
        """Store the file raw, record its document, parse it (once per run)."""
        if relpath in self.data:
            return self.data[relpath]
        doc = seed_document(relpath, conn=self.conn)
        if relpath == HTML:
            parsed: Any = _html_json_block((seed_dir() / relpath).read_text(encoding="utf-8"))
            file_as_of = parsed.get("asOf")
        else:
            parsed = load_json(relpath)
            file_as_of = as_of_value(parsed) if callable(as_of_value) else as_of_value
        self.sources[relpath] = Src(relpath, doc.id, as_of(file_as_of))
        self.data[relpath] = parsed
        return parsed

    def src(self, relpath: str, record_date: Any = None) -> Src:
        base = self.sources[relpath]
        return Src(base.relpath, base.doc_id, as_of(record_date, base.as_of)) if record_date else base

    # ------------------------------------------------------------ the files
    def load_all(self, cases: list[str]) -> None:
        self.read("aiwc/memo/titles.json")
        self.read("aiwc/memo/shortnames.json")
        for case in cases:
            self.read(f"aiwc/cases/{case}.json", as_of_value=lambda d: d.get("asof"))
            self.read(f"aiwc/memo/{case}.json")
        self.read("aiwc/q3x/site.json", as_of_value=lambda d: d.get("reviewed") or d.get("made"))
        self.read("aiwc/q3x/ctr.json", as_of_value=lambda d: d.get("made"))
        self.read("aiwc/q3x/dcmap.json", as_of_value=lambda d: d.get("made"))
        self.read("aiwc/gpu/site_power.json", as_of_value=lambda d: d.get("asof"))
        self.read("aiwc/gpu/operators.json", as_of_value=lambda d: d.get("asof"))
        self.read("aiwc/q3x/docs.json")
        self.read("aiwc/q3x/docs/library.json", as_of_value=lambda d: d.get("made"))
        self.read("aiwc/q3x/docs/tickers.json")
        self.read("aidebt.json")
        self.read("aiwcredit.json", as_of_value=lambda d: d.get("asof"))
        self.read("power.json", as_of_value=lambda d: d.get("asof"))
        self.read(HTML)

    def case(self, case: str) -> dict[str, Any]:
        return self.data[f"aiwc/cases/{case}.json"]

    def memo(self, case: str) -> dict[str, Any]:
        return self.data[f"aiwc/memo/{case}.json"]

    @property
    def aichain(self) -> dict[str, Any]:
        return self.data[HTML]

    # ------------------------------------------------------------ derived indexes
    @cached_property
    def tickers(self) -> dict[str, Listing]:
        raw = self.data["aiwc/q3x/docs/tickers.json"]
        rows = raw.values() if isinstance(raw, dict) else raw
        out: dict[str, Listing] = {}
        for r in rows:
            t = str(r.get("ticker") or "").upper()
            if t and t not in out:
                out[t] = Listing(t, str(r["cik_str"]), str(r.get("title") or ""))
        return out

    @cached_property
    def debt_rows(self) -> dict[str, dict[str, Any]]:
        """aidebt rows as dicts keyed by instrument id."""
        d = self.data["aidebt.json"]
        cols = d["cols"].split(",")
        return {r[0]: dict(zip(cols, r, strict=True)) for r in d["rows"]}

    @cached_property
    def rowinfo(self) -> dict[str, dict[str, Any]]:
        return self.data["aiwcredit.json"]["rowinfo"]

    @cached_property
    def case_debt(self) -> dict[str, list[str]]:
        """case -> the case's own instruments (aiwcredit.rowinfo.c)."""
        out: dict[str, list[str]] = defaultdict(list)
        for iid, info in self.rowinfo.items():
            if info.get("c"):
                out[info["c"]].append(iid)
        return {k: sorted(v) for k, v in out.items()}

    @cached_property
    def case_grp(self) -> dict[str, str]:
        """case -> the aidebt group of its own debt (the project's borrower group)."""
        out: dict[str, str] = {}
        for case, ids in self.case_debt.items():
            grps = Counter(self.debt_rows[i]["grp"] for i in ids if i in self.debt_rows)
            if grps:
                out[case] = grps.most_common(1)[0][0]
        return out

    @cached_property
    def case_links(self) -> dict[str, dict[str, list[str]]]:
        """case -> role -> instruments linked to the case (aiwcredit.rowinfo.x)."""
        out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for iid, info in self.rowinfo.items():
            for case, role in info.get("x") or []:
                out[case][role].append(iid)
        return out

    @cached_property
    def site_json(self) -> dict[str, dict[str, Any]]:
        return {r["key"]: r for r in self.data["aiwc/q3x/site.json"]["sites"]}

    @cached_property
    def dcmap_by_case(self) -> dict[str, dict[str, Any]]:
        return {r["case"]: r for r in self.data["aiwc/q3x/dcmap.json"]["rows"] if r.get("cov") and r.get("case")}

    @cached_property
    def ctr_by_case(self) -> dict[str, dict[str, Any]]:
        return {r["id"]: r for r in self.data["aiwc/q3x/ctr.json"]["rows"]}

    @property
    def aiwcredit_as_of(self) -> date:
        return self.sources["aiwcredit.json"].as_of
