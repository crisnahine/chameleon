"""SQLite configuration helpers — applies hardening pragmas to every connection.

Per ARCHITECTURE.md "SQLite schemas" hardening profile:
- WAL mode (concurrent readers + 1 writer)
- busy_timeout=30000 (30s; tolerates concurrent /chameleon-refresh)
- synchronous=NORMAL (durability OK for caches; transactional commit handled at application layer)
- trusted_schema=OFF (Round 5 AppSec hardening: no implicit trust of schema metadata)
- wal_autocheckpoint=10000 (~40MB amortization vs default 4MB)

Plus retry-with-jitter on SQLITE_BUSY for the writer path.
"""

from __future__ import annotations

import random
import sqlite3
import time
from pathlib import Path

# Pragmas applied to every connection
HARDENING_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=30000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA trusted_schema=OFF",
    "PRAGMA wal_autocheckpoint=10000",
)

# SQLITE_BUSY retry policy
MAX_RETRIES = 5
BASE_BACKOFF_MS = 100
MAX_BACKOFF_MS = 1600


def open_hardened(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with chameleon's hardening pragmas applied.

    Args:
        db_path: path to the SQLite file (parent directory created if missing)
        read_only: if True, open with mode=ro URI

    Returns:
        Configured sqlite3.Connection
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        # URI-based read-only mode + immutable hint where appropriate
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
    else:
        conn = sqlite3.connect(
            str(db_path),
            isolation_level=None,
        )

    # Apply hardening pragmas
    for pragma in HARDENING_PRAGMAS:
        conn.execute(pragma)

    # Use Row factory for ergonomic column access
    conn.row_factory = sqlite3.Row

    return conn


def execute_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple = (),
    *,
    max_retries: int = MAX_RETRIES,
) -> sqlite3.Cursor:
    """Execute SQL with retry-with-jitter on SQLITE_BUSY.

    Args:
        conn: an open SQLite connection
        sql: SQL statement
        params: bound parameters
        max_retries: how many retries before giving up

    Returns:
        sqlite3.Cursor for the executed statement.

    Raises:
        sqlite3.OperationalError: after exhausting retries.
    """
    last_err: sqlite3.OperationalError | None = None
    for attempt in range(max_retries + 1):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            last_err = e
            if attempt >= max_retries:
                break
            # Exponential backoff with jitter
            backoff_ms = min(BASE_BACKOFF_MS * (2 ** attempt), MAX_BACKOFF_MS)
            jitter = random.uniform(0.5, 1.5)
            time.sleep((backoff_ms * jitter) / 1000.0)
    if last_err:
        raise last_err
    raise RuntimeError("execute_with_retry: unreachable")
