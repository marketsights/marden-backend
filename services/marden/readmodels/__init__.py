"""Read models: one precomputed document per screen piece (ARCHITECTURE "Read models").

Builders live in modules of this package (one per view) and register with `@builder(view)`;
`marden.plugins.load_plugins()` imports them. Framework:

- registry: `builder`, `Builder`, `get_builder`, `registered_views`
- deps: `depends`, `mark_dirty`, `mark_dirty_for`, key functions `same`, `singleton`, `via`
- rebuild: `rebuild_dirty`, `rebuild_all`, `rebuild_every_view`, `upsert_doc`, `RebuildStats`
"""

from marden.readmodels.deps import depends, mark_dirty, mark_dirty_for, same, singleton, via
from marden.readmodels.rebuild import (
    RebuildStats,
    business_today,
    rebuild_all,
    rebuild_dirty,
    rebuild_every_view,
    upsert_doc,
)
from marden.readmodels.registry import Builder, builder, get_builder, registered_views

__all__ = [
    "Builder",
    "RebuildStats",
    "builder",
    "business_today",
    "depends",
    "get_builder",
    "mark_dirty",
    "mark_dirty_for",
    "rebuild_all",
    "rebuild_dirty",
    "rebuild_every_view",
    "registered_views",
    "same",
    "singleton",
    "upsert_doc",
    "via",
]
