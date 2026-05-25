"""SQLite configuration helpers — applies hardening pragmas to every connection.

Per docs/architecture.md "SQLite schemas" hardening profile:
- WAL mode (concurrent readers + 1 writer)
- busy_timeout=30000 (30s; tolerates concurrent /chameleon-refresh)
- synchronous=NORMAL (durability OK for caches; transactional commit handled at application layer)
- trusted_schema=OFF (Round 5 AppSec hardening: no implicit trust of schema metadata)
- wal_autocheckpoint=10000 (~40MB amortization vs default 4MB)

"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Pragmas applied to every connection
HARDENING_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=30000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA trusted_schema=OFF",
    "PRAGMA wal_autocheckpoint=10000",
)


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
            isolation_level="",  # deferred transactions; with conn: issues BEGIN/COMMIT
        )
    else:
        conn = sqlite3.connect(
            str(db_path),
            isolation_level="",
        )

    # Apply hardening pragmas
    for pragma in HARDENING_PRAGMAS:
        conn.execute(pragma)

    # Use Row factory for ergonomic column access
    conn.row_factory = sqlite3.Row

    return conn


