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


def init_drift_db(db_path: Path) -> sqlite3.Connection:
    """Create or open drift.db with the canonical schema.

    Idempotent: safe to call on existing databases (uses CREATE TABLE IF NOT EXISTS).
    Returns an open hardened connection.
    """
    conn = open_hardened(db_path)
    conn.executescript(SCHEMA_DDL)

    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (k, v) VALUES ('schema_version', ?)",
        (DRIFT_DB_SCHEMA_VERSION,),
    )

    return conn
