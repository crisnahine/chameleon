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

# DDL applied on `init_drift_db()`. Idempotent — safe to run repeatedly.
SCHEMA_DDL = """
-- Schema metadata
CREATE TABLE IF NOT EXISTS schema_meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

-- Per-file drift state (hot path: PreToolUse hook reads + writes).
-- WITHOUT ROWID saves a rowid->row indirection on PK lookup.
CREATE TABLE IF NOT EXISTS files (
  rel_path TEXT PRIMARY KEY,
  inode INTEGER,
  mtime_ns INTEGER NOT NULL,
  size INTEGER,
  sha_hint BLOB,                      -- xxhash64 (8 bytes), non-crypto
  archetype TEXT,
  cached_sig BLOB,                    -- serialized 7-tuple cluster signature
  last_observed_confidence REAL,
  last_seen_at INTEGER NOT NULL       -- unix epoch seconds
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_files_last_seen ON files(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_files_archetype ON files(archetype);

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

    # Set schema version (insert if missing; do not overwrite if present)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (k, v) VALUES ('schema_version', ?)",
        (DRIFT_DB_SCHEMA_VERSION,),
    )

    return conn


def gc_old_observations(conn: sqlite3.Connection, *, max_age_seconds: int = 30 * 86_400) -> int:
    """Delete edit_observations older than `max_age_seconds`. Returns rows deleted.

    Per docs/architecture.md "drift.db" GC policy: 30-day record purge weekly.
    """
    import time

    cutoff = int(time.time()) - max_age_seconds
    cursor = conn.execute(
        "DELETE FROM edit_observations WHERE observed_at < ?", (cutoff,)
    )
    deleted = cursor.rowcount

    # Truncate WAL on GC (PRAGMA wal_checkpoint(TRUNCATE) per architecture)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return deleted
