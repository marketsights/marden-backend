"""render.yaml (the first deploy's host while Railway builds are paused) agrees with the code it deploys.

- The worker's shutdown wait covers the worker's own graceful timeout, as railway.json's drainingSeconds does.
- Every variable the worker needs to do real work in production is declared on core-worker (a missing one
  would make that feature report "not configured" for good, with nothing in the Blueprint to fill in).
- The API verifies the unsubscribe links with the worker's ALERT_LINK_SECRET, and nothing is a literal secret.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from marden.worker import DEFAULT_SHUTDOWN_TIMEOUT_S

ROOT = Path(__file__).resolve().parents[2]
BLUEPRINT = yaml.safe_load((ROOT / "render.yaml").read_text())
SERVICES = {s["name"]: s for s in BLUEPRINT["services"]}

WORKER_NEEDS = {
    "DATABASE_URL", "DATABASE_URL_UNPOOLED",  # pooled for queries, direct for the queue's LISTEN and migrations
    "ANTHROPIC_API_KEY",  # the agents
    "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",  # raw files, uploads, backups
    "ALERT_FROM_EMAIL", "ALERT_LINK_SECRET", "MARDEN_APP_URL",  # alert email (plus a provider key)
    "FRED_API_KEY", "ERCOT_API_KEY", "ERCOT_API_USERNAME", "ERCOT_API_PASSWORD",  # collectors
}


def env_keys(service: str) -> dict[str, dict]:
    return {e["key"]: e for e in SERVICES[service].get("envVars", [])}


def test_the_worker_gets_longer_to_stop_than_its_jobs_get_to_finish():
    railway = json.loads((ROOT / "services" / "railway.json").read_text())
    assert SERVICES["core-worker"]["maxShutdownDelaySeconds"] > DEFAULT_SHUTDOWN_TIMEOUT_S
    assert SERVICES["core-worker"]["maxShutdownDelaySeconds"] <= 300  # Render's maximum
    assert railway["deploy"]["drainingSeconds"] > DEFAULT_SHUTDOWN_TIMEOUT_S


def test_the_worker_declares_every_variable_it_needs():
    keys = env_keys("core-worker")
    assert set(keys) >= WORKER_NEEDS, sorted(WORKER_NEEDS - set(keys))
    assert {"RESEND_API_KEY", "POSTMARK_TOKEN"} & set(keys)  # an email provider


def test_secrets_are_asked_for_or_generated_never_written_here():
    for service in SERVICES.values():
        for e in service.get("envVars", []):
            if any(word in e["key"] for word in ("SECRET", "KEY", "TOKEN", "PASSWORD", "DATABASE_URL")):
                assert "value" not in e, (service["name"], e["key"])


def test_the_api_verifies_unsubscribe_links_with_the_workers_secret():
    link = env_keys("core-api")["ALERT_LINK_SECRET"]
    assert link["fromService"] == {"type": "worker", "name": "core-worker", "envVarKey": "ALERT_LINK_SECRET"}
    assert env_keys("core-worker")["ALERT_LINK_SECRET"].get("generateValue") is True
