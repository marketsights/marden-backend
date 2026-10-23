"""Dependency rules of the Delivery record and Mispriced views (readmodels/views/delivery.py, mispriced.py),
installed when `marden.readmodels.deps` is imported (see its last lines).

- `delivery` (all): any site fact, record or model output (status, cushion, event impact; the Completion and
  Schedule documents it reads are rebuilt on the same marks), building facts (state registrations, completion),
  instrument identity (issuers) and chain parties (entities).
- `mispriced` (all): any instrument fact, price or model output (price, yield, mark gap), site records and the
  site's event-impact and cushion outputs, entity records (issuer filings), holdings linked to an instrument
  (fund marks).
"""

from __future__ import annotations

from marden.readmodels.deps import depends, singleton, via

_HOLDING_LINKED = via("SELECT 'all' FROM holding WHERE id = %s AND instrument_id IS NOT NULL", int_id=True)


def _install_deps() -> None:
    for subject in ("site", "building", "instrument", "entity"):
        depends(subject, "", "delivery", singleton)
    for subject in ("instrument", "site", "entity"):
        depends(subject, "", "mispriced", singleton)
    depends("holding", "", "mispriced", _HOLDING_LINKED)  # a fund line not yet linked to an instrument moves nothing


_install_deps()
