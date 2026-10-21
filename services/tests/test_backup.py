"""Nightly backup (marden.core.backup, task ops.backup_db): pg_dump of the real test database streamed
to an object store, recorded in backup_run, fetched back, checked and restored into a scratch database.

R2 itself is the one external boundary: `MemoryBucket` stands in for the S3 client and keeps the
uploaded bytes exactly as boto3 would hand them over (upload_fileobj reads the stream to its end). The
dump, the hashing, the run records, the archive check and the restore are the real thing.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from conftest import BASE_URL, TEST_URL
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from marden import worker
from marden.core import backup
from marden.core.config import Settings

R2 = {"r2_account_id": "acct", "r2_access_key_id": "id", "r2_secret_access_key": "secret-key-value"}
WHEN = datetime(2026, 10, 21, 7, 23, 5, tzinfo=UTC)

pytestmark = pytest.mark.skipif(shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
                                reason="pg_dump/pg_restore not installed")


class MemoryBucket:
    """The S3 calls marden.core.backup makes, on a dict."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def upload_fileobj(self, fileobj: Any, bucket: str, key: str, ExtraArgs: dict | None = None,
                       Config: Any = None) -> None:
        chunks = []
        while chunk := fileobj.read(1024 * 1024):
            chunks.append(chunk)
        self.objects[(bucket, key)] = (b"".join(chunks), dict((ExtraArgs or {}).get("Metadata", {})))

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        return {"ContentLength": len(self.objects[(Bucket, Key)][0])}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        Path(filename).write_bytes(self.objects[(bucket, key)][0])


def _settings(**overrides: Any) -> Settings:
    return Settings(database_url=TEST_URL, **{**R2, **overrides})


def _runs(committed: psycopg.Connection) -> list[dict[str, Any]]:
    return committed.execute("SELECT * FROM backup_run ORDER BY id").fetchall()


@pytest.fixture
def scratch_db():
    name = f"marden_restore_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(BASE_URL, dbname=name)
    finally:
        with psycopg.connect(BASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_without_r2_the_run_is_recorded_as_not_configured_and_nothing_is_dumped(committed):
    result = backup.run_backup(Settings(database_url=TEST_URL))
    assert result.status == "not_configured"
    assert result.detail is not None and "R2_ACCOUNT_ID" in result.detail and "R2_SECRET_ACCESS_KEY" in result.detail
    [run] = _runs(committed)
    assert run["status"] == "not_configured" and run["finished_at"] is not None
    assert run["object_key"] is None and run["bytes"] is None
    assert run["database"] == conninfo_to_dict(TEST_URL)["dbname"]
    # The worker task reports the same (conftest removes the R2 variables from the environment).
    assert worker.backup_db_task(timestamp=0) == "not_configured"
    assert [r["status"] for r in _runs(committed)] == ["not_configured", "not_configured"]


def test_backup_uploads_a_complete_archive_that_fetches_and_restores(committed, tmp_path, scratch_db):
    committed.execute(
        "INSERT INTO source (id, name, kind, cadence_minutes) VALUES ('backup_check', 'Backup check', 'api', 60)"
    )
    committed.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, args) VALUES ('default', 'x.y', '{}')"
    )
    bucket = MemoryBucket()
    result = backup.run_backup(_settings(), client=bucket, now=WHEN)

    dbname = conninfo_to_dict(TEST_URL)["dbname"]
    assert result.status == "ok" and result.bucket == "core-backups"
    assert result.key == f"pg/{dbname}/2026/10/{dbname}-20261021T072305Z.dump"
    body, meta = bucket.objects[("core-backups", result.key)]
    assert body[:5] == b"PGDMP"  # pg_dump custom format
    assert len(body) == result.bytes and hashlib.sha256(body).hexdigest() == result.sha256
    assert meta["database"] == dbname and meta["pg-dump"].startswith("pg_dump (PostgreSQL)")
    [run] = _runs(committed)
    expected = ("ok", result.key, len(body), result.sha256)
    assert (run["status"], run["object_key"], run["bytes"], run["sha256"]) == expected

    # Fetch the newest ok backup back, check its hash against backup_run, list it with pg_restore.
    target = tmp_path / "latest.dump"
    info = backup.fetch_backup(target, settings=_settings(), client=bucket)
    assert info["key"] == result.key and info["sha256"] == result.sha256 and info["entries"] > 100

    # Restore it into an empty database: the schema and the rows come back; queue rows do not.
    out = subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "--dbname", scratch_db,
                          str(target)], capture_output=True, text=True, env=backup.libpq_env(scratch_db))
    assert out.returncode == 0, out.stderr[-2000:]
    with psycopg.connect(scratch_db) as restored:
        assert restored.execute("SELECT name FROM source WHERE id = 'backup_check'").fetchone() == ("Backup check",)
        assert restored.execute("SELECT count(*) FROM procrastinate_jobs").fetchone() == (0,)
        assert restored.execute("SELECT to_regclass('public.backup_run') IS NOT NULL").fetchone() == (True,)


def test_a_failed_dump_is_recorded_redacted_and_leaves_no_object(committed):
    params = conninfo_to_dict(TEST_URL)
    bad = make_conninfo(TEST_URL, port="1", password="hunter2-do-not-log")  # nothing listens on port 1
    bucket = MemoryBucket()
    with pytest.raises(backup.BackupError) as err:
        backup.run_backup(Settings(database_url=bad, **R2), client=bucket, now=WHEN)
    assert "hunter2-do-not-log" not in str(err.value)
    [run] = _runs(committed)
    assert run["status"] == "failed" and run["object_key"] is None and run["finished_at"] is not None
    assert "pg_dump exited" in run["detail"] and "hunter2-do-not-log" not in run["detail"]
    assert bucket.objects == {}  # upload_fileobj saw the error, not an end of stream
    assert params["dbname"] == run["database"]


def test_fetch_refuses_without_r2_unknown_keys_and_a_hash_mismatch(committed, tmp_path):
    with pytest.raises(backup.BackupError, match="R2 is not configured"):
        backup.fetch_backup(tmp_path / "x.dump", settings=Settings(database_url=TEST_URL))
    with pytest.raises(backup.BackupError, match="no successful backup"):
        backup.fetch_backup(tmp_path / "x.dump", settings=_settings(), client=MemoryBucket())
    with pytest.raises(backup.BackupError, match="not a backup key"):
        backup.fetch_backup(tmp_path / "x.dump", key="../etc/passwd", settings=_settings(), client=MemoryBucket())

    bucket = MemoryBucket()
    result = backup.run_backup(_settings(), client=bucket, now=WHEN)
    data, meta = bucket.objects[("core-backups", result.key)]
    bucket.objects[("core-backups", result.key)] = (data[:-1] + bytes([data[-1] ^ 1]), meta)  # one bit flipped
    with pytest.raises(backup.BackupError, match="sha256"):
        backup.fetch_backup(tmp_path / "y.dump", settings=_settings(), client=bucket)


def test_a_run_left_running_by_a_killed_worker_is_closed_by_the_next_run(committed):
    committed.execute(
        "INSERT INTO backup_run (status, started_at) VALUES ('running', now() - interval '7 hours'), "
        "('running', now() - interval '1 hour')"
    )
    backup.run_backup(Settings(database_url=TEST_URL))
    statuses = [(r["status"], r["detail"]) for r in _runs(committed)]
    assert statuses[0][0] == "failed" and "interrupted" in statuses[0][1]
    assert statuses[1] == ("running", None)  # may still be in progress on another worker
    assert statuses[2][0] == "not_configured"


def test_credentials_reach_pg_dump_through_the_environment_never_argv():
    url = "postgresql://app_rw:s3cret@ep-x.us-east-2.aws.neon.tech/app?sslmode=require&channel_binding=require"
    env = backup.libpq_env(url, base={"PATH": "/usr/bin", "PGPASSWORD": "stale"})
    assert env["PGPASSWORD"] == "s3cret" and env["PGHOST"] == "ep-x.us-east-2.aws.neon.tech"
    assert env["PGSSLMODE"] == "require" and env["PGCHANNELBINDING"] == "require" and env["PGDATABASE"] == "app"
    assert env["PATH"] == "/usr/bin"
    assert not any("s3cret" in part for part in backup.pg_dump_command("/usr/bin/pg_dump"))
    with pytest.raises(backup.BackupError, match="cannot take"):
        backup.libpq_env("postgresql://u:p@h/db?keepalives=1")  # libpq knows it; no env variable carries it
    assert backup.object_key("app", WHEN) == "pg/app/2026/10/app-20261021T072305Z.dump"
