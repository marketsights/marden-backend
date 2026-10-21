"""The read-model shape check (tests/readmodels/ts_shapes.py: tsc --strict against handoff/api/types.ts),
loaded by path so the model tests can check their outputs against the same types."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("readmodels_ts_shapes",
                                               Path(__file__).resolve().parents[1] / "readmodels" / "ts_shapes.py")
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
assert_matches_types = _MODULE.assert_matches_types
