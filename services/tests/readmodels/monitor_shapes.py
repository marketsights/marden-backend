"""Check monitor read-model documents and API responses against the API contracts with tsc --strict.

The types are apps/api/src/contracts/types.ts (a verbatim copy of handoff/api/types.ts: RiskList,
GpuModelDetail, GpuDeploymentPage) and app.ts (the shapes the handoff leaves open: HomeDoc and
HomeResponse, TrackRecord, GpuIndexPage, SearchResponse, NotReadyBody). Each document is written as an
object literal annotated with its type, so excess properties, literal unions and null checks apply as
in the app's compiler. Usable from tests (`check`) and on saved API responses:

    python tests/readmodels/monitor_shapes.py A.HomeResponse=home.json T.RiskList=risk.json ...
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from sourcing import sourcing_problems

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "apps" / "api" / "src" / "contracts"
TSC = REPO / "apps" / "api" / "node_modules" / ".bin" / "tsc"
GEOJSON = REPO / "apps" / "api" / "node_modules" / "@types" / "geojson" / "index.d.ts"

VIEW_TYPES = {
    "home": "A.HomeDoc",
    "risk_list": "T.RiskList",
    "track_record": "A.TrackRecord",
    "gpus": "A.GpuIndexPage",
    "gpu_model": "T.GpuModelDetail",
    "gpu_deployment": "T.GpuDeploymentPage",
}


def available() -> bool:
    return TSC.exists() and GEOJSON.exists() and (CONTRACTS / "app.ts").exists()


def check(docs: list[tuple[str, Any]]) -> str:
    """'' when every (type expression, document) pair type-checks, else the compiler's messages (or the
    Sourced values without their source, hard rule 4)."""
    unsourced = [p for typ, doc in docs for p in sourcing_problems(doc, typ)]
    if unsourced:
        return "Sourced values without their source:\n" + "\n".join(unsourced[:40])
    with tempfile.TemporaryDirectory(prefix="marden-monitor-shape-") as tmp:
        base = Path(tmp)
        lines = [f"import type * as T from {json.dumps(str(CONTRACTS / 'types'))};",
                 f"import type * as A from {json.dumps(str(CONTRACTS / 'app'))};"]
        lines += [f"export const doc{i}: {typ} = {json.dumps(doc, ensure_ascii=False)};"
                  for i, (typ, doc) in enumerate(docs)]
        (base / "check.ts").write_text("\n".join(lines) + "\n")
        (base / "tsconfig.json").write_text(json.dumps({
            "compilerOptions": {"strict": True, "noEmit": True, "skipLibCheck": True, "module": "ESNext",
                                "moduleResolution": "Bundler", "target": "ES2022", "types": [],
                                "paths": {"geojson": [str(GEOJSON)]}},
            "files": ["check.ts"],
        }))
        run = subprocess.run([str(TSC), "-p", str(base / "tsconfig.json")], capture_output=True, text=True,
                             timeout=300)
        return "" if run.returncode == 0 else (run.stdout + run.stderr)[:6000]


if __name__ == "__main__":
    pairs = [arg.split("=", 1) for arg in sys.argv[1:]]
    errors = check([(typ, json.loads(Path(path).read_text())) for typ, path in pairs])
    print(errors or f"ok: {len(pairs)} documents match their types")
    sys.exit(1 if errors else 0)
