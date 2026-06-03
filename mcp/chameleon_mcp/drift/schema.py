"""drift.db schema initialization.

Per docs/architecture.md "SQLite schemas" → "drift.db" subsection. drift.db is a
per-repo cache (lives in `${PLUGIN_DATA}/<repo_id>/drift.db`).

Migration policy: drift.db is a CACHE. Drop-and-recreate is permitted on
schema bumps. `/chameleon-refresh` rebuilds in <60s on typical repos.

Use `chameleon_mcp.drift.sqlite_config.open_hardened()` to open the
connection (applies WAL pragmas + busy_timeout + retry-with-jitter).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from chameleon_mcp.drift.sqlite_config import open_hardened

DRIFT_DB_SCHEMA_VERSION = "1"

# The schema-init write (DDL + schema_meta insert) runs on the hook hot path,
# where a cold process pays for it on every fresh hook. open_hardened sets a
# 30s busy_timeout suited to long-lived MCP processes; under that timeout a
# contended drift.db could block the init write for the full 30s. Pin the init
# write to a short timeout so a locked db raises fast (drift is advisory, so a
# skipped write is acceptable). Steady-state writes keep the 200ms timeout set
# by the connection cache.
_INIT_BUSY_TIMEOUT_MS = 200

# Substrings (matched case-insensitively) that mark a drift.db as unrecoverably
# corrupt. Drift is advisory, so dropping corrupt observations to recover an
# empty db is acceptable.
_CORRUPT_DB_MARKERS = ("malformed", "not a database")

SCHEMA_DDL = """
-- Schema metadata
CREATE TABLE IF NOT EXISTS schema_meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

-- (The `files` table was removed: it had no readers anywhere — drift
-- detection runs entirely off edit_observations — so it was pure write
-- amplification + unbounded growth. Old databases keep the table harmlessly.)

-- Per-edit confidence history (powers drift-driven nags).
CREATE TABLE IF NOT EXISTS edit_observations (
  id INTEGER PRIMARY KEY,
  rel_path TEXT NOT NULL,
  archetype TEXT,
  confidence_observed REAL,
  matched_canonical INTEGER NOT NULL DEFAULT 0,  -- 0 or 1
  observed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edit_obs_at ON edit_observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_edit_obs_path ON edit_observations(rel_path, observed_at);
"""


def _open_and_init(db_path: Path) -> sqlite3.Connection:
    """Open drift.db and run the schema-init write under a short busy_timeout."""
    conn = open_hardened(db_path)
    conn.execute(f"PRAGMA busy_timeout={_INIT_BUSY_TIMEOUT_MS}")
    conn.executescript(SCHEMA_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (k, v) VALUES ('schema_version', ?)",
        (DRIFT_DB_SCHEMA_VERSION,),
    )
    return conn


def _is_corrupt_db_error(err: sqlite3.DatabaseError) -> bool:
    """True when the error message marks the file as unrecoverably corrupt."""
    msg = str(err).lower()
    return any(marker in msg for marker in _CORRUPT_DB_MARKERS)


def _drop_db_files(db_path: Path) -> None:
    """Unlink drift.db and its WAL/SHM sidecars, ignoring missing files."""
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def init_drift_db(db_path: Path) -> sqlite3.Connection:
    """Create or open drift.db with the canonical schema.

    Idempotent: safe to call on existing databases (uses CREATE TABLE IF NOT EXISTS).
    Returns an open hardened connection.

    Self-heal: a malformed / not-a-database file can never be opened again, so
    on that specific error the corrupt file and its WAL/SHM sidecars are dropped
    and the open is retried once against a fresh, empty db. Dropping the corrupt
    observations is acceptable because drift is advisory.
    """
    try:
        return _open_and_init(db_path)
    except sqlite3.DatabaseError as err:
        if not _is_corrupt_db_error(err):
            raise
    _drop_db_files(db_path)
    return _open_and_init(db_path)
