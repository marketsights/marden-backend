"""Hard rule 4 on built documents: every Sourced<T> that carries a value names its source.

A Sourced value (an object with `value` and `tag`) that is not n/d needs a tag (F/R/M), an as-of date and
a sourceName; an M value also needs its methodVersion. An n/d value (value null) has no source to name.
The area shape checks call this on every document they check, so the rule holds on the real copied rows.
"""

from __future__ import annotations

import re
from typing import Any

DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?")


def sourcing_problems(doc: Any, path: str = "") -> list[str]:
    """'<path>: <problem>' for each Sourced value with a value but no tag, as-of, source or M method."""
    out: list[str] = []
    if isinstance(doc, dict):
        if "value" in doc and "tag" in doc and doc["value"] is not None:
            if doc["tag"] not in ("F", "R", "M"):
                out.append(f"{path}: tag {doc['tag']!r}")
            if not isinstance(doc.get("asOf"), str) or not DATE.match(doc["asOf"]):
                out.append(f"{path}: no asOf")
            if not doc.get("sourceName"):
                out.append(f"{path}: no sourceName")
            if doc["tag"] == "M" and not doc.get("methodVersion"):
                out.append(f"{path}: M value without methodVersion")
        for key, value in doc.items():
            out += sourcing_problems(value, f"{path}/{key}")
    elif isinstance(doc, list):
        for i, value in enumerate(doc):
            out += sourcing_problems(value, f"{path}/{i}")
    return out


def assert_sourced(docs: list[Any]) -> None:
    problems = [p for i, doc in enumerate(docs) for p in sourcing_problems(doc, f"doc{i}")]
    assert not problems, f"{len(problems)} Sourced values without their source:\n" + "\n".join(problems[:40])
