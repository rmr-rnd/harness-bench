"""P8: Database._execute_write retries on a dropped connection, but not on
data/SQL errors, and never disables the DB on a transient failure."""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from framework.config import DatabaseConfig
from framework.db import Database, _is_retryable_conn_error


# ── detector ────────────────────────────────────────────────────────────────

def test_detector_retryable_vs_not():
    assert _is_retryable_conn_error(ConnectionAbortedError())            # WinError 1236
    assert _is_retryable_conn_error(OSError("network reset"))
    assert _is_retryable_conn_error(RuntimeError("connection was closed in the middle of operation"))
    assert not _is_retryable_conn_error(ValueError("bad data"))
    assert not _is_retryable_conn_error(KeyError("missing"))


# ── fake asyncpg pool ─────────────────────────────────────────────────────────

class _FakeConn:
    def __init__(self, behavior):
        self.behavior = behavior   # call_index -> Exception | None
        self.calls = 0

    async def execute(self, sql, *params):
        self.calls += 1
        out = self.behavior(self.calls)
        if isinstance(out, Exception):
            raise out
        return out


class _FakeAcquire:
    def __init__(self, conn): self._conn = conn
    async def __aenter__(self): return self._conn
    async def __aexit__(self, *a): return False


class _FakePool:
    def __init__(self, conn): self._conn = conn
    def acquire(self): return _FakeAcquire(self._conn)


def _make_db(conn) -> Database:
    db = Database(DatabaseConfig(host="x"))
    db._enabled = True          # force on regardless of asyncpg presence
    db._pool = _FakePool(conn)
    return db


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("framework.db.asyncio.sleep", return_value=None):
        yield


# ── behaviour ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retries_and_succeeds_after_drop(caplog):
    # Fails once with a connection drop, then succeeds.
    conn = _FakeConn(lambda n: RuntimeError("connection was closed in the middle of operation") if n == 1 else None)
    db = _make_db(conn)
    with caplog.at_level(logging.WARNING, logger="framework.db"):
        await db._execute_write("save_x 1", "INSERT 1")
    assert conn.calls == 2                       # retried once, then ok
    assert "DB write failed" not in caplog.text  # no failure logged


@pytest.mark.asyncio
async def test_data_error_not_retried(caplog):
    conn = _FakeConn(lambda n: ValueError("bad data"))
    db = _make_db(conn)
    with caplog.at_level(logging.WARNING, logger="framework.db"):
        await db._execute_write("save_x 2", "INSERT 1")
    assert conn.calls == 1                        # NOT retried (not a conn error)
    assert "DB write failed (save_x 2)" in caplog.text


@pytest.mark.asyncio
async def test_persistent_drop_logs_once_and_keeps_enabled(caplog):
    conn = _FakeConn(lambda n: ConnectionResetError("connection was closed in the middle of operation"))
    db = _make_db(conn)
    with caplog.at_level(logging.WARNING, logger="framework.db"):
        await db._execute_write("save_x 3", "INSERT 1", attempts=3)
    assert conn.calls == 3                         # all attempts used
    assert caplog.text.count("DB write failed (save_x 3)") == 1
    assert db._enabled is True                     # transient drop must not disable the DB


@pytest.mark.asyncio
async def test_noop_when_disabled():
    conn = _FakeConn(lambda n: None)
    db = _make_db(conn)
    db._enabled = False
    await db._execute_write("save_x 4", "INSERT 1")
    assert conn.calls == 0
