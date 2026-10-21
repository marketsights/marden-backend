"""The read-model views the API serves: the view/key contract (docs/ARCHITECTURE.md "Read models").

The API reads `read_model(view, key)` with exactly these names; builders, dependency rules and
dirty marks may only use them, so a typo fails at import time instead of leaving a screen at 503.
Key kinds: `all` (one document), `<table>.<column>` (that identity row's key), `week` (the
Monday a risk list was published, YYYY-MM-DD), `gpu_model` (the `:model` path segment).

The builders live in this package's modules, one area each (instruments.py, instrument_detail.py,
curve.py, gpu_collateral.py, sites*.py, ...), and are imported by `marden.plugins.load_plugins()`.
"""

from __future__ import annotations

SITE_TABS = ("evidence", "power", "opposition", "events", "debt", "lease", "buildings")
CAMPUS_TABS = ("buildings", "records", "history", "capacity")

SITE = "site.slug"
INSTRUMENT = "instrument.id"
CAMPUS = "campus.id"
PLANT = "power_plant.id"
ENTITY = "entity.id"

VIEWS: dict[str, str] = {
    "home": "all",
    "completion": "all",
    "sites": "all",
    "site_detail": SITE,
    "site_schedule": SITE,
    **{f"site_tab:{tab}": SITE for tab in SITE_TABS},
    "project_file": SITE,
    "instruments": "all",
    "curve": "all",
    "instrument_detail": INSTRUMENT,
    "gpu_collateral": INSTRUMENT,
    "payers": "all",
    "payer": ENTITY,
    "payer_capacity": ENTITY,
    "risk_list": "week",
    "track_record": "all",
    "dc_row": CAMPUS,
    "dc_detail": CAMPUS,
    **{f"dc_tab:{tab}": CAMPUS for tab in CAMPUS_TABS},
    "plant_row": PLANT,
    "plant_detail": PLANT,
    "plant_debt": PLANT,
    "obligations": "all",
    "obligation_company": ENTITY,
    "gpus": "all",
    "gpu_deployment": "all",
    "gpu_model": "gpu_model",
    "leases": "all",
}

# Rebuilt by their own model or collector task (which calls mark_dirty), not by fact dependencies:
# no fact subject carries their inputs (GPU rental indexes) or they are dated outputs (risk list,
# backtest). See deps.py for every other view.
SCHEDULED_VIEWS = frozenset({"risk_list", "track_record", "gpus", "gpu_model"})


class UnknownView(ValueError):
    pass


def check_view(view: str) -> str:
    if view not in VIEWS:
        raise UnknownView(
            f"unknown read-model view {view!r}: add it to marden.readmodels.views.VIEWS, the API and the "
            "view/key table in docs/ARCHITECTURE.md together"
        )
    return view
