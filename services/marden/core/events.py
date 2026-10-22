"""Live updates: write a `stream_event` row; its trigger sends NOTIFY marden_stream '<id>'.

Payloads are validated against the StreamEvent union in handoff/api/types.ts before writing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime
from typing import Any

from marden.core import db
from marden.core.db import Conn
from marden.core.jsonutil import jsonb, to_jsonable

CHANNEL = "marden_stream"
TAGS = frozenset({"F", "R", "M"})
LINK_ROLES = frozenset({"end_user", "tenant", "developer", "lenders"})
LINK_STATUSES = frozenset({"red", "amber", "green", "none"})
PIPELINE_STATUSES = frozenset({"ok", "delayed", "down"})


class InvalidStreamEvent(ValueError):
    pass


Check = Callable[[Any, str], None]


def _fail(path: str, message: str) -> None:
    raise InvalidStreamEvent(f"{path}: {message}")


def _string(value: Any, path: str) -> None:
    if not isinstance(value, str) or not value:
        _fail(path, "expected a non-empty string")


def _bool(value: Any, path: str) -> None:
    if not isinstance(value, bool):
        _fail(path, "expected a boolean")


def _number(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail(path, "expected a number")


def _iso_date(value: Any, path: str) -> None:
    _string(value, path)
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(path, "expected an ISO date (YYYY-MM-DD)")


def _iso_datetime(value: Any, path: str) -> None:
    _string(value, path)
    try:
        datetime.fromisoformat(value)
    except ValueError:
        _fail(path, "expected an ISO timestamp")


def _one_of(options: frozenset[str]) -> Check:
    def check(value: Any, path: str) -> None:
        if value not in options:
            _fail(path, f"expected one of {sorted(options)}")

    return check


def _obj(required: dict[str, Check], optional: dict[str, Check] | None = None) -> Check:
    optional = optional or {}

    def check(value: Any, path: str) -> None:
        if not isinstance(value, dict):
            _fail(path, "expected an object")
        unknown = set(value) - set(required) - set(optional)
        if unknown:
            _fail(path, f"unknown keys {sorted(unknown)}")
        for key, sub in required.items():
            if key not in value:
                _fail(f"{path}.{key}", "missing")
            sub(value[key], f"{path}.{key}")
        for key, sub in optional.items():
            if key in value:
                sub(value[key], f"{path}.{key}")

    return check


def _list_of(item: Check) -> Check:
    def check(value: Any, path: str) -> None:
        if not isinstance(value, list):
            _fail(path, "expected an array")
        for i, entry in enumerate(value):
            item(entry, f"{path}[{i}]")

    return check


def _sourced(inner: Check) -> Check:
    """Sourced<T>: value T | null, tag, asOf, optional sourceUrl/sourceName/methodVersion."""
    base = _obj(
        {"value": lambda v, p: None if v is None else inner(v, p), "tag": _one_of(TAGS), "asOf": _iso_date},
        {"sourceUrl": _string, "sourceName": _string, "methodVersion": _string},
    )

    def check(value: Any, path: str) -> None:
        base(value, path)
        if value["tag"] == "M" and "methodVersion" not in value:
            _fail(path, "model (M) values need methodVersion")

    return check


_ALERT = _obj(
    {
        "id": _string, "at": _iso_datetime, "headline": _string, "tag": _one_of(TAGS),
        "ruleLabel": _string, "refUrl": _string, "read": _bool, "emailed": _bool,
    }
)
_CHAIN_LINK = _obj(
    {"role": _one_of(LINK_ROLES), "party": _string, "status": _one_of(LINK_STATUSES), "reason": _string},
    {"ruleCode": _string, "fact": _sourced(_string)},
)

SCHEMAS: dict[str, Check] = {
    "alert": _obj({"type": _string, "alert": _ALERT}),
    "status": _obj({"type": _string, "siteSlug": _string, "chain": _list_of(_CHAIN_LINK)}),
    "cushion": _obj({"type": _string, "siteSlug": _string, "cushionMonths": _sourced(_number)}),
    "price": _obj(
        {"type": _string, "instrumentId": _string, "price": _sourced(_number), "change1w": _sourced(_number)}
    ),
    "mark": _obj({"type": _string, "instrumentId": _string, "holder": _string, "mark": _sourced(_number)}),
    "pipeline": _obj(
        {"type": _string, "source": _string, "status": _one_of(PIPELINE_STATUSES), "at": _iso_datetime}
    ),
    "campus": _obj({"type": _string, "campusId": _string}),
    "plant": _obj({"type": _string, "plantId": _string}),
    "home": _obj({"type": _string, "asOf": _iso_date}),
}


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the JSON-normalized payload, or raise InvalidStreamEvent."""
    if not isinstance(payload, dict):
        raise InvalidStreamEvent("payload must be an object")
    normalized = to_jsonable(payload)
    kind = normalized.get("type")
    check = SCHEMAS.get(kind) if isinstance(kind, str) else None
    if check is None:
        raise InvalidStreamEvent(f"unknown stream event type {kind!r}; expected one of {sorted(SCHEMAS)}")
    check(normalized, kind)
    return normalized


def emit(
    payload: dict[str, Any], org_id: str | None = None, *, user_id: str | None = None, conn: Conn | None = None
) -> int:
    """Validate and store a StreamEvent; NOTIFY fires when the transaction commits. Returns the id.

    `org_id` scopes the event to one firm (None = everyone); `user_id` to one member of it (their alert)."""
    if user_id is not None and org_id is None:
        raise InvalidStreamEvent("a user-scoped stream event needs its org_id")
    normalized = validate(payload)
    with db.transaction(conn) as c:
        row = c.execute(
            "INSERT INTO stream_event (org_id, user_id, payload) VALUES (%s, %s, %s) RETURNING id",
            (org_id, user_id, jsonb(normalized)),
        ).fetchone()
    assert row is not None
    return row["id"]


class Listener:
    """LISTEN on marden_stream from a dedicated autocommit connection (registered on creation).

        with Listener() as listener:
            ...
            for event_id in listener.ids(timeout=5):
                ...
    """

    def __init__(self) -> None:
        self.conn = db.connect(autocommit=True)
        self.conn.execute(f"LISTEN {CHANNEL}")

    def ids(self, *, timeout: float | None = None, stop_after: int | None = None) -> Iterator[int]:
        for note in self.conn.notifies(timeout=timeout, stop_after=stop_after):
            if note.channel == CHANNEL:
                yield int(note.payload)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Listener:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
