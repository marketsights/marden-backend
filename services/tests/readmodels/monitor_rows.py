"""Copy the real rows behind the monitor views (home, risk_list, track_record, gpus, gpu_model,
gpu_deployment) from the local `marden` database into the test database.

Home reads every site and instrument as the list screens do, so this starts from site_rows.copy_sites
(sites, parties, instruments, facts, site events, prices, holdings, site and instrument model outputs)
and adds what only the monitor views read: events on instruments (filings and scheduled steps), the
model outputs for subjects other than sites and instruments (the GPU index, the GPU model pages, the
delivered-vs-energized page, the backtest and the risk list when they exist) and the model runs (the
Track record's models table). Copied as stored, not hand-made; document and run references cleared.
"""

from __future__ import annotations

from typing import Any

import psycopg
from conftest import BASE_URL, TEST_URL
from real_rows import _copy, _reset_sequence
from site_rows import copy_sites

MONITOR_MODELS = ["gpu_index", "gpu_model", "gpu_deployment", "track_record", "risk_list", "curve"]


def copy_monitor() -> list[str]:
    """Copy the site area's rows plus the monitor views' own inputs. Returns the site slugs."""
    slugs = copy_sites()
    with psycopg.connect(BASE_URL, connect_timeout=5) as src, psycopg.connect(TEST_URL) as dst:
        p: dict[str, Any] = {"models": MONITOR_MODELS}
        _copy(src, dst, "event", "subject_type = 'instrument'", p, nulls=("document_id", "fact_id"))
        _copy(src, dst, "model_output", "subject_type NOT IN ('site', 'instrument') AND model = ANY(%(models)s)", p,
              nulls=("run_id",))
        _copy(src, dst, "model_run", "true", p)
        for table in ("event", "model_output", "model_run"):
            _reset_sequence(dst, table)
        dst.commit()
    return slugs
