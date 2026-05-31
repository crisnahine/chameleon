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

HARDENING_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=30000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA trusted_schema=OFF",
    "PRAGMA wal_autocheckpoint=10000",
)

# Pragmas that succeed on a read-only connection. journal_mode=WAL,
# synchronous, and wal_autocheckpoint all require write access and THROW on a
# read-only / non-WAL db file — which would abort the open and silently disable
# every index.db read. busy_timeout and trusted_schema=OFF are safe read-only.
READONLY_PRAGMAS = (
    "PRAGMA busy_timeout=30000",
    "PRAGMA trusted_schema=OFF",
)


def open_hardened(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with chameleon's hardening pragmas applied.

    Args:
        db_path: path to the SQLite file (parent directory created if missing
            on the read-write path only)
        read_only: if True, open with mode=ro URI and apply only the pragmas
            that succeed on a read-only connection

    Returns:
        Configured sqlite3.Connection
    """
    if read_only:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            isolation_level="",
        )
        pragmas = READONLY_PRAGMAS
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(db_path),
            isolation_level="",
        )
        pragmas = HARDENING_PRAGMAS

    for pragma in pragmas:
        conn.execute(pragma)

    conn.row_factory = sqlite3.Row

    return conn
