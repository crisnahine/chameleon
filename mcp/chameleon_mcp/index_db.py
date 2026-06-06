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

_INDEX_CONN: sqlite3.Connection | None = None


def _get_index_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """Return a cached connection to index.db, creating if needed.

    Health-checks the cached connection with ``SELECT 1``. If the check
    fails, drops the cache and opens a fresh one via ``init_index_db``.

    Only caches when ``db_path`` is None (the default production path).
    Test overrides with explicit ``db_path`` bypass the cache to avoid
    cross-test contamination.

    Raises sqlite3.Error or OSError on failure.
    """
    global _INDEX_CONN
    if db_path is not None:
        return init_index_db(db_path)
    if _INDEX_CONN is not None:
        try:
            _INDEX_CONN.execute("SELECT 1")
            return _INDEX_CONN
        except sqlite3.Error:
            try:
                _INDEX_CONN.close()
            except Exception:
                pass
            _INDEX_CONN = None
    conn = init_index_db()
    _INDEX_CONN = conn
    return conn


def _get_index_conn_readonly(db_path: Path | None = None) -> tuple[sqlite3.Connection, bool]:
    """Open a read-only connection to index.db, skipping DDL.

    BUG-032: init_index_db runs CREATE TABLE + INSERT on every connection,
    which requires a write lock. When the process already holds write locks
    from prior upserts (connection leak), the DDL deadlocks. Read-only
    callers (resolve_repo_root, get_repo) don't need DDL — if the table
    doesn't exist, the SELECT fails with "no such table" which is handled
    the same as "not found".

    Returns ``(conn, owned)``. When ``owned`` is True the caller opened a
    fresh connection and must close it; when False the connection is the
    cached read-write handle and must be left open.
    """
    global _INDEX_CONN
    if db_path is None and _INDEX_CONN is not None:
        try:
            _INDEX_CONN.execute("SELECT 1")
            return _INDEX_CONN, False
        except sqlite3.Error:
            pass
    path = db_path if db_path is not None else _index_db_path()
    if not path.is_file():
        raise sqlite3.OperationalError("index.db does not exist")
    conn = open_hardened(path, read_only=True)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn, True


def close_index_connections() -> None:
    """Close the cached index.db connection. Safe to call at shutdown."""
    global _INDEX_CONN
    if _INDEX_CONN is not None:
        try:
            _INDEX_CONN.close()
        except Exception:
            pass
        _INDEX_CONN = None


INDEX_DB_SCHEMA_VERSION = "1"

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
  repo_id         TEXT NOT NULL,
  repo_root       TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,         -- ISO 8601 UTC, sortable lexicographically
  profile_sha256  TEXT,                  -- mirrors trust record's profile_sha256
  archetype_count INTEGER,
  files_indexed   INTEGER,
  bootstrap_ms    INTEGER,               -- last successful bootstrap duration
  PRIMARY KEY (repo_id, repo_root)
) WITHOUT ROWID;

-- Cursor pagination orders by (last_seen_at DESC, repo_id ASC). The composite
-- index covers both ordering keys so the planner can stream the result
-- without a sort step on the >1000-repo path.
CREATE INDEX IF NOT EXISTS idx_repos_last_seen
  ON repos(last_seen_at DESC, repo_id ASC);
CREATE INDEX IF NOT EXISTS idx_repos_repo_root
  ON repos(repo_root);
CREATE INDEX IF NOT EXISTS idx_repos_repo_id
  ON repos(repo_id);

-- Phase 4.3-extended: per-file cluster assignment. Populated by
-- bootstrap_repo after a successful run; consulted by refresh_repo to
-- decide whether a partial re-clustering is viable (change_ratio <= 10%)
-- vs. a full re-bootstrap. Additive table; legacy installs without rows
-- transparently fall through to full re-bootstrap.
CREATE TABLE IF NOT EXISTS file_clusters (
  repo_id      TEXT NOT NULL,
  rel_path     TEXT NOT NULL,
  cluster_id   TEXT NOT NULL,
  sha_hint     TEXT,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_file_clusters_repo ON file_clusters(repo_id);
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

    Migration: the `repos` table changes its PRIMARY KEY from
    `(repo_id)` to `(repo_id, repo_root)` so monorepo sub-workspaces no
    longer overwrite the root's row. Detected via `PRAGMA table_info`;
    runs the CREATE/COPY/DROP/RENAME pass in a single transaction so a
    crash mid-migration leaves the old table intact. The schema_version
    row stays at "1" because the change is consumer-additive.
    """
    if db_path is None:
        from chameleon_mcp.plugin_paths import ensure_plugin_data_dir

        ensure_plugin_data_dir()
        path = _index_db_path()
    else:
        path = db_path
        path.parent.mkdir(parents=True, exist_ok=True)

    def _open_and_setup() -> sqlite3.Connection:
        conn = open_hardened(path)
        conn.execute("PRAGMA busy_timeout=5000")
        _migrate_repos_to_composite_pk(conn)
        conn.executescript(SCHEMA_DDL)
        # Commit before returning: an uncommitted INSERT pins the WAL writer
        # lock for the connection's lifetime, starving every other process's
        # write (same failure class as drift.db's schema-init — see
        # drift/schema.py).
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (k, v) VALUES ('schema_version', ?)",
                (INDEX_DB_SCHEMA_VERSION,),
            )
        return conn

    try:
        return _open_and_setup()
    except sqlite3.DatabaseError as exc:
        if not _is_corruption_error(exc):
            raise
        # index.db is a derived cache of repo metadata: corruption (truncated
        # header, overwritten pages) otherwise persists forever because every
        # reader fails open and nothing ever rewrites the file. Rebuilding
        # from scratch loses only re-derivable rows.
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(f"{path}{suffix}").unlink()
            except OSError:
                pass
        return _open_and_setup()


def _is_corruption_error(exc: sqlite3.Error) -> bool:
    """True for the sqlite errors that mean the file itself is damaged.

    Lock contention and missing tables are OperationalErrors with different
    messages and must NOT trigger a rebuild.
    """
    msg = str(exc).lower()
    return "not a database" in msg or "malformed" in msg or "corrupt" in msg


def _migrate_repos_to_composite_pk(conn: sqlite3.Connection) -> None:
    """One-time migration: `repos` PK `(repo_id)` → `(repo_id, repo_root)`.

    Bug 1 fix: monorepo sub-workspaces share a git-remote-derived repo_id
    with the root, so the old single-column PK silently clobbered rows on
    upsert. Detect via PRAGMA table_info — if the existing `repos` table
    declares exactly one PK column, we copy every row into a fresh table
    with the composite PK and drop the old one. Wrapped in a single
    transaction so a crash mid-migration leaves the v1 table intact.

    On fresh installs (no `repos` table yet) this is a no-op.
    """
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='repos'"
        ).fetchone()
    except sqlite3.Error:
        return
    if row is None:
        return

    try:
        info = conn.execute("PRAGMA table_info(repos)").fetchall()
    except sqlite3.Error:
        return
    pk_columns = [r["name"] for r in info if (r["pk"] or 0) > 0]
    if pk_columns == ["repo_id", "repo_root"] or pk_columns == ["repo_root", "repo_id"]:
        return

    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repos_v2 (
                  repo_id         TEXT NOT NULL,
                  repo_root       TEXT NOT NULL,
                  last_seen_at    TEXT NOT NULL,
                  profile_sha256  TEXT,
                  archetype_count INTEGER,
                  files_indexed   INTEGER,
                  bootstrap_ms    INTEGER,
                  PRIMARY KEY (repo_id, repo_root)
                ) WITHOUT ROWID
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO repos_v2
                  (repo_id, repo_root, last_seen_at, profile_sha256,
                   archetype_count, files_indexed, bootstrap_ms)
                SELECT repo_id, repo_root, last_seen_at, profile_sha256,
                       archetype_count, files_indexed, bootstrap_ms
                FROM repos
                """
            )
            conn.execute("DROP TABLE repos")
            conn.execute("ALTER TABLE repos_v2 RENAME TO repos")
    except sqlite3.Error:
        return


def _now_iso() -> str:
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
        conn = _get_index_conn(db_path)
    except (sqlite3.Error, OSError):
        return
    # A caller-supplied db_path bypasses the cache and returns a fresh owned
    # connection; close it here. The default cached connection stays open.
    owns_conn = db_path is not None
    try:
        with conn:
            inherit = conn.execute(
                """
                SELECT profile_sha256, archetype_count, files_indexed, bootstrap_ms
                FROM repos
                WHERE repo_id = ? AND repo_root != ?
                ORDER BY last_seen_at DESC, repo_root ASC
                LIMIT 1
                """,
                (repo_id, repo_root),
            ).fetchone()
            already_present = (
                conn.execute(
                    "SELECT 1 FROM repos WHERE repo_id = ? AND repo_root = ?",
                    (repo_id, repo_root),
                ).fetchone()
                is not None
            )
            inherited_sha = inherit["profile_sha256"] if (inherit and not already_present) else None
            inherited_arch = (
                inherit["archetype_count"] if (inherit and not already_present) else None
            )
            inherited_files = (
                inherit["files_indexed"] if (inherit and not already_present) else None
            )
            inherited_ms = inherit["bootstrap_ms"] if (inherit and not already_present) else None
            conn.execute(
                """
                INSERT INTO repos
                  (repo_id, repo_root, last_seen_at, profile_sha256,
                   archetype_count, files_indexed, bootstrap_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, repo_root) DO UPDATE SET
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
                    profile_sha256 if profile_sha256 is not None else inherited_sha,
                    archetype_count if archetype_count is not None else inherited_arch,
                    files_indexed if files_indexed is not None else inherited_files,
                    bootstrap_ms if bootstrap_ms is not None else inherited_ms,
                ),
            )
    except sqlite3.Error:
        return
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass


def resolve_repo_root(
    repo_id: str,
    *,
    repo_root_hint: str | None = None,
    db_path: Path | None = None,
) -> str | None:
    """Primary fast lookup: repo_id → repo_root.

    Bug 1: monorepo sub-workspaces share a git-remote-derived
    repo_id with the root, so a single repo_id may now match multiple
    rows.

    Bug H: when the workspace-bootstrap path in tools.py inserts
    a row PER workspace (e.g., plane: 1 root + 17 ``packages/*`` /
    ``apps/*`` rows, all sharing the same repo_id), the previous
    freshest-row rule returned the alphabetically-last workspace
    (``packages/utils``). Downstream consumers (get_canonical_excerpt,
    get_drift_status) then loaded the wrong .chameleon/ and silently
    emitted ``archetype not found`` for valid archetypes. This makes
    the resolver ANCESTOR-AWARE.

    Resolution order:
      1. If ``repo_root_hint`` matches a row exactly, return it
         (unchanged).
      2. If multiple rows exist for this repo_id, prefer the row whose
         ``repo_root`` is an ancestor of (or equal to) every other row's
         ``repo_root`` — i.e., the actual repo root, not a workspace.
         When several rows are mutually ancestors of nothing (rare —
         can happen with sibling clones), fall back to the freshest.
      3. Otherwise (single row, or no ancestor relation), return the
         most recently updated row.

    Returns None if the repo is unknown to the index OR the resolved row
    points at a path that no longer exists on disk. Caller is responsible
    for the trust-record fallback (see `_resolve_repo_root_by_id`).
    """
    if not repo_id:
        return None
    path = db_path if db_path is not None else _index_db_path()
    if not path.is_file():
        return None
    try:
        conn, owns_conn = _get_index_conn_readonly(db_path)
    except (sqlite3.Error, OSError):
        return None
    try:
        if repo_root_hint:
            row = conn.execute(
                "SELECT repo_root FROM repos WHERE repo_id = ? AND repo_root = ?",
                (repo_id, repo_root_hint),
            ).fetchone()
            if row is not None:
                root = row["repo_root"]
                return root if root else None

        rows = conn.execute(
            """
            SELECT repo_root FROM repos
            WHERE repo_id = ?
            ORDER BY last_seen_at DESC, repo_id ASC
            """,
            (repo_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if owns_conn:
            conn.close()
    if not rows:
        return None
    candidates = [r["repo_root"] for r in rows if r["repo_root"]]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return _pick_ancestor_or_freshest(candidates)


def _pick_ancestor_or_freshest(candidates: list[str]) -> str:
    """Return the root whose path is an ancestor of every other candidate.

    Bug H. When ``bootstrap_repo`` runs on a monorepo, the
    workspace-completion path inserts a row per workspace using
    ``_compute_repo_id(ws_root)``. Because ``_compute_repo_id`` hashes
    the git remote URL, every workspace and the root collapse to the
    same ``repo_id``. The resolver must disambiguate by path topology.

    Algorithm:
      1. Resolve each candidate to a canonical absolute path.
      2. For each candidate, count how many other candidates sit under it
         (strict descendants). The one with the maximum descendant count
         is the ancestor-most.
      3. Tie-break: the candidate with the shortest path string wins
         (ancestors are always shorter). If still tied, fall back to the
         original order (freshest first).

    Returns the ORIGINAL string (not the resolved Path) so callers
    comparing against ``upsert_repo`` insertion keys see the same value.
    """
    pairs: list[tuple[str, Path | None]] = []
    for c in candidates:
        try:
            pairs.append((c, Path(c).resolve()))
        except (OSError, ValueError):
            pairs.append((c, None))

    best_idx = 0
    best_descendants = -1
    best_path_len = float("inf")
    for i, (_, p_i) in enumerate(pairs):
        if p_i is None:
            continue
        descendants = 0
        for j, (_, p_j) in enumerate(pairs):
            if i == j or p_j is None:
                continue
            try:
                if p_j != p_i and p_i in p_j.parents:
                    descendants += 1
            except (OSError, ValueError):
                continue
        path_len = len(str(p_i))
        if descendants > best_descendants or (
            descendants == best_descendants and path_len < best_path_len
        ):
            best_idx = i
            best_descendants = descendants
            best_path_len = path_len
    return pairs[best_idx][0]


def get_repo(
    repo_id: str,
    *,
    repo_root_hint: str | None = None,
    db_path: Path | None = None,
) -> dict | None:
    """Return the full row for a repo as a dict, or None if absent.

    Accepts an optional `repo_root_hint` so monorepo callers can
    pinpoint a specific sub-workspace row when the repo_id alone is
    ambiguous. Without a hint, the freshest matching row wins (same
    ordering as `resolve_repo_root`).
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
        if repo_root_hint:
            row = conn.execute(
                """
                SELECT repo_id, repo_root, last_seen_at, profile_sha256,
                       archetype_count, files_indexed, bootstrap_ms
                FROM repos
                WHERE repo_id = ? AND repo_root = ?
                """,
                (repo_id, repo_root_hint),
            ).fetchone()
            if row is not None:
                return dict(row)
        row = conn.execute(
            """
            SELECT repo_id, repo_root, last_seen_at, profile_sha256,
                   archetype_count, files_indexed, bootstrap_ms
            FROM repos
            WHERE repo_id = ?
            ORDER BY last_seen_at DESC, repo_id ASC
            LIMIT 1
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
        so existing behavior is preserved.
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

        if cursor:
            parts = cursor.split("|", 2)
            if len(parts) == 2:
                cursor_ts, cursor_id = parts
                cursor_root: str | None = None
            elif len(parts) == 3:
                cursor_ts, cursor_id, cursor_root = parts
            else:
                raise ValueError(f"unknown cursor {cursor!r}")
            if cursor_root is not None:
                check = conn.execute(
                    """
                    SELECT 1 FROM repos
                    WHERE repo_id = ? AND last_seen_at = ? AND repo_root = ?
                    """,
                    (cursor_id, cursor_ts, cursor_root),
                ).fetchone()
            else:
                check = conn.execute(
                    "SELECT 1 FROM repos WHERE repo_id = ? AND last_seen_at = ?",
                    (cursor_id, cursor_ts),
                ).fetchone()
            if check is None:
                raise ValueError(f"unknown cursor {cursor!r}")
            sql = """
                SELECT repo_id, repo_root, last_seen_at, profile_sha256,
                       archetype_count, files_indexed, bootstrap_ms
                FROM repos
                WHERE (last_seen_at < ?)
                   OR (last_seen_at = ? AND repo_id > ?)
                   OR (last_seen_at = ? AND repo_id = ? AND repo_root > ?)
                ORDER BY last_seen_at DESC, repo_id ASC, repo_root ASC
                LIMIT ?
            """
            rows = conn.execute(
                sql,
                (
                    cursor_ts,
                    cursor_ts,
                    cursor_id,
                    cursor_ts,
                    cursor_id,
                    cursor_root or "",
                    limit + 1,
                ),
            ).fetchall()
        else:
            sql = """
                SELECT repo_id, repo_root, last_seen_at, profile_sha256,
                       archetype_count, files_indexed, bootstrap_ms
                FROM repos
                ORDER BY last_seen_at DESC, repo_id ASC, repo_root ASC
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

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    page = [dict(r) for r in page_rows]

    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor: str | None = f"{last['last_seen_at']}|{last['repo_id']}|{last['repo_root']}"
    else:
        next_cursor = None

    return page, next_cursor, total


def forget_repo(
    repo_id: str,
    *,
    repo_root: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Delete repo row(s). Returns True if at least one row was removed.

    Bug 1: with the composite (repo_id, repo_root) PK, a single
    repo_id can map to multiple rows (monorepo root + sub-workspaces).
    When `repo_root` is supplied, only that specific row is removed.
    When omitted, every row sharing `repo_id` is removed — preserving
    Behavior for uninstall flows that want to forget the entire
    project at once.

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
            if repo_root:
                cur = conn.execute(
                    "DELETE FROM repos WHERE repo_id = ? AND repo_root = ?",
                    (repo_id, repo_root),
                )
            else:
                cur = conn.execute("DELETE FROM repos WHERE repo_id = ?", (repo_id,))
            return cur.rowcount > 0
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def upsert_file_clusters(
    repo_id: str,
    rows: Iterable[tuple[str, str, str | None]],
    *,
    last_seen_at: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Insert or update per-file cluster assignment rows.

    Each `rows` entry is `(rel_path, cluster_id, sha_hint)`. The
    `last_seen_at` column is filled with the supplied timestamp (or
    "now" when omitted) so all rows from a single bootstrap/partial
    refresh share a stable observation moment that downstream consumers
    can range-query against.

    Fail-open on sqlite errors: a transient write failure must not block
    the calling bootstrap/refresh — file_clusters is opportunistic state
    that's reconstructable from a full re-bootstrap.
    """
    if not repo_id:
        return
    materialized = list(rows)
    if not materialized:
        return
    ts = last_seen_at or _now_iso()
    try:
        conn = init_index_db(db_path)
    except (sqlite3.Error, OSError):
        return
    try:
        with conn:
            conn.executemany(
                """
                INSERT INTO file_clusters
                  (repo_id, rel_path, cluster_id, sha_hint, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, rel_path) DO UPDATE SET
                  cluster_id   = excluded.cluster_id,
                  sha_hint     = excluded.sha_hint,
                  last_seen_at = excluded.last_seen_at
                """,
                [
                    (repo_id, rel_path, cluster_id, sha_hint, ts)
                    for rel_path, cluster_id, sha_hint in materialized
                ],
            )
    except sqlite3.Error:
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_file_clusters(
    repo_id: str, *, db_path: Path | None = None
) -> dict[str, dict[str, str | None]]:
    """Return prev-state per-file cluster assignment for a repo.

    Result shape: `{rel_path: {"cluster_id": str, "sha_hint": str|None,
    "last_seen_at": str}}`. Empty dict when the repo is unknown or has
    no rows yet (legacy profiles); callers treat this as "no
    partial-refresh state available, fall through to full bootstrap".
    """
    if not repo_id:
        return {}
    path = db_path if db_path is not None else _index_db_path()
    if not path.is_file():
        return {}
    try:
        conn = open_hardened(path, read_only=True)
    except (sqlite3.Error, OSError):
        return {}
    try:
        rows = conn.execute(
            """
            SELECT rel_path, cluster_id, sha_hint, last_seen_at
            FROM file_clusters
            WHERE repo_id = ?
            """,
            (repo_id,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {
        r["rel_path"]: {
            "cluster_id": r["cluster_id"],
            "sha_hint": r["sha_hint"],
            "last_seen_at": r["last_seen_at"],
        }
        for r in rows
    }


def delete_file_clusters_for_paths(
    repo_id: str,
    rel_paths: Iterable[str],
    *,
    db_path: Path | None = None,
) -> int:
    """Bulk-delete per-file cluster rows. Returns the number removed.

    Used by the partial-refresh path when a previously-tracked file has
    been removed from the discovery set (deleted from disk, moved into
    an excluded directory). Fail-open: a delete error returns 0 rather
    than propagating, because a stale row only over-reports cluster
    membership; the next full bootstrap cleans it up.
    """
    if not repo_id:
        return 0
    materialized = list(rel_paths)
    if not materialized:
        return 0
    try:
        conn = init_index_db(db_path)
    except (sqlite3.Error, OSError):
        return 0
    try:
        with conn:
            cur = conn.executemany(
                "DELETE FROM file_clusters WHERE repo_id = ? AND rel_path = ?",
                [(repo_id, rel_path) for rel_path in materialized],
            )
            return cur.rowcount if cur.rowcount >= 0 else len(materialized)
    except sqlite3.Error:
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def delete_all_file_clusters(repo_id: str, *, db_path: Path | None = None) -> int:
    """Remove every per-file cluster row for a repo.

    Called when the partial-refresh path falls through to full bootstrap
    and we want to rebuild the table from scratch instead of leaving
    stale rows alongside the fresh ones. Caller is expected to repopulate
    via upsert_file_clusters within the same logical transaction.
    """
    if not repo_id:
        return 0
    try:
        conn = init_index_db(db_path)
    except (sqlite3.Error, OSError):
        return 0
    try:
        with conn:
            cur = conn.execute("DELETE FROM file_clusters WHERE repo_id = ?", (repo_id,))
            return cur.rowcount if cur.rowcount >= 0 else 0
    except sqlite3.Error:
        return 0
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
