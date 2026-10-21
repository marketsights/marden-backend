"""Nightly database backup: pg_dump of the direct endpoint, streamed to the R2 bucket core-backups.

    python -m marden backup run              one backup now (the worker runs ops.backup_db nightly)
    python -m marden backup list             the latest backup_run rows
    python -m marden backup fetch latest F   download a backup to F, check its sha256, list its contents

Each attempt is a `backup_run` row (db/migrations/*_backup_run.sql). Without the three R2 credentials
the run is recorded as `not_configured` and nothing is dumped: there is no local fallback, because a
deployed container's disk does not survive a redeploy (hard rule: a missing credential reports "not
configured", it never pretends to succeed).

The dump is pg_dump's custom format (compressed, restorable table by table with pg_restore), of the
DATABASE_URL_UNPOOLED endpoint (pg_dump needs a session; PgBouncer's transaction mode breaks it). The
queue's own tables are dumped without their rows. pg_dump's output is streamed straight into an S3
multipart upload, so the container needs no disk for it; if pg_dump fails part-way, the upload is
aborted and no object is left behind. Credentials go to pg_dump through libpq's environment variables,
never on its command line, and never into a log line or the `detail` column.

Retention is an R2 lifecycle rule on the bucket (docs/DEPLOY.md), not code: this module never deletes.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from psycopg.conninfo import conninfo_to_dict

from marden.core import db
from marden.core.config import Settings, get_settings
from marden.core.logs import redact, secret_values

log = logging.getLogger(__name__)

KEY_PREFIX = "pg"
# Procrastinate's tables: structure is dumped, rows are not (jobs and events are operational state).
EXCLUDE_TABLE_DATA = ("procrastinate_jobs", "procrastinate_events", "procrastinate_periodic_defers",
                      "procrastinate_workers")
STALE_RUNNING_HOURS = 6
_DETAIL_MAX = 2000
_KEY = re.compile(r"^pg/[A-Za-z0-9_\-]+/\d{4}/\d{2}/[A-Za-z0-9_\-]+-\d{8}T\d{6}Z\.dump$")

# libpq connection parameters -> the environment variables pg_dump reads instead of argv.
_LIBPQ_ENV = {
    "host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT", "dbname": "PGDATABASE", "user": "PGUSER",
    "password": "PGPASSWORD", "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY", "sslnegotiation": "PGSSLNEGOTIATION", "channel_binding": "PGCHANNELBINDING",
    "options": "PGOPTIONS", "application_name": "PGAPPNAME", "connect_timeout": "PGCONNECT_TIMEOUT",
    "target_session_attrs": "PGTARGETSESSIONATTRS", "gssencmode": "PGGSSENCMODE",
}


class BackupError(RuntimeError):
    """A backup step failed; the message is safe to log (secrets redacted)."""


@dataclass(frozen=True)
class BackupResult:
    run_id: int
    status: str  # ok | failed | not_configured
    database: str | None = None
    bucket: str | None = None
    key: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------------------- pg_dump
def libpq_env(url: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a libpq tool connecting to `url`: PG* variables, nothing on the command line."""
    params = conninfo_to_dict(url)
    unknown = sorted(set(params) - set(_LIBPQ_ENV))
    if unknown:
        raise BackupError(f"database URL has parameters pg_dump cannot take from the environment: {unknown}")
    env = {k: v for k, v in (base if base is not None else os.environ).items() if not k.startswith("PG")}
    for name, value in params.items():
        if value is not None and value != "":
            env[_LIBPQ_ENV[name]] = str(value)
    return env


def database_name(url: str) -> str:
    return str(conninfo_to_dict(url).get("dbname") or "postgres")


def pg_dump_path() -> str:
    path = shutil.which("pg_dump")
    if not path:
        raise BackupError("pg_dump is not installed (the worker image installs postgresql-client)")
    return path


def pg_dump_version(path: str | None = None) -> str:
    out = subprocess.run([path or pg_dump_path(), "--version"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def pg_dump_command(path: str | None = None) -> list[str]:
    cmd = [path or pg_dump_path(), "--format=custom", "--compress=6", "--no-owner", "--no-privileges",
           "--lock-wait-timeout=60000"]
    cmd += [f"--exclude-table-data={table}" for table in EXCLUDE_TABLE_DATA]
    return cmd


class DumpStream(io.RawIOBase):
    """pg_dump's stdout as a readable stream that hashes and counts what passes through. At end of
    stream it waits for pg_dump; a non-zero exit raises BackupError instead of signalling EOF, so an
    upload reading this stream is aborted rather than completed with a truncated archive."""

    def __init__(self, url: str, *, secrets: list[str] | None = None, path: str | None = None):
        super().__init__()
        self._secrets = [*(secrets or []), *_url_secrets(url)]
        self._stderr: IO[bytes] = tempfile.TemporaryFile()  # noqa: SIM115 (closed in _finish/abort)
        self.proc = subprocess.Popen(pg_dump_command(path), stdout=subprocess.PIPE, stderr=self._stderr,
                                     env=libpq_env(url))
        self.sha256 = hashlib.sha256()
        self.size = 0
        self.finished = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        assert self.proc.stdout is not None
        n = self.proc.stdout.readinto(buffer)
        if n:
            self.sha256.update(memoryview(buffer)[:n])
            self.size += n
            return n
        self._finish()
        return 0

    def _finish(self) -> None:
        if self.finished:
            return
        code = self.proc.wait()
        self._stderr.seek(0)
        err = self._stderr.read().decode("utf-8", "replace").strip()
        self._stderr.close()
        self.finished = True
        if code != 0:
            raise BackupError(redact(f"pg_dump exited {code}: {err[-_DETAIL_MAX:]}", self._secrets))
        if self.size == 0:
            raise BackupError("pg_dump wrote nothing")

    def abort(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if not self._stderr.closed:
            self._stderr.close()

    def close(self) -> None:
        self.abort()
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        super().close()


def _url_secrets(url: str) -> list[str]:
    password = conninfo_to_dict(url).get("password")
    return [url, *([str(password)] if password else [])]


def dump_to_file(url: str, target: Path) -> tuple[int, str]:
    """pg_dump `url` into `target`; returns (bytes, sha256). Used by tests and for local copies."""
    stream = DumpStream(url)
    try:
        with open(target, "wb") as fh:
            shutil.copyfileobj(stream, fh, length=1024 * 1024)
        stream._finish()
    finally:
        stream.close()
    return stream.size, stream.sha256.hexdigest()


# ---------------------------------------------------------------------------------------- R2
def object_key(database: str, when: datetime) -> str:
    when = when.astimezone(UTC)
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", database)
    return f"{KEY_PREFIX}/{safe}/{when:%Y}/{when:%m}/{safe}-{when:%Y%m%dT%H%M%SZ}.dump"


def r2_client(settings: Settings) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url(),
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )


def upload_stream(client: Any, bucket: str, key: str, stream: DumpStream, metadata: dict[str, str]) -> None:
    from boto3.s3.transfer import TransferConfig

    # 64 MiB parts: a 10 GB dump stays well under S3's 10,000-part limit.
    config = TransferConfig(multipart_threshold=64 * 1024 * 1024, multipart_chunksize=64 * 1024 * 1024,
                            max_concurrency=4)
    client.upload_fileobj(stream, bucket, key, ExtraArgs={"Metadata": metadata,
                                                          "ContentType": "application/octet-stream"},
                          Config=config)


# ---------------------------------------------------------------------------------------- runs
def _start_run(database: str | None) -> int:
    with db.transaction() as conn:
        conn.execute(
            """UPDATE backup_run SET status = 'failed', finished_at = now(),
                      detail = 'interrupted: the worker stopped before this run finished'
               WHERE status = 'running' AND started_at < now() - make_interval(hours => %s)""",
            (STALE_RUNNING_HOURS,),
        )
        row = conn.execute(
            "INSERT INTO backup_run (status, database) VALUES ('running', %s) RETURNING id", (database,)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def _finish_run(run_id: int, status: str, **cols: Any) -> None:
    allowed = ("bucket", "object_key", "bytes", "sha256", "pg_dump_version", "detail")
    sets = [f"{c} = %({c})s" for c in allowed if c in cols]
    with db.transaction() as conn:
        conn.execute(
            f"UPDATE backup_run SET status = %(status)s, finished_at = now(){''.join(', ' + s for s in sets)} "
            "WHERE id = %(id)s",
            {"status": status, "id": run_id, **{c: cols[c] for c in allowed if c in cols}},
        )


def missing_r2(settings: Settings) -> list[str]:
    return [name for name, value in (("R2_ACCOUNT_ID", settings.r2_account_id),
                                     ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
                                     ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key)) if not value]


def run_backup(settings: Settings | None = None, *, client: Any = None, now: datetime | None = None) -> BackupResult:
    """One backup attempt, recorded in backup_run. Raises BackupError after recording a failure."""
    settings = settings or get_settings()
    url = settings.require_session_database_url()
    database = database_name(url)
    run_id = _start_run(database)
    missing = missing_r2(settings)
    if missing:
        detail = f"R2 is not configured: {', '.join(missing)} not set; nothing was dumped"
        _finish_run(run_id, "not_configured", detail=detail)
        log.warning("backup %s: not configured (%s)", run_id, detail)
        return BackupResult(run_id, "not_configured", database=database, detail=detail)

    secrets = secret_values(os.environ) + _url_secrets(url)
    bucket = settings.r2_bucket_backups
    key = object_key(database, now or datetime.now(UTC))
    version: str | None = None
    stream: DumpStream | None = None
    try:
        path = pg_dump_path()
        version = pg_dump_version(path)
        client = client or r2_client(settings)
        log.info("backup %s: %s -> r2://%s/%s", run_id, version, bucket, key)
        stream = DumpStream(url, secrets=secrets, path=path)
        upload_stream(client, bucket, key, stream, {"database": database, "pg-dump": version})
        stream._finish()  # raises if pg_dump failed after the last byte was uploaded
        head = client.head_object(Bucket=bucket, Key=key)
        if int(head["ContentLength"]) != stream.size:
            raise BackupError(f"uploaded object is {head['ContentLength']} bytes, pg_dump wrote {stream.size}")
    except Exception as exc:
        detail = redact(str(exc), secrets)[:_DETAIL_MAX] or type(exc).__name__
        _finish_run(run_id, "failed", bucket=bucket, pg_dump_version=version, detail=detail)
        log.error("backup %s failed: %s", run_id, detail)
        raise BackupError(detail) from None
    finally:
        if stream is not None:
            stream.close()
    sha = stream.sha256.hexdigest()
    _finish_run(run_id, "ok", bucket=bucket, object_key=key, bytes=stream.size, sha256=sha, pg_dump_version=version)
    log.info("backup %s ok: %s bytes, sha256 %s", run_id, stream.size, sha)
    return BackupResult(run_id, "ok", database=database, bucket=bucket, key=key, bytes=stream.size, sha256=sha)


def recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    with db.connection() as conn:
        return conn.execute(
            """SELECT id, started_at, finished_at, status, database, bucket, object_key, bytes, sha256,
                      pg_dump_version, detail
               FROM backup_run ORDER BY started_at DESC, id DESC LIMIT %s""",
            (limit,),
        ).fetchall()


def latest_ok_key() -> tuple[str, str, str] | None:
    """(bucket, object_key, sha256) of the newest successful backup, or None."""
    with db.connection() as conn:
        row = conn.execute(
            "SELECT bucket, object_key, sha256 FROM backup_run WHERE status = 'ok' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return (row["bucket"], row["object_key"], row["sha256"]) if row else None


def verify_archive(path: Path) -> int:
    """pg_restore --list the archive; returns the number of entries (raises BackupError if unreadable)."""
    restore = shutil.which("pg_restore")
    if not restore:
        raise BackupError("pg_restore is not installed")
    out = subprocess.run([restore, "--list", str(path)], capture_output=True, text=True)
    if out.returncode != 0:
        raise BackupError(f"pg_restore cannot read {path.name}: {out.stderr.strip()[-500:]}")
    return sum(1 for line in out.stdout.splitlines() if line and not line.startswith(";"))


def fetch_backup(target: Path, *, key: str | None = None, settings: Settings | None = None,
                 client: Any = None) -> dict[str, Any]:
    """Download a backup (default: the newest ok one) to `target`, check sha256, list it."""
    settings = settings or get_settings()
    missing = missing_r2(settings)
    if missing:
        raise BackupError(f"R2 is not configured: {', '.join(missing)} not set")
    expected: str | None = None
    bucket = settings.r2_bucket_backups
    if key is None or key == "latest":
        latest = latest_ok_key()
        if latest is None:
            raise BackupError("no successful backup is recorded in backup_run")
        bucket, key, expected = latest
    elif not _KEY.match(key):
        raise BackupError(f"not a backup key: {key!r}")
    client = client or r2_client(settings)
    client.download_file(bucket, key, str(target))
    digest = hashlib.sha256()
    with open(target, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    if expected and digest.hexdigest() != expected:
        raise BackupError(f"sha256 of {key} does not match backup_run ({digest.hexdigest()} != {expected})")
    return {"bucket": bucket, "key": key, "sha256": digest.hexdigest(), "entries": verify_archive(target)}
