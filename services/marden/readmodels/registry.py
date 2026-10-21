"""Registry of read-model builders: one per view (screen document).

    @builder("site_detail", keys=all_site_slugs)   # build one key: fn(conn, key) -> doc | None
    def build_site_detail(conn, slug): ...

    @builder("sites", many=True)                    # build the whole view: fn(conn) -> [(key, doc)]
    def build_sites(conn): yield "all", {...}

A builder returns the exact response type from handoff/api/types.ts, or None when the subject
no longer has a document. `event(key, doc)` returns the StreamEvent to emit when the doc changes;
dc_*/plant_* views default to the campus/plant events.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from marden.core.db import Conn

Doc = dict[str, Any]
BuildOne = Callable[[Conn, str], Doc | None]
BuildMany = Callable[[Conn], Iterable[tuple[str, Doc]]]
KeysFn = Callable[[Conn], Iterable[str]]
EventFn = Callable[[str, Doc], dict[str, Any] | None]


@dataclass(frozen=True)
class Builder:
    view: str
    build_one: BuildOne | None = None
    build_many: BuildMany | None = None
    keys: KeysFn | tuple[str, ...] | None = None
    event: EventFn | None = None

    def all_keys(self, conn: Conn) -> list[str]:
        if self.keys is None:
            raise ValueError(f"builder {self.view!r} has no key list")
        return list(self.keys) if isinstance(self.keys, tuple) else list(self.keys(conn))

    def event_for(self, key: str, doc: Doc) -> dict[str, Any] | None:
        fn = self.event or DEFAULT_EVENTS.get(self.view)
        return fn(key, doc) if fn else None


def _campus_event(key: str, doc: Doc) -> dict[str, Any]:
    return {"type": "campus", "campusId": key}


def _plant_event(key: str, doc: Doc) -> dict[str, Any]:
    return {"type": "plant", "plantId": key}


DEFAULT_EVENTS: dict[str, EventFn] = {
    "dc_row": _campus_event,
    "dc_detail": _campus_event,
    "plant_row": _plant_event,
    "plant_detail": _plant_event,
}

_BUILDERS: dict[str, Builder] = {}


def register(b: Builder) -> Builder:
    if (b.build_one is None) == (b.build_many is None):
        raise ValueError(f"builder {b.view!r} needs exactly one of build_one / build_many")
    if b.build_one is not None and b.keys is None:
        raise ValueError(f"per-key builder {b.view!r} needs keys (a function or e.g. ('all',))")
    existing = _BUILDERS.get(b.view)
    if existing is not None and existing != b:
        raise ValueError(f"view {b.view!r} already has a builder")
    _BUILDERS[b.view] = b
    return b


def builder(
    view: str,
    *,
    many: bool = False,
    keys: KeysFn | Iterable[str] | None = None,
    event: EventFn | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a builder function for `view` (see module docstring)."""
    key_spec: KeysFn | tuple[str, ...] | None
    key_spec = keys if keys is None or callable(keys) else tuple(keys)

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if many:
            register(Builder(view, build_many=fn, keys=key_spec, event=event))
        else:
            register(Builder(view, build_one=fn, keys=key_spec, event=event))
        return fn

    return decorate


def get_builder(view: str) -> Builder | None:
    return _BUILDERS.get(view)


def registered_views() -> list[str]:
    return sorted(_BUILDERS)


def unregister(view: str) -> None:
    _BUILDERS.pop(view, None)
