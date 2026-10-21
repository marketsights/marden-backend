"""Test harness: a throwaway Postgres database with the real migrations applied.

A database `marden_test_<pid>_<hex>` is created on the server named by DATABASE_URL (default: the
local dev server) for each test session, every migration's `-- migrate:up` section is applied, and
the database is dropped at the end. The real `marden` database is never touched, and NOTIFY
(per database) never reaches a real listener. Tests that need commits (heartbeats, NOTIFY, the
worker) use the pooled code path; tables are truncated after every test. Tests that can run inside
one transaction use the `conn` fixture, which is rolled back.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

LOCAL_DEV_URL = "postgres://marden:marden@localhost:5432/marden?sslmode=disable"
BASE_URL = os.environ.get("DATABASE_URL") or LOCAL_DEV_URL
TEST_DB = f"marden_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
TEST_URL = make_conninfo(BASE_URL, dbname=TEST_DB)
MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"
RAW_DIR = tempfile.mkdtemp(prefix="marden-raw-test-")
KEEP_TABLES = {"spatial_ref_sys"}  # PostGIS reference data

assert conninfo_to_dict(TEST_URL)["dbname"] != conninfo_to_dict(BASE_URL).get("dbname")

# Configure the environment before any marden module reads settings.
os.environ["DATABASE_URL"] = TEST_URL
os.environ["MARDEN_RAW_DIR"] = RAW_DIR
for _name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
    os.environ.pop(_name, None)  # tests never write to a real bucket


def _up_section(path: Path) -> str:
    text = path.read_text()
    up = text.split("-- migrate:up", 1)[1]
    return up.split("-- migrate:down", 1)[0]


@pytest.fixture(scope="session", autouse=True)
def test_database() -> Iterator[str]:
    from marden.core import config, db

    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    try:
        with psycopg.connect(TEST_URL) as conn:
            for path in sorted(MIGRATIONS.glob("*.sql")):
                conn.execute(_up_section(path))
        config.reload_settings()
        yield TEST_DB
    finally:
        db.close_pool()
        with psycopg.connect(BASE_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB)))
        shutil.rmtree(RAW_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _truncate_after(test_database: str) -> Iterator[None]:
    yield
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        names = [
            r[0]
            for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            if r[0] not in KEEP_TABLES
        ]
        if names:
            tables = sql.SQL(", ").join(sql.Identifier("public", n) for n in names)
            c.execute(sql.SQL("TRUNCATE {} CASCADE").format(tables))


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A connection whose work is rolled back at the end of the test."""
    c = psycopg.connect(TEST_URL, row_factory=dict_row)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def committed() -> Iterator[psycopg.Connection]:
    """An autocommit connection for setup rows and assertions on committed state."""
    c = psycopg.connect(TEST_URL, row_factory=dict_row, autocommit=True)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def raw_dir() -> Path:
    return Path(RAW_DIR)
