"""Dependency rules of the moves and event-effect views (readmodels/views/moves.py, debt_effects.py), installed when
`marden.readmodels.deps` is imported (see its last lines).

- `moves` (all) and `instrument_moves` (instrument): the `moves` model's output per instrument; `moves` also shows
  instrument names, types and sizes.
- `instrument_moves` also lists the priced changes of its site and of the sites whose tenant or end user issued
  it (the `event_impact` output on those sites).
- `site_debt_effects` (site): the `event_impact` output on the site, and its events (a new record gets its line).
"""

from __future__ import annotations

from marden.readmodels.deps import depends, singleton, via

_SITE_INSTRUMENTS = via("SELECT id FROM instrument WHERE site_slug = %s")
_RELATED_BONDS = via(
    """SELECT DISTINCT i.id FROM chain_party cp JOIN entity e ON e.id = cp.entity_id OR e.parent_id = cp.entity_id
         JOIN instrument i ON i.issuer_entity_id = e.id
        WHERE cp.site_slug = %s AND cp.role IN ('tenant', 'end_user') AND i.in_v1 AND i.kind = 'bond'""")


def _install_deps() -> None:
    depends("instrument", "@model:moves", "moves", singleton)
    depends("instrument", "@model:moves", "instrument_moves")
    depends("site", "@model:event_impact", "instrument_moves", _SITE_INSTRUMENTS)
    depends("site", "@model:event_impact", "instrument_moves", _RELATED_BONDS)
    depends("site", "@model:event_impact", "site_debt_effects")
    depends("site", "@event", "site_debt_effects")  # a new record on the tabs gets its line at once
    depends("instrument", "", "moves", singleton)  # names, types, sizes


_install_deps()
