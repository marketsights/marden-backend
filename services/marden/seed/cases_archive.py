"""The archive files the `cases` loader reads (raw first: each is stored as a seed document before
it is parsed), the case -> site map, the loaded instruments and the party resolver.

Canonical copies (DATA_DICTIONARY 0.1): case records from ``aiwcredit.json`` (``cases[]`` and
``gpu``), memos from ``aiwc/memo/<CASE>.json``, model outputs from ``aiwc/out/<CASE>.json`` (not
``out/all.json``), the q3x tables from their loose ``aiwc/q3x/*.json`` files (JSON-identical to
``aiwcredit.q3x``). Not read: ``memo.bak_secfill``, ``memo_prefix``, ``aidebt.pretrace``,
``case_seeds.debt_rows`` and every ``trace/*`` file.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from marden.core.db import Conn
from marden.seed.cases_sink import Src
from marden.seed.common import SEED_AS_OF, as_of, ensure_entity, load_json, seed_dir, seed_document, slugify

AIWCREDIT = "aiwcredit.json"
PROJ = "aiwc/aiw_proj.json"
UW = "aiwc/q3x/underwater.json"
REC = "aiwc/q3x/rec.json"
TRG = "aiwc/q3x/trg.json"
CTG = "aiwc/q3x/contagion.json"
OA = "aiwc/q3x/oa.json"
RV = "aiwc/q3x/rv.json"
COMP = "aiwc/q3x/comp.json"
FAC = "aiwc/q3x/fac.json"
TABLES = (AIWCREDIT, PROJ, UW, REC, TRG, CTG, OA, RV, COMP, FAC)
DOSSIER, MEMO, PARAMS, OUT = "aiwc/cases/{}.json", "aiwc/memo/{}.json", "aiwc/params/{}.json", "aiwc/out/{}.json"
# aiwc/params and aiwc/out carry no date; the per-case files were written on 2026-10-17
# (docs/seed-data/cases-research.md section 4).
MODEL_RUN_AS_OF = date(2026, 10, 17)
SITE_MAP = Path(__file__).with_name("site_map.json")


def site_map() -> list[tuple[str, str]]:
    return [(s["case"], s["slug"]) for s in json.loads(SITE_MAP.read_text())["sites"]]


def file_as_of(relpath: str, data: Any) -> date:
    if relpath.startswith(("aiwc/params/", "aiwc/out/")):
        return MODEL_RUN_AS_OF
    if isinstance(data, dict):
        return as_of(data.get("asof"), data.get("made"))
    return SEED_AS_OF


@dataclass
class Archive:
    conn: Conn
    only: frozenset[str] | None = None  # case ids (tests load a slice)
    data: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, Src] = field(default_factory=dict)
    slugs: dict[str, str] = field(default_factory=dict)  # case -> site slug (sites in the DB only)
    unsited: list[str] = field(default_factory=list)
    instruments: set[str] = field(default_factory=set)

    def load_all(self) -> None:
        in_db = {r["slug"] for r in self.conn.execute("SELECT slug FROM site").fetchall()}
        for case, slug in site_map():
            if self.only is not None and case not in self.only:
                continue
            if slug in in_db:
                self.slugs[case] = slug
            else:
                self.unsited.append(case)
        self.instruments = {r["id"] for r in self.conn.execute("SELECT id FROM instrument").fetchall()}
        paths = list(TABLES)
        for case in self.slugs:
            paths += [p.format(case) for p in (DOSSIER, MEMO, PARAMS, OUT) if (seed_dir() / p.format(case)).exists()]
        for relpath in paths:
            doc = seed_document(relpath, conn=self.conn)  # raw first
            self.data[relpath] = load_json(relpath)
            self.sources[relpath] = Src(relpath, doc.id, file_as_of(relpath, self.data[relpath]))
        self.credit_cases = {c["id"]: c for c in self.data[AIWCREDIT]["cases"]}

    def src(self, relpath: str) -> Src:
        return self.sources[relpath]

    def get(self, pattern: str, case: str) -> dict[str, Any]:
        return self.data.get(pattern.format(case)) or {}

    def has(self, pattern: str, case: str) -> bool:
        return pattern.format(case) in self.data

    def site_of(self, case: Any) -> str | None:
        return self.slugs.get(case) if isinstance(case, str) else None

    def known(self, ids: Any) -> list[str]:
        """The loaded instruments among `ids` (order kept, duplicates dropped)."""
        seq = [ids] if isinstance(ids, str) else list(ids or [])
        return list(dict.fromkeys(i for i in seq if isinstance(i, str) and i in self.instruments))

    @property
    def gpu(self) -> dict[str, Any]:
        return self.data[AIWCREDIT]["gpu"]


# ---------------------------------------------------------------- parties

_PAREN = re.compile(r"\([^)]*\)")
_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|company|llc|ltd|limited|plc|nv|n v|sa|lp|group|holdings)\b")
# Display names in the case tables that differ from the entity's name (checked against the entity
# table at run time: an id that does not exist is ignored).
CURATED = {"alphabet": "google", "amazon": "amazon-aws", "spacex xai": "xai-spacex", "xai": "xai-spacex",
           "cipher": "cipher-digital-inc", "galaxy": "galaxy-digital-inc", "nebius": "nebius"}


def norm(name: Any) -> str:
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", " ", _PAREN.sub(" ", text))
    return " ".join(_SUFFIX.sub(" ", text).split())


@dataclass
class Parties:
    """Finds existing entities by CIK, ticker or normalized name / alias; creates one only when
    asked (`ensure`), through common.ensure_entity."""

    conn: Conn
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_name: dict[str, set[str]] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        for r in self.conn.execute("SELECT id, name, aliases, ticker, cik FROM entity").fetchall():
            self._index(r)

    def _index(self, r: dict[str, Any]) -> None:
        self.rows[r["id"]] = r
        for n in [r["name"], *(r["aliases"] or [])]:
            if norm(n):
                self.by_name.setdefault(norm(n), set()).add(r["id"])

    def _best(self, ids: set[str], name: Any = None) -> str | None:
        """One entity among the candidates: the only one, the one whose id is the name's slug, or
        the only one with a CIK. Ambiguous otherwise (None)."""
        if len(ids) == 1:
            return next(iter(ids))
        if name and slugify(str(name)) in ids:
            return slugify(str(name))
        with_cik = sorted(i for i in ids if self.rows[i].get("cik"))
        return with_cik[0] if len(with_cik) == 1 else None

    def find(self, name: Any = None, *, ticker: str | None = None, cik: str | None = None) -> str | None:
        if cik:
            hit = {i for i, r in self.rows.items() if r.get("cik") == str(cik).lstrip("0")}
            if len(hit) == 1:
                return hit.pop()
        if ticker:
            found = self._best({i for i, r in self.rows.items() if r.get("ticker") == ticker}, name)
            if found:
                return found
        key = norm(name)
        if not key:
            return None
        curated = CURATED.get(key)
        if curated in self.rows:
            return curated
        found = self._best(self.by_name.get(key, set()), name)
        if found is None:
            self.unresolved.add(str(name))
        return found

    def ensure(self, name: str, kind: str, *, ticker: str | None = None, aliases: tuple[str, ...] = ()) -> str:
        found = self.find(name, ticker=ticker)
        for alias in aliases:
            found = found or self.find(alias)
        if found:
            self.unresolved.difference_update([name, *aliases])
            return found
        eid = ensure_entity(self.conn, name, kind, aliases=aliases, ticker=ticker)
        row = self.conn.execute("SELECT id, name, aliases, ticker, cik FROM entity WHERE id = %s", (eid,)).fetchone()
        self._index(row)
        self.unresolved.discard(name)
        return eid
