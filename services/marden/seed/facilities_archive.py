"""The archive files the `facilities` loader reads, stored raw first and parsed as data only.

Every file is recorded with ``seed_document`` before it is parsed; its document id and file-level
as-of travel with each value (``Src``). CSVs are read with the csv module; nothing is executed.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from marden.core.db import Conn
from marden.seed.common import as_of, load_json, seed_dir, seed_document, slugify
from marden.seed.facilities_geo import COUNTIES
from marden.seed.facilities_sink import Src

SITE_MAP = Path(__file__).with_name("site_map.json")
DCMAP = "aiwc/q3x/dcmap.json"
EPOCH_CSV = "aiwc/q3x/epoch_dl/x/data_centers.csv"
EPOCH_TIMELINES = "aiwc/q3x/epoch_dl/x/data_center_timelines.csv"
EPOCH_MAP = "aiwc/q3x/epoch_dl/map_props.json"
SQFT = "aiwc/q3x/sqft.json"
PW_FOUND = "aiwc/q3x/pw_found.json"
POWER = "power.json"
AIWCREDIT = "aiwcredit.json"
SITE_POWER = "aiwc/gpu/site_power.json"
INVENTORY = "dockets/inventory.json"
EVENTS = "aiwc/state/events.json"
CATALYSTS = "aiwc/q3x/cat.json"
DOCS = "aiwc/q3x/docs.json"
LIBRARY = "aiwc/q3x/docs/library.json"


def campus_id(key: str) -> str:
    """Campus id = kebab case of the dcmap key without the 'ep:' prefix."""
    return slugify(key.removeprefix("ep:"))


def docket_key(agency: str, number: str) -> str:
    """'FERC', 'EL25-49' -> 'FERC_EL25-49' (the dockets/out file naming)."""
    return f"{agency.strip()}_{number.strip()}"


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
        if relpath.endswith(".csv"):
            text = (seed_dir() / relpath).read_text(encoding="utf-8")
            parsed: Any = list(csv.DictReader(io.StringIO(text)))
        else:
            parsed = load_json(relpath)
        file_as_of = as_of_value(parsed) if callable(as_of_value) else as_of_value
        self.sources[relpath] = Src(relpath, doc.id, as_of(file_as_of))
        self.data[relpath] = parsed
        return parsed

    def src(self, relpath: str, record_date: Any = None) -> Src:
        base = self.sources[relpath]
        return Src(base.relpath, base.doc_id, as_of(record_date, base.as_of)) if record_date else base

    def load_all(self) -> None:
        made = lambda d: d.get("made") if isinstance(d, dict) else None  # noqa: E731
        self.read(DCMAP, as_of_value=made)
        dcmap_made = self.sources[DCMAP].as_of
        self.read(EPOCH_CSV, as_of_value=dcmap_made)  # the Epoch download behind dcmap
        self.read(EPOCH_TIMELINES, as_of_value=dcmap_made)
        self.read(EPOCH_MAP, as_of_value=dcmap_made)
        self.read(SQFT)
        self.read(PW_FOUND, as_of_value=dcmap_made)  # merged into dcmap rows (pw_conf) in that build
        self.read(COUNTIES)
        self.read(POWER, as_of_value=lambda d: d.get("asof"))
        self.read(AIWCREDIT, as_of_value=lambda d: d.get("asof"))
        self.read(SITE_POWER, as_of_value=lambda d: d.get("asof"))
        self.read(INVENTORY, as_of_value=lambda d: d.get("asof"))
        for key in sorted({r["proceeding_key"] for r in self.inventory}):
            self.read(f"dockets/out/{key}.json", as_of_value=self.sources[INVENTORY].as_of)
        self.read(EVENTS)
        self.read(CATALYSTS, as_of_value=made)
        self.read(DOCS, as_of_value=made)
        self.read(LIBRARY, as_of_value=made)
        for case in sorted({r["case"] for r in self.dcmap_rows if r.get("cov") and r.get("case")}):
            self.read(f"aiwc/memo/{case}.json")

    # ------------------------------------------------------------ campuses
    @cached_property
    def dcmap_rows(self) -> list[dict[str, Any]]:
        return self.data[DCMAP]["rows"]

    @cached_property
    def campus_of_key(self) -> dict[str, str]:
        return {r["key"]: campus_id(r["key"]) for r in self.dcmap_rows}

    @cached_property
    def campus_of_epoch(self) -> dict[str, str]:
        """Epoch data-center name -> campus id (the covered row that carries it, else 'ep:<name>')."""
        out: dict[str, str] = {}
        for r in self.dcmap_rows:
            if r.get("ep") and r["ep"].get("name"):
                out[r["ep"]["name"]] = self.campus_of_key[r["key"]]
        return out

    @cached_property
    def campuses_of_case(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for r in self.dcmap_rows:
            if r.get("cov") and r.get("case"):
                out[r["case"]].append(self.campus_of_key[r["key"]])
        return dict(out)

    @cached_property
    def epoch_rows(self) -> dict[str, dict[str, str]]:
        return {r["Name"]: r for r in self.data[EPOCH_CSV]}

    @cached_property
    def timelines(self) -> dict[str, list[dict[str, str]]]:
        out: dict[str, list[dict[str, str]]] = defaultdict(list)
        for r in self.data[EPOCH_TIMELINES]:
            out[r["Data center"]].append(r)
        return {k: sorted(v, key=lambda r: r["Date"]) for k, v in out.items()}

    @cached_property
    def map_dcs(self) -> dict[str, dict[str, Any]]:
        return {dc["id"]: dc for dc in self.data[EPOCH_MAP]["dataCenters"]}

    @cached_property
    def sqft(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.data[SQFT]:
            out[r["key"]].append(r)
        return dict(out)

    # ------------------------------------------------------------ sites, power and dockets
    @cached_property
    def site_slugs(self) -> dict[str, str]:
        """case id -> financed-site slug (curated map packaged with the sites loader)."""
        return {s["case"]: s["slug"] for s in json.loads(SITE_MAP.read_text())["sites"]}

    @cached_property
    def projects(self) -> dict[str, dict[str, Any]]:
        return {p["id"]: p for p in self.data[POWER]["projects"]}

    @cached_property
    def pw2c(self) -> dict[str, str]:
        return dict(self.data[AIWCREDIT].get("pw2c") or {})

    @cached_property
    def inventory(self) -> list[dict[str, Any]]:
        return self.data[INVENTORY]["rows"]

    @cached_property
    def inventory_by_key(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.inventory:
            out[r["proceeding_key"]].append(r)
        return dict(out)

    def docket_file(self, key: str) -> dict[str, Any]:
        return self.data[f"dockets/out/{key}.json"]

    def memo(self, case: str) -> dict[str, Any] | None:
        return self.data.get(f"aiwc/memo/{case}.json")
