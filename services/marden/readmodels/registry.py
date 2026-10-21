"""Registry of read-model builders: one per view (screen document).

    @builder("site_detail", keys=all_site_slugs)   # build one key: fn(conn, key) -> doc | None
    def build_site_detail(conn, slug): ...

    @builder("sites", many=True)                    # build the whole view: fn(conn) -> [(key, doc)]
    def build_sites(conn): yield "all", {...}

A builder returns the exact response type from handoff/api/types.ts, or None when the subject
no longer has a document. `event(key, doc)` returns the StreamEvent to emit when the doc changes;
dc_*/plant_* views default to the campus/plant events, and a view may add companion events that
carry another value the doc holds (site_detail: the site's `cushion`; instrument_detail: a
`mark` per holder whose last mark changed since the previous snapshot). Views must be in `views.VIEWS`.

Builders read facts with `marden.readmodels.known_facts` (fact_current for today's snapshot,
fact_asof(day) when a past day is backfilled) and date every other read with `build_date()`;
they never read the `fact` table directly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from marden.core import events
from marden.core.db import Conn
from marden.readmodels.views import VIEWS, check_view

Doc = dict[str, Any]
BuildOne = Callable[[Conn, str], Doc | None]
BuildMany = Callable[[Conn], Iterable[tuple[str, Doc]]]
KeysFn = Callable[[Conn], Iterable[str]]
EventFn = Callable[[str, Doc], dict[str, Any] | None]
# Companion events that compare with the previous snapshot of the same key (None: first build).
DiffEventFn = Callable[[str, Doc | None, Doc], list[dict[str, Any]]]


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

    @property
    def compares_previous(self) -> bool:
        """Whether events_for needs the previous snapshot of the key."""
        return self.view in DIFF_EVENTS

    def events_for(self, key: str, doc: Doc, previous: Doc | None = None) -> list[dict[str, Any]]:
        """The StreamEvents to emit when this doc changed: the view's own event, then its companions."""
        out: list[dict[str, Any]] = []
        for fn in (self.event or DEFAULT_EVENTS.get(self.view), COMPANION_EVENTS.get(self.view)):
            payload = fn(key, doc) if fn else None
            if payload is not None:
                out.append(payload)
        diff = DIFF_EVENTS.get(self.view)
        if diff is not None:
            out.extend(diff(key, previous, doc))
        return out


def _campus_event(key: str, doc: Doc) -> dict[str, Any]:
    return {"type": "campus", "campusId": key}


def _plant_event(key: str, doc: Doc) -> dict[str, Any]:
    return {"type": "plant", "plantId": key}


DEFAULT_EVENTS: dict[str, EventFn] = {
    **{view: _campus_event for view in VIEWS if view.startswith(("dc_row", "dc_detail", "dc_tab:"))},
    **{view: _plant_event for view in VIEWS if view.startswith("plant_")},
}


def _cushion_event(key: str, doc: Doc) -> dict[str, Any] | None:
    """types.ts StreamEvent 'cushion' from a site page (SiteDetail.cushionMonths), so open screens
    show the new cushion without a request. None when the site has no cushion value to send (no
    lease date, or a model value without its method version)."""
    cushion = doc.get("cushionMonths")
    if not isinstance(cushion, dict):
        return None
    payload = {"type": "cushion", "siteSlug": key, "cushionMonths": cushion}
    try:
        return events.validate(payload)
    except events.InvalidStreamEvent:
        return None


COMPANION_EVENTS: dict[str, EventFn] = {"site_detail": _cushion_event}


def _holder_rows(doc: Doc | None) -> list[dict[str, Any]]:
    holders = doc.get("holders") if isinstance(doc, dict) else None
    rows = holders.get("rows") if isinstance(holders, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get("name"), str)]


def _mark_events(key: str, previous: Doc | None, doc: Doc) -> list[dict[str, Any]]:
    """types.ts StreamEvent 'mark' for each holder whose filed last mark changed since the previous
    snapshot (Holder.lastMark, from that holder's filing). None on a first build, and none for a holder
    row the previous snapshot did not have (rows appearing once the mark gap is priced): no screen
    holds that mark, and the web app patches existing rows only."""
    if previous is None:
        return []
    before = {r["name"]: r.get("lastMark") for r in _holder_rows(previous)}
    out: list[dict[str, Any]] = []
    for row in _holder_rows(doc):
        mark = row.get("lastMark")
        if row["name"] not in before or before[row["name"]] == mark:
            continue
        payload = {
            "type": "mark", "instrumentId": key, "holder": row["name"],
            "mark": {"value": mark, "tag": "F", "asOf": row.get("filingDate"),
                     "sourceName": row.get("filing"), "sourceUrl": row.get("filingUrl")},
        }
        try:
            out.append(events.validate(payload))
        except events.InvalidStreamEvent:
            continue
    return out


DIFF_EVENTS: dict[str, DiffEventFn] = {"instrument_detail": _mark_events}

_BUILDERS: dict[str, Builder] = {}


def register(b: Builder) -> Builder:
    check_view(b.view)
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
