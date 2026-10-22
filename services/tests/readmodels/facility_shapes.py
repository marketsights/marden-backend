"""Check Data centers / On-site power documents against handoff/api/types.ts with the TypeScript compiler.

Each document is an object literal annotated with its response type, so `tsc --strict` applies
excess-property checks, literal unions (DcStatus, PlantStatus, PlantTech, Tag ...) and null checks as
the web app's compiler would. The only departures from types.ts are the n/d allowances below: plain
(non-Sourced) fields that types.ts declares non-null but that no record has filled yet (a plant's
point and county, a unit's OEM or rating, an air permit's type or status, a site's chain status
before the status model has run). Each is null in the document, never a placeholder.
Skips when the API's node_modules (tsc, @types/geojson) are not installed.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from sourcing import assert_sourced
from ts_shapes import GEOJSON, TSC, TYPES

ND_TYPES = """
type N<X> = X | null;
type Over<A, B> = Omit<A, keyof B> & B;
type Financed = Over<T.FinancedLink, { chainStatus: N<T.ChainStatus> }>;
type DcOver = { county: N<string>; lastChange: N<T.Evidence>; firstSeen: N<string>; financed: N<Financed> };
export type DcRowDoc = Over<T.DataCenterRow, DcOver>;
export type DcDetailDoc = Over<T.DataCenterDetail, DcOver>;
export type DcPageDoc = Over<T.DataCenterPage, { rows: DcRowDoc[]; asOf: N<string> }>;
type PermitOver = { type: N<string>; status: N<T.PlantStatus> };
type RowPermit = Over<NonNullable<T.PowerPlantRow['airPermit']>, PermitOver>; // Omit over `X | null` keeps no keys
export type UnitDoc = Over<T.PowerUnit, {
  oem: N<string>; model: N<string>; totalMw: N<number>; fuel: N<string>; status: N<T.PlantStatus>;
  permitRef: N<string> }>;
type PlantOver = {
  county: N<string>; lat: N<number>; lon: N<number>; technology: N<T.PlantTech>;
  units: { count: number; byModel: { oem: N<string>; model: N<string>; count: number }[] };
  airPermit: N<RowPermit>; financed: N<Financed>; lastChange: N<T.Evidence> };
export type PlantRowDoc = Over<T.PowerPlantRow, PlantOver>;
type Fuel = Over<NonNullable<T.PowerPlantDetail['fuel']>, { regulator: N<string>; docket: N<string> }>;
type Served = Over<NonNullable<T.PowerPlantDetail['servedCampus']>, { status: N<T.DcStatus> }>;
// Plant detail additions (spec 15 Air permit card and Fuel supply): the permit's holder, facility, other
// ids and emission limits as the record states them, and the units' fuel as stated.
type PermitExtra = {
  holder: N<string>; facility: N<{ id: string; name: N<string> }>; otherIds: string[];
  limits: { pollutant: string; value: number; unit: string; basis: string; moreThan: boolean; url: string }[];
  related: { agency: string; number: string; type: N<string>; status: N<T.PlantStatus>; filed: N<string>;
    decided: N<string>; url: string }[] };
export type PlantDetailDoc = Over<T.PowerPlantDetail, PlantOver & {
  unitRows: UnitDoc[]; permit: N<Over<T.AirPermit, PermitOver> & PermitExtra>; fuel: N<Fuel>; servedCampus: N<Served>;
  fuelType: T.Sourced<string> }>;
export type PlantPageDoc = Over<T.PowerPlantPage, { rows: PlantRowDoc[]; asOf: N<string> }>;
export type BuildingDoc = T.Building & { inService: T.Sourced<string> };
"""

# Response type (with the n/d allowances above) of each facility view's document.
VIEW_TYPES = {
    "dc_row": "DcRowDoc",
    "dc_detail": "DcDetailDoc",
    "dc_tab:buildings": "BuildingDoc[]",  # + inService (date in service, n/d until a record dates it)
    "dc_tab:records": "T.DcRecord[]",
    "dc_tab:history": "T.DcHistoryEvent[]",
    "dc_tab:capacity": "T.CapacityEstimate[]",
    "plant_row": "PlantRowDoc",
    "plant_detail": "PlantDetailDoc",
    "plant_debt": "T.PlantDebt",
}


def assert_shapes(checks: list[tuple[str, list[Any]]]) -> None:
    """Every doc of each (TS type expression, docs) pair has that type; one tsc run for all."""
    assert_sourced([doc for _, docs in checks for doc in docs])  # hard rule 4: every value names its source
    if not (TSC.exists() and GEOJSON.exists() and TYPES.exists()):
        pytest.skip("TypeScript toolchain (apps/api/node_modules) not installed")
    lines = [f"import type * as T from {json.dumps(str(TYPES.with_suffix('')))};", ND_TYPES]
    n = 0
    for type_expr, docs in checks:
        for doc in docs:
            lines.append(f"export const doc{n}: {type_expr} = {json.dumps(doc, ensure_ascii=False)};")
            n += 1
    with tempfile.TemporaryDirectory(prefix="marden-facility-shape-") as tmp:
        base = Path(tmp)
        (base / "check.ts").write_text("\n".join(lines) + "\n")
        (base / "tsconfig.json").write_text(
            json.dumps(
                {
                    "compilerOptions": {
                        "strict": True,
                        "noEmit": True,
                        "skipLibCheck": True,
                        "module": "ESNext",
                        "moduleResolution": "Bundler",
                        "target": "ES2022",
                        "types": [],
                        "paths": {"geojson": [str(GEOJSON)]},
                    },
                    "files": ["check.ts"],
                }
            )
        )
        run = subprocess.run([str(TSC), "-p", str(base / "tsconfig.json")], capture_output=True, text=True, timeout=300)
        assert run.returncode == 0, f"documents do not match types.ts:\n{run.stdout[:6000]}{run.stderr[:2000]}"
