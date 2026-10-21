"""Postgres access: one psycopg connection pool per process, dict rows.

    with db.transaction() as conn:          # pooled connection, one transaction, committed on exit
        conn.execute("...")

    with db.transaction(conn):              # nested: a savepoint on a connection you already hold
        ...

Every writer in marden.core takes an optional `conn`; pass it to make several writes atomic.
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from marden.core.config import get_settings

Row = dict[str, Any]
Conn = psycopg.Connection[Row]

POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 10

_lock = threading.Lock()
_pool: ConnectionPool[Conn] | None = None
_pool_url: str | None = None
_atexit_registered = False


def get_pool() -> ConnectionPool[Conn]:
    """The process-wide pool for the worker database URL (re-created if the URL changed)."""
    global _pool, _pool_url, _atexit_registered
    url = get_settings().require_session_database_url()
    with _lock:
        if _pool is not None and _pool_url == url:
            return _pool
        if _pool is not None:
            _pool.close()
        if not _atexit_registered:
            atexit.register(close_pool)  # stop pool threads cleanly instead of at interpreter teardown
            _atexit_registered = True
        _pool = ConnectionPool(
            url,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            kwargs={"row_factory": dict_row},
            check=ConnectionPool.check_connection,
            name="marden",
            open=True,
        )
        _pool_url = url
        return _pool


def close_pool() -> None:
    global _pool, _pool_url
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _pool_url = None


@contextmanager
def connection() -> Iterator[Conn]:
    """A pooled connection; its open transaction is committed on success, rolled back on error."""
    with get_pool().connection() as conn:
        yield conn


@contextmanager
def transaction(conn: Conn | None = None) -> Iterator[Conn]:
    """A transaction block: a savepoint on `conn` if given, else a pooled connection's transaction."""
    if conn is not None:
        with conn.transaction():
            yield conn
        return
    with get_pool().connection() as pooled, pooled.transaction():
        yield pooled


def connect(*, autocommit: bool = False) -> Conn:
    """A dedicated (unpooled) connection, e.g. for LISTEN. The caller closes it."""
    url = get_settings().require_session_database_url()
    return psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)


def fetch_one(conn: Conn, sql: str, params: Any = None) -> Row | None:
    return conn.execute(sql, params).fetchone()


def fetch_all(conn: Conn, sql: str, params: Any = None) -> list[Row]:
    return conn.execute(sql, params).fetchall()
