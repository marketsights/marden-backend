"""Dependency rules of the pipeline view (readmodels/views/pipeline.py), installed when `marden.readmodels.deps` is
imported (see its last lines). One document (key 'all') reads:

- every `pipeline_project` fact (announced projects, seed/pipeline_2026_10);
- every campus fact (status, developer, tenant, MW, TDLR registration, large-load request) and the building
  facts that date a construction start or a state registration;
- the linked sites' large-load requests, land and air permits, and the serving plants' air permits;
- the BPA and NYISO queue entries' request facts (the load requests it lists) and their campus links.
"""

from __future__ import annotations

from marden.core.db import Conn
from marden.readmodels.deps import depends, singleton

QUEUE_FIELDS = ("status", "mw_requested", "queue_date", "ia_date", "developer", "end_use", "name", "linked_campus")


def _load_queue(conn: Conn, subject_id: str) -> list[str]:
    """Key 'all' for an entry of the queues the page lists load requests from (BPA, NYISO)."""
    return ["all"] if subject_id.startswith(("bpa:", "nyiso:")) else []


def _install_deps() -> None:
    depends("pipeline_project", "", "pipeline", singleton)
    depends("campus", "", "pipeline", singleton)
    for field in ("construction_start", "state_registration"):
        depends("building", field, "pipeline", singleton)
    for field in ("large_load_request", "land", "air_permits"):
        depends("site", field, "pipeline", singleton)
    depends("plant", "air_permit", "pipeline", singleton)  # air_permit and air_permits
    for field in QUEUE_FIELDS:
        depends("queue_project", field, "pipeline", _load_queue)


_install_deps()
