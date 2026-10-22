"""Every parameter set the Stress screen offers, computed ahead (stress v0.2), so a slider or dropdown change is
answered by the SQL function stress_run(jsonb) from a stored output instead of a queued model run.

The screen's choices (marden-front-end src/components/stress/scenario-params.ts): end user = a payer that is an AI
lab, cutPct 0..100 in steps of 5; tenant = any other payer, rating one of RATINGS; power delay 0..24 months; each at
base and severe. A request with other parameters (or limited to chosen instruments) is still queued as before.

Each output is stored under the subject stress_run gives that request ('custom:' || md5 of the normalized request,
computed by Postgres itself so the keys match), as of today; stress_run serves stored custom results of today.
An output identical to the one already stored today is not written again (a triggered re-run changes only what
moved). Book.warm() first computes every instrument's shock-free price state, so each request only applies its
shock (well under a second each).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Any

from marden.core.db import Conn
from marden.core.jsonutil import canonical_json, to_jsonable
from marden.models import runs
from marden.models.sourced import Batch, run_outputs
from marden.models.stress import MODEL, VERSION, run_request
from marden.models.stress_price import Book, cached_book
from marden.models.stress_scenarios import DEFAULTS
from marden.readmodels.context import business_today
from marden.readmodels.views.payers_common import payer_ids

log = logging.getLogger(__name__)

RATINGS = ("BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+")
CUT_PCTS = tuple(range(0, 101, 5))
DELAY_MONTHS = tuple(range(0, 25))
SEVERITIES = ("base", "severe")


def grid_requests(conn: Conn) -> list[dict[str, Any]]:
    """The screen's parameter sets, the saved defaults left out (stress_run serves those from the nightly run)."""
    ids = payer_ids(conn)
    kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM entity WHERE id = ANY(%s)", (ids,))}
    labs = sorted(i for i in ids if kinds.get(i) == "ai_lab")
    others = sorted(i for i in ids if kinds.get(i) != "ai_lab")
    params: list[tuple[str, dict[str, Any]]] = [
        *(("end_user_funding_miss", {"endUser": e, "cutPct": c}) for e in labs for c in CUT_PCTS),
        *(("tenant_downgrade", {"tenant": t, "rating": r}) for t in others for r in RATINGS),
        *(("power_delay", {"months": m}) for m in DELAY_MONTHS),
    ]
    return [{"scenario": s, "params": p, "severity": sev} for s, p in params for sev in SEVERITIES if p != DEFAULTS[s]]


def subject_of(conn: Conn, request: dict[str, Any]) -> str:
    """The subject stress_run(jsonb) keys this request by (its own normalization and md5)."""
    row = conn.execute(
        "SELECT 'custom:' || md5(jsonb_build_object('scenario', %s::text, 'params', %s::jsonb, "
        "'severity', %s::text)::text) AS s",
        (request["scenario"], json.dumps(request["params"]), request["severity"]),
    ).fetchone()
    return str(row["s"])


def compute(conn: Conn, day: date, book: Book) -> Batch:
    t0 = time.time()
    warmed = book.warm()
    batch, skipped = Batch(), 0
    for req in grid_requests(conn):
        subject = subject_of(conn, req)
        try:
            out = run_request(conn, book, req)
        except (KeyError, ValueError) as exc:  # a party without the inputs a scenario needs: left to the queue
            log.info("stress grid %s: %s", req, exc)
            continue
        prev = runs.latest_output(conn, MODEL, "scenario", subject, day)
        if (
            prev is not None
            and prev["as_of"] == day
            and canonical_json(prev["output"]) == canonical_json(to_jsonable(out))
        ):
            skipped += 1
            continue
        batch.add("scenario", subject, out)
    log.info(
        "stress grid: %d instruments warmed, %d written, %d unchanged in %.0fs",
        warmed,
        len(batch.outputs),
        skipped,
        time.time() - t0,
    )
    return batch


def run(trigger: str = "grid") -> dict[str, Any]:
    """Compute and store today's grid with the process's cached book (the nightly and triggered runs call it)."""
    day = business_today()

    def go(conn: Conn, d: date) -> Batch:
        return compute(conn, d, cached_book(conn, d))

    out = run_outputs(MODEL, VERSION, day, go, trigger=trigger, mark=False)
    return {"written": len(out["outputs"]), "run_id": out["run_id"]}
