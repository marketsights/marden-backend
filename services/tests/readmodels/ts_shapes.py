"""Check read-model documents against handoff/api/types.ts with the TypeScript compiler.

Each document is written as an object literal annotated with its response type, so tsc --strict
applies excess-property checks, literal unions (Tag, ChainStatus, ...) and null checks exactly as the
web app's compiler would. `nd` names top-level fields that may be null (n/d) on a type that has no
null, e.g. a chain status before the status model has run. Skips when the API's node_modules (tsc,
@types/geojson) are not installed.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
TYPES = REPO / "handoff" / "api" / "types.ts"
TSC = REPO / "apps" / "api" / "node_modules" / ".bin" / "tsc"
GEOJSON = REPO / "apps" / "api" / "node_modules" / "@types" / "geojson" / "index.d.ts"


def assert_matches_types(type_name: str, docs: list[Any], *, nd: tuple[str, ...] = (), element: bool = False) -> None:
    """Every doc in `docs` is a `type_name` (or, with element=True, an array of them)."""
    if not (TSC.exists() and GEOJSON.exists() and TYPES.exists()):
        pytest.skip("TypeScript toolchain (apps/api/node_modules) not installed")
    with tempfile.TemporaryDirectory(prefix="marden-shape-") as tmp:
        base = Path(tmp)
        target = f"T.{type_name}"
        if nd:
            keys = " | ".join(json.dumps(k) for k in nd)
            nullable = " ".join(f"{k}: T.{type_name}[{json.dumps(k)}] | null;" for k in nd)
            target = f"Omit<T.{type_name}, {keys}> & {{ {nullable} }}"
        lines = [f"import type * as T from {json.dumps(str(TYPES.with_suffix('')))};"]
        lines.append(f"type Doc = {f'({target})[]' if element else target};")
        lines += [f"export const doc{i}: Doc = {json.dumps(doc, ensure_ascii=False)};" for i, doc in enumerate(docs)]
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
        run = subprocess.run([str(TSC), "-p", str(base / "tsconfig.json")], capture_output=True, text=True, timeout=180)
        assert run.returncode == 0, f"{type_name} does not match types.ts:\n{run.stdout[:4000]}{run.stderr[:2000]}"
