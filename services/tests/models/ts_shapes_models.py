"""The read-model shape check (tests/readmodels/ts_shapes.py: tsc --strict against handoff/api/types.ts),
loaded by path so the model tests can check their outputs against the same types.

`assert_matches_ts(expr, docs, prelude)` checks documents against any TypeScript type expression over the
handoff types (`T.*`) and the API's own contracts (`A.*`, apps/api/src/contracts/app.ts), e.g.
`Pick<T.SiteSummary, 'chain' | 'status'> & { missing: string[] }`, so a model output that is a piece of
a types.ts interface is compiled against exactly that piece (excess properties, literal unions, nulls).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

_READMODEL_TESTS = Path(__file__).resolve().parents[1] / "readmodels"
if str(_READMODEL_TESTS) not in sys.path:  # ts_shapes imports its sibling `sourcing` (tests/models run alone)
    sys.path.append(str(_READMODEL_TESTS))
_SPEC = importlib.util.spec_from_file_location("readmodels_ts_shapes", _READMODEL_TESTS / "ts_shapes.py")
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
assert_matches_types = _MODULE.assert_matches_types

APP_TYPES = _MODULE.REPO / "apps" / "api" / "src" / "contracts" / "app.ts"
# n/d helpers for fields types.ts declares non-null (00-global data rule 3: n/d is null, never 0)
ND_PRELUDE = """
type ND<V> = V | null;
type NDFields<O, K extends keyof O> = Omit<O, K> & { [P in K]: O[P] | null };
"""


def assert_matches_ts(expr: str, docs: list[Any], *, prelude: str = "", element: bool = False) -> None:
    """Every doc is a value of the TypeScript type `expr` (or, with element=True, an array of them)."""
    _MODULE.assert_sourced(docs)
    tsc, geojson, types = _MODULE.TSC, _MODULE.GEOJSON, _MODULE.TYPES
    if not (tsc.exists() and geojson.exists() and types.exists() and APP_TYPES.exists()):
        pytest.skip("TypeScript toolchain (apps/api/node_modules) not installed")
    with tempfile.TemporaryDirectory(prefix="marden-model-shape-") as tmp:
        base = Path(tmp)
        lines = [
            f"import type * as T from {json.dumps(str(types.with_suffix('')))};",
            f"import type * as A from {json.dumps(str(APP_TYPES.with_suffix('')))};",
            ND_PRELUDE,
            prelude,
            f"type Doc = {f'({expr})[]' if element else expr};",
        ]
        lines += [f"export const doc{i}: Doc = {json.dumps(doc, ensure_ascii=False)};" for i, doc in enumerate(docs)]
        (base / "check.ts").write_text("\n".join(lines) + "\n")
        (base / "tsconfig.json").write_text(json.dumps({
            "compilerOptions": {
                "strict": True, "noEmit": True, "skipLibCheck": True, "module": "ESNext",
                "moduleResolution": "Bundler", "target": "ES2022", "types": [],
                "paths": {"geojson": [str(geojson)]},
            },
            "files": ["check.ts"],
        }))
        run = subprocess.run([str(tsc), "-p", str(base / "tsconfig.json")], capture_output=True, text=True,
                             timeout=300)
        assert run.returncode == 0, f"{expr} does not match:\n{run.stdout[:6000]}{run.stderr[:2000]}"
