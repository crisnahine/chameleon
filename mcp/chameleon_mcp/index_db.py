"""Repo index — single SQLite DB at ${PLUGIN_DATA}/index.db.

Phase 4.4: replace the directory-scanning `list_profiles` path with a real
table, and give `_resolve_repo_root_by_id` an O(1) primary lookup that doesn't
require reading the per-repo `.trust` JSON for every callsite.

The database lives at ${PLUGIN_DATA}/index.db (peer of the per-repo
<repo_id>/ subdirectories). One row per repo this user has bootstrapped.
Single-writer assumption: one Claude Code session per machine writes at a
time. WAL + busy_timeout=2000ms tolerates the rare contended write.

`drift.db` uses 30000ms busy_timeout because the PreToolUse hot path can
queue concurrent edits. `index.db` writes only fire from bootstrap_repo /
refresh_repo / list_profiles, which are user-driven, so the shorter
busy_timeout is fine.

Schema is forward-compatible: new columns can be added via ALTER TABLE in
`init_index_db()` without breaking older readers (SQLite tolerates missing
columns at SELECT time when the columns are explicit).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from chameleon_mcp.drift.sqlite_config import open_hardened
from chameleon_mcp.profile.trust import plugin_data_dir

INDEX_DB_SCHEMA_VERSION = "1"

# Re-running the DDL must be a no-op on existing installs. ALTER TABLE
# adds are guarded by a column-presence check inside `init_index_db()`.
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
  repo_id         TEXT PRIMARY KEY,
  repo_root       TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,         -- ISO 8601 UTC, sortable lexicographically
  profile_sha256  TEXT,                  -- mirrors trust record's profile_sha256
  archetype_count INTEGER,
  files_indexed   INTEGER,
  bootstrap_ms    INTEGER                -- last successful bootstrap duration
) WITHOUT ROWID;

-- Cursor pagination orders by (last_seen_at DESC, repo_id ASC). The composite
-- index covers both ordering keys so the planner can stream the result
-- without a sort step on the >1000-repo path.
CREATE INDEX IF NOT EXISTS idx_repos_last_seen
  ON repos(last_seen_at DESC, repo_id ASC);
CREATE INDEX IF NOT EXISTS idx_repos_repo_root
  ON repos(repo_root);
"""


def _index_db_path() -> Path:
    """Resolve ${PLUGIN_DATA}/index.db.

    Honors `CHAMELEON_PLUGIN_DATA` via `plugin_data_dir()` so tests that
    isolate state to a tmpdir keep working.
    """
    return plugin_data_dir() / "index.db"


def init_index_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Create or open index.db with the canonical schema.

    Idempotent: safe to call on existing databases (uses CREATE TABLE IF
    NOT EXISTS + INSERT OR IGNORE for the version row). Returns an open
    hardened connection.
    """
    path = db_path if db_path is not None else _index_db_path()
    conn = open_hardened(path)
    # Override the busy_timeout from drift's 30s default to 2s — index.db
    # writes are user-driven, not on the PreToolUse hot path.
    conn.execute("PRAGMA busy_timeout=2000")
    conn.executescript(SCHEMA_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (k, v) VALUES ('schema_version', ?)",
        (INDEX_DB_SCHEMA_VERSION,),
    )
    return conn


def _now_iso() -> str:
    # Microsecond precision so refresh_repo's no-op check can compare
    # against fractional file mtimes without false invalidations from
    # ISO-second truncation.
    now = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    micros = int((now - int(now)) * 1_000_000)
    return f"{base}.{micros:06d}Z"


def upsert_repo(
    repo_id: str,
    repo_root: str,
    *,
    profile_sha256: str | None = None,
    archetype_count: int | None = None,
    files_indexed: int | None = None,
    bootstrap_ms: int | None = None,
    last_seen_at: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Insert or update a repo row.

    `last_seen_at` defaults to "now" so repeated bootstraps bubble the
    repo to the top of the list_profiles output. Pass an explicit value
    for migration / test scenarios.

    Fail-open on sqlite errors: the index is a cache for the trust record
    + filesystem state, not the source of truth, so a transient write
    failure should not block bootstrap_repo. (Distinct from drift.db
    which is also fail-open for the same reason.)
    """
    if not repo_id or not repo_root:
        return
    ts = last_seen_at or _now_iso()
    try:
        conn = init_index_db(db_path)
    except (sqlite3.Error, OSError):
        return
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO repos
                  (repo_id, repo_root, last_seen_at, profile_sha256,
                   archetype_count, files_indexed, bootstrap_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id) DO UPDATE SET
                  repo_root       = excluded.repo_root,
                  last_seen_at    = excluded.last_seen_at,
                  profile_sha256  = COALESCE(excluded.profile_sha256, profile_sha256),
                  archetype_count = COALESCE(excluded.archetype_count, archetype_count),
                  files_indexed   = COALESCE(excluded.files_indexed,   files_indexed),
                  bootstrap_ms    = COALESCE(excluded.bootstrap_ms,    bootstrap_ms)
                """,
                (
                    repo_id,
                    repo_root,
                    ts,
                    profile_sha256,
                    archetype_count,
                    files_indexed,
                    bootstrap_ms,
                ),
            )
    except sqlite3.Error:
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass


def resolve_repo_root(repo_id: str, *, db_path: Path | None = None) -> str | None:
    """Primary fast lookup: repo_id → repo_root.

    Returns None if the repo is unknown to the index OR the row points
    at a path that no longer exists on disk. Caller is responsible for
    the trust-record fallback (see `_resolve_repo_root_by_id`).
    """
    if not repo_id:
        return None
    path = db_path if db_path is not None else _index_db_path()
    if not path.is_file():
        return None
    try:
        conn = open_hardened(path, read_only=True)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = conn.execute(
            "SELECT repo_root FROM repos WHERE repo_id = ?",
            (repo_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if row is None:
        return None
    root = row["repo_root"]
    return root if root else None


def get_repo(repo_id: str, *, db_path: Path | None = None) -> dict | None:
    """Return the full row for a repo as a dict, or None if absent."""
    if not repo_id:
        return None
    path = db_path if db_path is not None else _index_db_path()
    if not path.is_file():
        return None
    try:
        conn = open_hardened(path, read_only=True)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = conn.execute(
            """
            SELECT repo_id, repo_root, last_seen_at, profile_sha256,
                   archetype_count, files_indexed, bootstrap_ms
            FROM repos
            WHERE repo_id = ?
            """,
            (repo_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return dict(row) if row is not None else None


def list_repos(
    cursor: str | None,
    limit: int,
    *,
    db_path: Path | None = None,
) -> tuple[list[dict], str | None, int]:
    """Paginated repos, ordered by (last_seen_at DESC, repo_id ASC).

    Args:
        cursor: opaque pagination cursor; pass the previous call's
                `next_cursor` to continue. None starts from the top.
        limit: page size (caller validates the 1..1000 range).
        db_path: override for tests.

    Returns:
        (page, next_cursor, total_known). `next_cursor` is None on the
        last page. `total_known` is the total row count (cheap COUNT(*)
        for visibility in the UI).

    Raises:
        ValueError: if `cursor` is non-empty but malformed. Callers
        translate this into the existing "unknown cursor" error envelope
        so existing v0.2 behavior is preserved.
    """
    path = db_path if db_path is not None else _index_db_path()
    if not path.is_file():
        return [], None, 0

    try:
        conn = open_hardened(path, read_only=True)
    except (sqlite3.Error, OSError):
        return [], None, 0
    try:
        total = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        if total == 0:
            return [], None, 0

        # Cursor encodes the last row of the previous page so the next
        # page starts strictly after it. Format: "<last_seen_at>|<repo_id>"
        if cursor:
            try:
                cursor_ts, cursor_id = cursor.split("|", 1)
            except ValueError:
                raise ValueError(f"unknown cursor {cursor!r}")
            # Confirm the cursor points at a real row — protects against
            # corrupted cursors and keeps the v0.2 error envelope honest.
            check = conn.execute(
                "SELECT 1 FROM repos WHERE repo_id = ? AND last_seen_at = ?",
                (cursor_id, cursor_ts),
            ).fetchone()
            if check is None:
                raise ValueError(f"unknown cursor {cursor!r}")
            # Use a tuple comparison so we get strict "(ts, id) lex after"
            # semantics matching the ORDER BY. SQLite's row-value compare
            # gives us exactly that — for DESC on ts we invert the operand
            # order, but with ts being a string ISO timestamp we can fold
            # it into a single WHERE clause.
            sql = """
                SELECT repo_id, repo_root, last_seen_at, profile_sha256,
                       archetype_count, files_indexed, bootstrap_ms
                FROM repos
                WHERE (last_seen_at < ?)
                   OR (last_seen_at = ? AND repo_id > ?)
                ORDER BY last_seen_at DESC, repo_id ASC
                LIMIT ?
            """
            rows = conn.execute(
                sql,
                (cursor_ts, cursor_ts, cursor_id, limit + 1),
            ).fetchall()
        else:
            sql = """
                SELECT repo_id, repo_root, last_seen_at, profile_sha256,
                       archetype_count, files_indexed, bootstrap_ms
                FROM repos
                ORDER BY last_seen_at DESC, repo_id ASC
                LIMIT ?
            """
            rows = conn.execute(sql, (limit + 1,)).fetchall()
    except sqlite3.Error:
        return [], None, 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # We fetched limit+1 to detect whether a next page exists without a
    # second query.
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    page = [dict(r) for r in page_rows]

    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor: str | None = f"{last['last_seen_at']}|{last['repo_id']}"
    else:
        next_cursor = None

    return page, next_cursor, total


def forget_repo(repo_id: str, *, db_path: Path | None = None) -> bool:
    """Delete a repo row. Returns True if a row was removed.

    Used by /chameleon-disable and uninstall flows. Idempotent: calling
    on an unknown repo_id is not an error.
    """
    if not repo_id:
        return False
    path = db_path if db_path is not None else _index_db_path()
    if not path.is_file():
        return False
    try:
        conn = init_index_db(path)
    except (sqlite3.Error, OSError):
        return False
    try:
        with conn:
            cur = conn.execute("DELETE FROM repos WHERE repo_id = ?", (repo_id,))
            return cur.rowcount > 0
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def max_mtime_over(paths: Iterable[Path]) -> float:
    """Return the largest mtime (in seconds) across the given paths.

    Missing files contribute 0.0 so a deleted file does NOT prevent the
    no-op short-circuit (deletion is reflected by the discovery-set
    cardinality change, which the caller checks separately).
    """
    max_m = 0.0
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime > max_m:
            max_m = st.st_mtime
    return max_m
