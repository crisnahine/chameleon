"""Regression tests for drift.db init: short-timeout schema write + self-heal.

Two failure modes that bit the hook hot path:

- A cold process (empty connection cache, the common case on every fresh
  hook) opens drift.db via ``open_hardened`` (30s busy_timeout) and then runs
  the schema DDL + a schema_meta insert under that timeout. On a contended
  drift.db that write could block up to 30s, defeating the sub-second hook
  budget. ``init_drift_db`` must do the schema-init write under a short
  busy_timeout and return/raise fast.

- A malformed drift.db (truncated WAL, partial write, foreign corruption)
  used to wedge the open path forever. Since drift is advisory, ``init_drift_db``
  must drop the corrupt file (plus its -wal/-shm sidecars) and recreate an
  empty db once.

Isolation mirrors test_drift_observations.py: a fresh tmp_path drift.db and
no shared connection cache.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from chameleon_mcp.drift.schema import init_drift_db


class TestSchemaInitShortTimeout:
    def test_init_does_not_hang_when_db_briefly_write_locked(self, tmp_path: Path):
        db_path = tmp_path / "drift.db"
        # Seed a valid db so a foreign connection can take the WAL write lock.
        seed = init_drift_db(db_path)
        seed.close()

        holder = sqlite3.connect(str(db_path))
        try:
            # BEGIN IMMEDIATE grabs the single WAL writer lock; any other
            # connection's write must wait out its busy_timeout.
            holder.execute("PRAGMA busy_timeout=30000")
            holder.execute("BEGIN IMMEDIATE")

            start = time.monotonic()
            with pytest.raises(sqlite3.OperationalError):
                # The schema-init write contends for the held write lock. With
                # the hardened 30s timeout this blocks ~30s; with a short
                # timeout it raises "database is locked" almost immediately.
                conn = init_drift_db(db_path)
                conn.close()
            elapsed = time.monotonic() - start

            # Well under the 30s hardened timeout. A couple seconds of slack
            # for slow CI, but nowhere near 30s.
            assert elapsed < 3.0, f"schema-init write blocked {elapsed:.1f}s on a locked db"
        finally:
            try:
                holder.rollback()
            except sqlite3.Error:
                pass
            holder.close()

    def test_steady_state_timeout_is_200ms(self, tmp_path: Path):
        db_path = tmp_path / "drift.db"
        conn = init_drift_db(db_path)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 200
        finally:
            conn.close()


class TestSchemaInitLeavesNoPendingTransaction:
    """The schema-meta insert must be committed before init returns.

    Under ``isolation_level=""`` an uncommitted INSERT leaves the connection
    holding the WAL writer lock. A long-lived process that only ever READS
    through this connection (the MCP server answering explain_edit /
    get_shadow_report) then pins that lock for its whole lifetime, and every
    hook write in other processes dies at its 200ms busy_timeout — silently,
    because drift writes are fail-open. That starved the entire decision_log
    during a multi-session campaign.
    """

    def test_init_returns_with_no_open_transaction(self, tmp_path: Path):
        db_path = tmp_path / "drift.db"
        conn = init_drift_db(db_path)
        try:
            assert conn.in_transaction is False
        finally:
            conn.close()

    def test_reinit_on_existing_db_also_commits(self, tmp_path: Path):
        db_path = tmp_path / "drift.db"
        first = init_drift_db(db_path)
        first.close()
        conn = init_drift_db(db_path)
        try:
            assert conn.in_transaction is False
        finally:
            conn.close()

    def test_schema_meta_row_is_durable_without_later_writes(self, tmp_path: Path):
        # A reader-only process must still leave a committed schema_version
        # behind: close without any write, reopen, row is there.
        db_path = tmp_path / "drift.db"
        conn = init_drift_db(db_path)
        conn.close()
        check = sqlite3.connect(str(db_path))
        try:
            row = check.execute("SELECT v FROM schema_meta WHERE k = 'schema_version'").fetchone()
            assert row is not None
        finally:
            check.close()


class TestSchemaInitSelfHeal:
    def test_malformed_db_self_heals_to_empty_working_db(self, tmp_path: Path):
        db_path = tmp_path / "drift.db"
        # Not a sqlite file at all -> "file is not a database" on open/write.
        db_path.write_bytes(b"this is not a sqlite database, it is garbage bytes")
        (tmp_path / "drift.db-wal").write_bytes(b"junk-wal")
        (tmp_path / "drift.db-shm").write_bytes(b"junk-shm")

        conn = init_drift_db(db_path)
        try:
            # Recovered to a working, empty db with the canonical schema.
            (count,) = conn.execute("SELECT COUNT(*) FROM edit_observations").fetchone()
            assert count == 0
            row = conn.execute("SELECT v FROM schema_meta WHERE k = 'schema_version'").fetchone()
            assert row is not None
            # Writable after recovery.
            conn.execute(
                "INSERT INTO edit_observations "
                "(rel_path, archetype, confidence_observed, matched_canonical, observed_at) "
                "VALUES ('x.ts', 'c', 0.9, 0, 1)"
            )
            conn.commit()
            (count_after,) = conn.execute("SELECT COUNT(*) FROM edit_observations").fetchone()
            assert count_after == 1
        finally:
            conn.close()

    def test_self_heal_removes_stale_sidecars(self, tmp_path: Path):
        db_path = tmp_path / "drift.db"
        db_path.write_bytes(b"not a database")
        wal = tmp_path / "drift.db-wal"
        shm = tmp_path / "drift.db-shm"
        wal.write_bytes(b"stale wal")
        shm.write_bytes(b"stale shm")

        conn = init_drift_db(db_path)
        try:
            # The garbage sidecars were unlinked during recovery; any -wal/-shm
            # present now is freshly created by the recovered connection.
            assert not wal.read_bytes().startswith(b"stale wal")
            assert not shm.read_bytes().startswith(b"stale shm") if shm.exists() else True
        finally:
            conn.close()
