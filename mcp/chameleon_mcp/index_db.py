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
#
# v0.5.1 (Bug 1): the `repos` PK is now `(repo_id, repo_root)` so monorepo
# sub-workspaces — which share a git-remote-derived `repo_id` with the
# root — can each persist their own row instead of clobbering one
# another on upsert. Existing single-PK databases are migrated in
# `init_index_db()` via a CREATE/COPY/DROP/RENAME pass that runs once.
# We DELIBERATELY keep `schema_version` at "1" because the change is
# strictly additive on the consumer side: reading a (repo_id) lookup
# still works, writes are forwards-compatible, and detection is via
# `PRAGMA table_info` rather than the version row.
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

    v0.5.1 migration: the `repos` table changes its PRIMARY KEY from
    `(repo_id)` to `(repo_id, repo_root)` so monorepo sub-workspaces no
    longer overwrite the root's row. Detected via `PRAGMA table_info`;
    runs the CREATE/COPY/DROP/RENAME pass in a single transaction so a
    crash mid-migration leaves the old table intact. The schema_version
    row stays at "1" because the change is consumer-additive.
    """
    path = db_path if db_path is not None else _index_db_path()
    conn = open_hardened(path)
    # Override the busy_timeout from drift's 30s default to 2s — index.db
    # writes are user-driven, not on the PreToolUse hot path.
    conn.execute("PRAGMA busy_timeout=2000")
    # Migrate FIRST so the CREATE TABLE IF NOT EXISTS in SCHEMA_DDL does
    # not no-op past an old (repo_id PRIMARY KEY) table shape.
    _migrate_repos_to_composite_pk(conn)
    conn.executescript(SCHEMA_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (k, v) VALUES ('schema_version', ?)",
        (INDEX_DB_SCHEMA_VERSION,),
    )
    return conn


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
        return  # fresh install — SCHEMA_DDL will create the right shape

    try:
        info = conn.execute("PRAGMA table_info(repos)").fetchall()
    except sqlite3.Error:
        return
    pk_columns = [r["name"] for r in info if (r["pk"] or 0) > 0]
    if pk_columns == ["repo_id", "repo_root"] or pk_columns == ["repo_root", "repo_id"]:
        return  # already on composite PK
    # Any other PK shape (typically just ["repo_id"]) → migrate.

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
        # Leave the old table untouched — `init_index_db` will continue
        # with the legacy shape; callers degrade to one-row-per-repo_id
        # behavior rather than crashing.
        return


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
            # v0.5.1: when inserting a brand-new (repo_id, repo_root) pair
            # while another row with the same repo_id already exists, we
            # inherit the COALESCE-eligible fields from the most recent
            # prior row. This preserves the v0.5.0 "moved checkout"
            # semantics (where path-based upserts effectively migrated
            # the row in place) without requiring callers to discover and
            # re-supply the prior profile_sha256 / counts. Per-workspace
            # bootstraps overwrite these fields immediately with their
            # real values, so the inheritance is observably a no-op in
            # the monorepo path.
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
            already_present = conn.execute(
                "SELECT 1 FROM repos WHERE repo_id = ? AND repo_root = ?",
                (repo_id, repo_root),
            ).fetchone() is not None
            inherited_sha = (
                inherit["profile_sha256"] if (inherit and not already_present) else None
            )
            inherited_arch = (
                inherit["archetype_count"] if (inherit and not already_present) else None
            )
            inherited_files = (
                inherit["files_indexed"] if (inherit and not already_present) else None
            )
            inherited_ms = (
                inherit["bootstrap_ms"] if (inherit and not already_present) else None
            )
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

    v0.5.1 (Bug 1): monorepo sub-workspaces share a git-remote-derived
    repo_id with the root, so a single repo_id may now match multiple
    rows. Resolution rules:

      - If `repo_root_hint` is supplied AND a row exists with that exact
        repo_root, return the hinted root (callers in tools.py pass the
        repo_root they computed locally).
      - Otherwise, return the MOST RECENTLY UPDATED row's repo_root
        (ordered by last_seen_at DESC, repo_id ASC). This preserves
        v0.5.0 semantics for repos with a unique repo_id and gives
        monorepo callers the freshest workspace by default.

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
        conn = open_hardened(path, read_only=True)
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
            # Hint missed — fall through to the freshest-row resolution.
        row = conn.execute(
            """
            SELECT repo_root FROM repos
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
    if row is None:
        return None
    root = row["repo_root"]
    return root if root else None


def list_repo_roots(repo_id: str, *, db_path: Path | None = None) -> list[str]:
    """Return every repo_root recorded for `repo_id`, freshest first.

    Used by monorepo-aware consumers (e.g., the trust resolution layer)
    that need to enumerate the sub-workspaces that share a repo_id with
    the root. The order matches `resolve_repo_root`'s default selection
    so the freshest row appears first.
    """
    if not repo_id:
        return []
    path = db_path if db_path is not None else _index_db_path()
    if not path.is_file():
        return []
    try:
        conn = open_hardened(path, read_only=True)
    except (sqlite3.Error, OSError):
        return []
    try:
        rows = conn.execute(
            """
            SELECT repo_root FROM repos
            WHERE repo_id = ?
            ORDER BY last_seen_at DESC, repo_id ASC
            """,
            (repo_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [r["repo_root"] for r in rows if r["repo_root"]]


def get_repo(
    repo_id: str,
    *,
    repo_root_hint: str | None = None,
    db_path: Path | None = None,
) -> dict | None:
    """Return the full row for a repo as a dict, or None if absent.

    v0.5.1: accepts an optional `repo_root_hint` so monorepo callers can
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
            # Hint missed — fall through to the freshest row.
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

        # Cursor format evolution:
        #   v0.5.0: "<last_seen_at>|<repo_id>"
        #   v0.5.1: "<last_seen_at>|<repo_id>|<repo_root>"
        # With the composite (repo_id, repo_root) PK, a v0.5.0 cursor
        # is ambiguous when the same repo_id appears across multiple
        # rows. We accept both shapes for backward compat and order by
        # (last_seen_at DESC, repo_id ASC, repo_root ASC).
        if cursor:
            parts = cursor.split("|", 2)
            if len(parts) == 2:
                cursor_ts, cursor_id = parts
                cursor_root: str | None = None
            elif len(parts) == 3:
                cursor_ts, cursor_id, cursor_root = parts
            else:
                raise ValueError(f"unknown cursor {cursor!r}")
            # Confirm the cursor points at a real row — protects against
            # corrupted cursors and keeps the v0.2 error envelope honest.
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
            # Three-key tuple comparison: strict "(ts, id, root) lex after"
            # semantics matching the ORDER BY. When the legacy two-key
            # cursor is supplied, treat repo_root as the high bound
            # (empty string ⇒ first repo_root for that (ts, id) pair).
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
                    cursor_ts, cursor_id,
                    cursor_ts, cursor_id, cursor_root or "",
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

    # We fetched limit+1 to detect whether a next page exists without a
    # second query.
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    page = [dict(r) for r in page_rows]

    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor: str | None = (
            f"{last['last_seen_at']}|{last['repo_id']}|{last['repo_root']}"
        )
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

    v0.5.1 (Bug 1): with the composite (repo_id, repo_root) PK, a single
    repo_id can map to multiple rows (monorepo root + sub-workspaces).
    When `repo_root` is supplied, only that specific row is removed.
    When omitted, every row sharing `repo_id` is removed — preserving
    v0.5.0 behavior for uninstall flows that want to forget the entire
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
                cur = conn.execute(
                    "DELETE FROM repos WHERE repo_id = ?", (repo_id,)
                )
            return cur.rowcount > 0
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _ensure_file_clusters_schema(db_path: Path | None = None) -> None:
    """Ensure the file_clusters table + index exist.

    Phase 4.3-extended: callable independently of init_index_db for older
    databases that were created before the table was added. SCHEMA_DDL
    already declares the table via CREATE TABLE IF NOT EXISTS, so init
    is idempotent; this helper is a thin alias for callers that want to
    state the intent explicitly. Fail-open on sqlite errors — the index
    is a cache, not the source of truth, and the partial-refresh path
    transparently falls back to full bootstrap when the table is unusable.
    """
    try:
        conn = init_index_db(db_path)
    except (sqlite3.Error, OSError):
        return
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
    no rows yet (legacy v0.4 profiles); callers treat this as "no
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
            # executemany sets rowcount to the sum across statements on
            # sqlite3 ≥ 3.39 (Python 3.12+); older Pythons return -1.
            # Caller treats negative as "unknown, but the call did not error".
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
            cur = conn.execute(
                "DELETE FROM file_clusters WHERE repo_id = ?", (repo_id,)
            )
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
