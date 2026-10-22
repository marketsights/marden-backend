"""Deploy safety (docs/DEPLOY.md): the migrate script, the Railway config, the worker's queues, stalled-job
recovery, the raw store's refusal of local disk on Railway, the worker's shutdown timeout and redacted
JSON logs."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from psycopg import sql
from psycopg.conninfo import make_conninfo

from marden import worker
from marden.core import config, logs, rawstore
from marden.core.config import ConfigError, Settings
from marden.core.queue import QUEUES, app
from marden.plugins import load_plugins

REPO = Path(__file__).resolve().parents[2]
MIGRATE = REPO / "scripts" / "migrate.sh"
MIGRATIONS = REPO / "db" / "migrations"


# ------------------------------------------------------------------ scripts/migrate.sh
@pytest.fixture
def empty_database():
    name = f"marden_migrate_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(BASE_URL, dbname=name)
    finally:
        with psycopg.connect(BASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _as_url(conninfo: str, extra: str = "") -> str:
    d = psycopg.conninfo.conninfo_to_dict(conninfo)
    host, port = d.get("host", "localhost"), d.get("port", 5432)
    return f"postgres://{d['user']}:{d['password']}@{host}:{port}/{d['dbname']}{extra}"


def _migrate(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "DATABASE_URL_UNPOOLED")}
    env["DATABASE_URL_UNPOOLED"] = url
    return subprocess.run(["sh", str(MIGRATE), *args], env=env, capture_output=True, text=True, timeout=300)


def test_migrate_applies_every_migration_to_an_empty_database_once(empty_database):
    # Neon-style URL: channel_binding=require makes dbmate's driver fail unless the script strips it.
    url = _as_url(empty_database, "?sslmode=disable&channel_binding=require")
    first = _migrate(url)
    assert first.returncode == 0, first.stdout + first.stderr
    files = sorted(p.name.split("_", 1)[0] for p in MIGRATIONS.glob("*.sql"))
    with psycopg.connect(empty_database) as c:
        applied = [r[0] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert c.execute("SELECT to_regclass('public.backup_run') IS NOT NULL").fetchone()[0]
    assert applied == files
    assert "password" not in first.stdout + first.stderr and "marden:marden" not in first.stdout + first.stderr

    second = _migrate(url)  # idempotent: nothing pending
    assert second.returncode == 0, second.stderr
    assert "Applying" not in second.stdout
    status = _migrate(url, "status")
    assert status.returncode == 0 and "Pending: 0" in status.stdout


def test_two_concurrent_migrations_wait_for_each_other(empty_database):
    url = _as_url(empty_database, "?sslmode=disable")
    results: list[subprocess.CompletedProcess[str]] = []
    threads = [threading.Thread(target=lambda: results.append(_migrate(url))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r.returncode for r in results] == [0, 0], [r.stderr for r in results]
    applying = [r.stdout.count("Applying:") for r in results]
    assert sorted(applying) == [0, len(list(MIGRATIONS.glob("*.sql")))]  # one did everything, one nothing


def test_migrate_refuses_without_a_database_url():
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "DATABASE_URL_UNPOOLED")}
    out = subprocess.run(["sh", str(MIGRATE)], env=env, capture_output=True, text=True)
    assert out.returncode == 2 and "must be set" in out.stderr


# ------------------------------------------------------------------ Railway config as code
# Keys of Railway's config-as-code schema (https://railway.com/railway.schema.json, fetched 2026-10-21). The
# schema allows unknown keys, so a misspelt one would be silently ignored; this catches it.
RAILWAY_BUILD_KEYS = {"buildCommand", "builder", "dockerfilePath", "nixpacksConfigPath", "nixpacksPlan",
                      "nixpacksVersion", "railpackVersion", "watchPatterns"}
RAILWAY_DEPLOY_KEYS = {"cronSchedule", "drainingSeconds", "healthcheckPath", "healthcheckTimeout", "ipv6EgressEnabled",
                       "limitOverride", "multiRegionConfig", "numReplicas", "overlapSeconds", "preDeployCommand",
                       "preDeployTimeoutSeconds", "region", "registryCredentials", "requiredMountPath",
                       "restartPolicyMaxRetries", "restartPolicyType", "runtime", "sleepApplication", "startCommand"}


def _railway(path: str) -> dict:
    cfg = json.loads((REPO / path).read_text())
    assert set(cfg) <= {"$schema", "build", "deploy"}
    assert set(cfg["build"]) <= RAILWAY_BUILD_KEYS, set(cfg["build"]) - RAILWAY_BUILD_KEYS
    assert set(cfg["deploy"]) <= RAILWAY_DEPLOY_KEYS, set(cfg["deploy"]) - RAILWAY_DEPLOY_KEYS
    return cfg


def test_railway_config_builds_each_image_and_migrates_before_deploy():
    api, wk = _railway("apps/api/railway.json"), _railway("services/railway.json")
    for cfg, dockerfile in ((api, "apps/api/Dockerfile"), (wk, "services/Dockerfile")):
        assert cfg["build"]["builder"] == "DOCKERFILE" and cfg["build"]["dockerfilePath"] == dockerfile
        text = (REPO / dockerfile).read_text()
        # The pre-deploy command exists in the image, migrations ride along, and the process is not root.
        assert cfg["deploy"]["preDeployCommand"] == ["marden-migrate"]
        # dbmate waits up to MARDEN_MIGRATE_LOCK_TIMEOUT (10 min) for a concurrent migration's lock.
        assert cfg["deploy"]["preDeployTimeoutSeconds"] > 600
        assert "scripts/migrate.sh /usr/local/bin/marden-migrate" in text and "COPY db/migrations" in text
        assert "db/migrations/**" in cfg["build"]["watchPatterns"]
        user = [line.split()[1] for line in text.splitlines() if line.startswith("USER ")]
        assert user and user[-1] not in ("root", "0")
        assert cfg["deploy"]["restartPolicyType"] == "ON_FAILURE" and cfg["deploy"]["sleepApplication"] is False
    assert api["deploy"]["healthcheckPath"] == "/healthz"
    assert "healthcheckPath" not in wk["deploy"]  # no HTTP server in the worker
    # The start command is the image's CMD, so `docker run` and Railway run the same thing.
    assert wk["deploy"]["startCommand"] == "python -m marden worker"
    assert 'CMD ["python", "-m", "marden", "worker"]' in (REPO / "services/Dockerfile").read_text()
    # Railway kills after drainingSeconds: the worker must have finished draining by then.
    assert wk["deploy"]["drainingSeconds"] > worker.DEFAULT_SHUTDOWN_TIMEOUT_S


def test_worker_image_installs_the_lockfile_only_when_it_matches_pyproject():
    # `uv sync --frozen` installs a stale lock without complaint (a dependency added to pyproject.toml
    # without `uv lock` would be missing from the image); --locked fails the build instead.
    text = (REPO / "services/Dockerfile").read_text()
    syncs = [line for line in text.splitlines() if line.startswith("RUN") and "uv sync" in line]
    assert len(syncs) == 2 and all("--locked" in s and "--frozen" not in s for s in syncs)
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    out = subprocess.run([uv, "lock", "--check", "--offline"], cwd=REPO / "services", capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-1000:]


def test_every_task_runs_on_a_declared_queue_and_the_worker_takes_all_queues():
    load_plugins()
    queues = {t.queue for name, t in app.tasks.items() if not name.startswith(("builtin:", "procrastinate."))}
    assert queues <= set(QUEUES), queues - set(QUEUES)
    assert {"collect", "models", "readmodels", "alerts", "default"} <= queues
    assert "extract" in QUEUES and "agents" in QUEUES
    # `python -m marden worker` passes no queue list: Procrastinate then works every queue.
    from marden import commands  # noqa: F401  (registers the built-in `worker` command)
    from marden.cli import build_parser

    args = build_parser().parse_args(["worker"])
    assert args.queues is None and args.once is False


# ------------------------------------------------------------------ worker
def _dead_worker(committed) -> int:
    return committed.execute(
        "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (now() - interval '10 minutes') RETURNING id"
    ).fetchone()["id"]


def test_retry_stalled_requeues_jobs_of_a_dead_worker(committed):
    dead = _dead_worker(committed)
    # Its own queueing lock: the worker's periodic deferrer queues a fresh rebuild_dirty run (with the task's
    # queueing lock) as it starts, and that case is the next test's.
    with app.open():
        job_id = worker.rebuild_dirty_task.configure(queueing_lock="test.requeue").defer(timestamp=0)
    committed.execute("UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s", (dead, job_id))
    with app.open():
        worker.retry_stalled_task.defer(timestamp=0)
    worker.run_worker(queues=["default"], concurrency=1, wait=False)
    job = committed.execute("SELECT status, worker_id FROM procrastinate_jobs WHERE id = %s", (job_id,)).fetchone()
    assert job["status"] == "todo" and job["worker_id"] is None  # back in the queue, lock released
    gone = committed.execute("SELECT count(*) AS n FROM procrastinate_workers WHERE id = %s", (dead,)).fetchone()
    assert gone["n"] == 0


def test_retry_stalled_closes_a_stalled_job_whose_next_run_is_already_queued(committed):
    dead = _dead_worker(committed)
    task = worker.rebuild_all_task.configure(queueing_lock="test.retry_stalled")
    with app.open():
        stalled = task.defer(timestamp=0)
    committed.execute("UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s", (dead, stalled))
    with app.open():
        queued = task.defer(timestamp=1)  # allowed: the queueing lock only binds `todo` jobs
        worker.retry_stalled_task.defer(timestamp=0)
    worker.run_worker(queues=["default"], concurrency=1, wait=False)
    found = committed.execute("SELECT id, status FROM procrastinate_jobs WHERE id IN (%s, %s)", (stalled, queued))
    rows = {r["id"]: r["status"] for r in found}
    assert rows == {stalled: "failed", queued: "todo"}  # the stalled one closed, its lock released


def test_retry_stalled_leaves_jobs_of_a_live_worker_alone(committed):
    live = committed.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id").fetchone()["id"]
    with app.open():
        job_id = worker.rebuild_dirty_task.defer(timestamp=0)
    committed.execute("UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s", (live, job_id))
    with app.open():
        worker.retry_stalled_task.defer(timestamp=0)
    worker.run_worker(queues=["default"], concurrency=1, wait=False)
    job = committed.execute("SELECT status FROM procrastinate_jobs WHERE id = %s", (job_id,)).fetchone()
    assert job["status"] == "doing"


def test_shutdown_timeout():
    assert worker.shutdown_timeout({}) == worker.DEFAULT_SHUTDOWN_TIMEOUT_S
    assert worker.shutdown_timeout({"MARDEN_WORKER_SHUTDOWN_TIMEOUT": "30"}) == 30.0
    for bad in ("0", "-5", "soon"):
        with pytest.raises(ConfigError):
            worker.shutdown_timeout({"MARDEN_WORKER_SHUTDOWN_TIMEOUT": bad})


# ------------------------------------------------------------------ raw store on Railway
def test_raw_store_refuses_local_disk_on_a_deployed_service(monkeypatch):
    assert config.deploy_marker({}) is None
    assert config.deploy_marker({"RAILWAY_ENVIRONMENT_NAME": "production"}) == "RAILWAY_ENVIRONMENT_NAME"
    assert config.deploy_marker({"RENDER": "true"}) == "RENDER"
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(rawstore.RawStoreError, match="R2 is not configured"):
        rawstore.get_store(Settings(database_url=TEST_URL))
    with pytest.raises(rawstore.RawStoreError):
        rawstore.put(b"x", source_id="sec_edgar", url="https://www.sec.gov/x", content_type=None)
    r2 = Settings(r2_account_id="acct", r2_access_key_id="id", r2_secret_access_key="secret")
    assert isinstance(rawstore.get_store(r2), rawstore.R2Store)  # configuration only, no request
    monkeypatch.delenv("RAILWAY_ENVIRONMENT")
    rawstore.get_store(config.get_settings())  # restore the process default


# ------------------------------------------------------------------ logs
ENV = {
    "DATABASE_URL": "postgres://app_rw:s3cret-pass@ep-x-pooler.neon.tech/app?sslmode=require",
    "CLERK_SECRET_KEY": "sk_live_abcdefghijklmnop",
    "FRED_API_KEY": "fredkey12345",
    "MARDEN_LOG_FORMAT": "json",
    "SEC_USER_AGENT": "Marden research cody@marden.ai",
}


def test_redaction_masks_env_secrets_url_passwords_and_query_keys():
    secrets = logs.secret_values(ENV)
    assert "s3cret-pass" in secrets and "sk_live_abcdefghijklmnop" in secrets
    assert "Marden research cody@marden.ai" not in secrets  # not a credential
    line = ("connect postgres://app_rw:s3cret-pass@ep-x-pooler.neon.tech/app failed; key sk_live_abcdefghijklmnop; "
            "GET https://api.stlouisfed.org/fred/series?series_id=SOFR&api_key=fredkey12345&file_type=json; "
            "other postgres://u:hunter22@h/db; view=sites key=all")
    out = logs.redact(line, secrets)
    for leaked in ("s3cret-pass", "sk_live_abcdefghijklmnop", "fredkey12345", "hunter22"):
        assert leaked not in out
    assert "app_rw:***@" in out and "api_key=***" in out and "series_id=SOFR" in out
    assert "view=sites key=all" in out  # ordinary log words are left alone


def test_json_formatter_writes_one_redacted_object_per_line():
    formatter = logs.JsonFormatter(logs.secret_values(ENV))
    logger = logging.getLogger("marden.test_deploy")
    record = logger.makeRecord(logger.name, logging.ERROR, __file__, 1, "token %s", ("fredkey12345",), None)
    try:
        raise RuntimeError("boom postgres://app_rw:s3cret-pass@host/app")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()
    doc = json.loads(formatter.format(record))
    assert doc["level"] == "error" and doc["logger"] == "marden.test_deploy" and doc["message"] == "token ***"
    assert "RuntimeError" in doc["exc"] and "s3cret-pass" not in doc["exc"]
    assert doc["ts"].endswith("+00:00")


def test_setup_logging_installs_one_handler_and_leaves_other_setups_alone(monkeypatch):
    root = logging.getLogger()
    saved, saved_level = list(root.handlers), root.level
    try:
        if saved:
            assert logs.setup_logging(ENV) is None  # pytest's capture handlers: left alone, like basicConfig
        for h in list(root.handlers):
            root.removeHandler(h)
        handler = logs.setup_logging(ENV)
        assert handler is not None and root.handlers == [handler]
        assert isinstance(handler.formatter, logs.JsonFormatter)
        again = logs.setup_logging({**ENV, "MARDEN_LOG_FORMAT": "text"})
        assert root.handlers == [again] and isinstance(again.formatter, logs.RedactingFormatter)
        stream = io.StringIO()
        again.setStream(stream)
        logging.getLogger("marden.x").warning("url postgres://app_rw:s3cret-pass@h/app")
        assert "s3cret-pass" not in stream.getvalue() and "app_rw:***@" in stream.getvalue()
        with pytest.raises(ValueError):
            logs.setup_logging({"MARDEN_LOG_FORMAT": "xml"})
        with pytest.raises(ValueError):
            logs.setup_logging({"MARDEN_LOG_LEVEL": "LOUD"})
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)
        root.setLevel(saved_level)
