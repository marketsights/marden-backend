"""Shared pieces of the monitor views (home, risk_list, track_record, gpus, gpu_model, gpu_deployment).

The monitor views mostly carry model outputs to the screen. These helpers check each piece against
its handoff/api/types.ts shape and keep only the type's keys: a Sourced<T> that a model already
sourced passes through (missing as-of and method version filled from the output row), a bare value
becomes tag M under the output's method version, and a missing or ill-typed value is n/d (value
null), never a placeholder. Every read is dated by the snapshot being built (`build_date()`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.views.instrument_inputs import day, model_sourced, nd, num

Doc = dict[str, Any]
TAGS = ("F", "R", "M")
SEVERITIES = ("red", "amber", "green", "none")
TAG_RANK = {"F": 0, "R": 1, "M": 2}


def is_iso_date(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


PARTIAL_DATE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2]|H[12]|Q[1-4])(-\d{2})?)?$")


def is_record_date(value: Any) -> bool:
    """An Evidence date: a day, or the year, half, quarter or month a record gives when that is all it
    states ('2026', '2026-H2', '2026-Q3', '2026-10')."""
    if not isinstance(value, str) or not PARTIAL_DATE.match(value):
        return False
    return len(value) < 10 or is_iso_date(value)


def week_end(start: str) -> str:
    """The Sunday that closes the week starting `start` (a risk-list week runs Monday to Sunday)."""
    return (date.fromisoformat(start) + timedelta(days=6)).isoformat()


# ---------------------------------------------------------------- model outputs
def latest_output_any(conn: Conn, model: str, subject_id: str) -> Row | None:
    """The newest output of `model` for the subject (any subject_type) dated on or before the build day."""
    return conn.execute(
        """SELECT id, model, version, subject_type, subject_id, as_of, output, created_at FROM model_output
            WHERE model = %s AND subject_id = %s AND as_of <= %s
            ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1""",
        (model, subject_id, day()),
    ).fetchone()


def output_subjects(conn: Conn, model: str) -> list[str]:
    """Subjects `model` had written an output for by the build day."""
    rows = conn.execute(
        "SELECT DISTINCT subject_id FROM model_output WHERE model = %s AND as_of <= %s ORDER BY 1", (model, day())
    ).fetchall()
    return [r["subject_id"] for r in rows]


def method_of(out: Row) -> str:
    """The output's method label: 'gpu_model' + '0.1' -> 'gpu_model v0.1'."""
    version = str(out["version"])
    if version[:1].isdigit():
        return f"{out['model']} v{version}"
    return version if out["model"] in version else f"{out['model']} {version}"


# ---------------------------------------------------------------- Sourced<T>
def checked(value: Any, kind: str) -> bool:
    if value is None:
        return True
    if kind == "number":
        return num(value) is not None
    if kind == "string":
        return isinstance(value, str)
    return True


def sourced_of(out: Row | None, raw: Any, kind: str = "number", tag: str = "M") -> Doc:
    """Sourced<number|string> from a model output value; n/d (value null) when it is missing or not
    of the kind. Keys outside Sourced<T> are dropped."""
    got = model_sourced(out, raw) if out is not None else None
    if got is None or got.get("tag") not in TAGS or not isinstance(got.get("asOf"), str) or not checked(
        got["value"], kind
    ):
        missing = nd(got["tag"] if got and got.get("tag") in TAGS else tag)
        if got is not None and got.get("methodVersion"):
            missing["methodVersion"] = got["methodVersion"]
        elif out is not None and missing["tag"] == "M":
            missing["methodVersion"] = method_of(out)
        return missing
    if kind == "number" and got["value"] is not None:
        got["value"] = num(got["value"])
    return got


def plain_sourced(raw: Any, kind: str = "number") -> Doc | None:
    """A Sourced<T> already in a document (a fact or model value carried as is), or None."""
    if not isinstance(raw, dict) or "value" not in raw or raw.get("tag") not in TAGS:
        return None
    if not isinstance(raw.get("asOf"), str) or not checked(raw["value"], kind):
        return None
    out = {k: raw[k] for k in ("value", "tag", "sourceUrl", "sourceName", "asOf", "methodVersion")
           if k in raw and (raw[k] is not None or k == "value")}
    if kind == "number" and out["value"] is not None:
        out["value"] = num(out["value"])
    return out


def evidence_of(raw: Any) -> Doc | None:
    """types.ts Evidence (date, text, tag, sourceName, sourceUrl), or None when it lacks a dated text."""
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not raw["text"]:
        return None
    if not is_record_date(raw.get("date")) or raw.get("tag") not in TAGS:
        return None
    return {"date": raw["date"], "text": raw["text"], "tag": raw["tag"],
            "sourceName": raw.get("sourceName") if isinstance(raw.get("sourceName"), str) else "",
            "sourceUrl": raw.get("sourceUrl") if isinstance(raw.get("sourceUrl"), str) else ""}


def items_of(raw: Any, check: Callable[[Any], Doc | None]) -> list[Doc]:
    """The list items that pass `check` (anything else in the list is dropped)."""
    return [got for item in raw if (got := check(item)) is not None] if isinstance(raw, list) else []


def weakest_tag(tags: Iterable[str]) -> str:
    return max(tags, key=lambda t: TAG_RANK.get(t, 2), default="M")


def sum_sourced(values: list[Doc]) -> Doc | None:
    """The sum of Sourced numbers: the weakest tag, the newest as-of, every source named once."""
    known = [v for v in values if num(v.get("value")) is not None]
    if not known:
        return None
    if len(known) == 1:
        return dict(known[0])
    names = sorted({v["sourceName"] for v in known if v.get("sourceName")})
    out: Doc = {"value": round(sum(float(v["value"]) for v in known), 4), "tag": weakest_tag(v["tag"] for v in known),
                "asOf": max(v["asOf"] for v in known)}
    if names:
        out["sourceName"] = " + ".join(names)
    return out


# ---------------------------------------------------------------- dates on screen
def when_label(when: date, today: date) -> str:
    """'Late Nov', 'Mid-Dec', 'Early Jan '27' for the next six months; 'H2 '27' further out (spec 01)."""
    ahead = (when.year - today.year) * 12 + when.month - today.month
    year = f" '{when.year % 100:02d}"
    if ahead > 5:
        return f"H{1 if when.month <= 6 else 2}{year}"
    month = when.strftime("%b") + ("" if when.year == today.year else year)
    if when.day <= 10:
        return f"Early {month}"
    return f"Mid-{month}" if when.day <= 20 else f"Late {month}"
