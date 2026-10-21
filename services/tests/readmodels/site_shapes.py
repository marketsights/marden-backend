"""Check site read-model documents against the API's response types with the TypeScript compiler.

The types are apps/api/src/contracts/types.ts (a verbatim copy of handoff/api/types.ts) and
site-tabs.ts (the Site detail tab payloads the handoff names only in words). Each document is written
as an object literal annotated with its type, so tsc --strict applies excess-property checks, literal
unions and null checks exactly as the app's compiler would.

`SITE_TYPES` names each view's type. `ND` widens the fields that are null while the model that fills
them has not run (a chain status before the status model, a projection before the cushion model, the
project file's draw-pace flag and equipment 'late' before the M v0 signals; the embedded plant row's
unrecorded plain fields, as facility_shapes allows them):
types.ts gives them no null, and the rule is n/d, never a placeholder. Everything else is exact.
Usable from tests (`check`) and from the command line on saved API responses:

    python tests/readmodels/site_shapes.py <view>=<file.json> ...
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from facility_shapes import ND_TYPES as FACILITY_ND  # the embedded plant row's n/d allowances

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "apps" / "api" / "src" / "contracts"
TSC = REPO / "apps" / "api" / "node_modules" / ".bin" / "tsc"
GEOJSON = REPO / "apps" / "api" / "node_modules" / "@types" / "geojson" / "index.d.ts"

PRELUDE = """
type Nd<X, K extends keyof X> = Omit<X, K> & { [P in K]: X[P] | null };
type SummaryNd = Nd<T.SiteSummary, 'status'>;
type CompletionRowNd = Nd<T.CompletionRow, 'driver' | 'lastEvidence'>;
type CompletionNd = Omit<T.CompletionResponse, 'rows'> & { rows: CompletionRowNd[] };
type DetailNd = Nd<T.SiteDetail, 'status' | 'debtExposed'>;
type ScheduleNd = Nd<T.SiteSchedule, 'projection'>;
type EquipmentNd = Nd<T.ProjectConstruction['equipment'][number], 'late'>;
type ConstructionNd = Omit<Nd<T.ProjectConstruction, 'drawPaceFlag'>, 'equipment'> & { equipment: EquipmentNd[] };
type ProjectFileNd = Omit<T.ProjectFile, 'construction'> & { construction: ConstructionNd };
""" + FACILITY_ND + """
type PowerTabNd = Omit<S.SitePowerTab, 'plant'> & { plant?: PlantRowDoc | null };
type BuildingsTabNd = Omit<S.SiteBuildingsTab, 'plant'> & { plant?: PlantRowDoc };
"""

# view -> (exact type, type with the model-pending n/d fields widened)
SITE_TYPES: dict[str, tuple[str, str]] = {
    "sites": ("T.SiteSummary[]", "SummaryNd[]"),
    "completion": ("T.CompletionResponse", "CompletionNd"),
    "site_detail": ("T.SiteDetail", "DetailNd"),
    "site_schedule": ("T.SiteSchedule", "ScheduleNd"),
    "site_tab:evidence": ("S.SiteEvidenceTab", "S.SiteEvidenceTab"),
    "site_tab:power": ("S.SitePowerTab", "PowerTabNd"),
    "site_tab:opposition": ("S.SiteOppositionTab", "S.SiteOppositionTab"),
    "site_tab:events": ("S.SiteEventsTab", "S.SiteEventsTab"),
    "site_tab:debt": ("S.SiteDebtTab", "S.SiteDebtTab"),
    "site_tab:lease": ("S.SiteLeaseTab", "S.SiteLeaseTab"),
    "site_tab:buildings": ("S.SiteBuildingsTab", "BuildingsTabNd"),
    "project_file": ("T.ProjectFile", "ProjectFileNd"),
}


def available() -> bool:
    return TSC.exists() and GEOJSON.exists() and (CONTRACTS / "site-tabs.ts").exists()


def check(docs: list[tuple[str, Any]], *, exact: bool = False) -> str:
    """tsc's errors for (view, doc) pairs ('' when every doc matches its view's type)."""
    with tempfile.TemporaryDirectory(prefix="marden-site-shape-") as tmp:
        base = Path(tmp)
        lines = [
            f"import type * as T from {json.dumps(str(CONTRACTS / 'types'))};",
            f"import type * as S from {json.dumps(str(CONTRACTS / 'site-tabs'))};",
            PRELUDE,
        ]
        for i, (view, doc) in enumerate(docs):
            strict, widened = SITE_TYPES[view]
            lines.append(f"// {view}")
            literal = json.dumps(doc, ensure_ascii=False)
            lines.append(f"export const doc{i}: {strict if exact else widened} = {literal};")
        (base / "check.ts").write_text("\n".join(lines) + "\n")
        (base / "tsconfig.json").write_text(json.dumps({
            "compilerOptions": {
                "strict": True, "noEmit": True, "skipLibCheck": True, "module": "ESNext",
                "moduleResolution": "Bundler", "target": "ES2022", "types": [],
                "paths": {"geojson": [str(GEOJSON)]},
            },
            "files": ["check.ts"],
        }))
        run = subprocess.run([str(TSC), "-p", str(base / "tsconfig.json")], capture_output=True, text=True,
                             timeout=600)
        if run.returncode == 0:
            return ""
        return re.sub(r"\S*check\.ts", "check.ts", run.stdout + run.stderr)


def main(argv: list[str]) -> int:
    pairs = [arg.split("=", 1) for arg in argv]
    docs = [(view, json.loads(Path(path).read_text())) for view, path in pairs]
    for exact in (False, True):
        errors = check(docs, exact=exact)
        label = "exact types.ts" if exact else "types.ts with model-pending fields n/d"
        print(f"{label}: {'OK' if not errors else 'MISMATCH'} ({len(docs)} documents)")
        if errors:
            kinds = Counter(re.sub(r"^check\.ts\(\d+,\d+\): ", "", line) for line in errors.splitlines() if line)
            for message, n in kinds.most_common(20):
                print(f"  {n} x {message}")
    return 0 if not check(docs) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
