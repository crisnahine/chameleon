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

-- Inline `chameleon-ignore` override history. A block-eligible rule dropped by
-- an inline directive leaves no trace once the turn ends, so the override is
-- invisible to anyone auditing whether enforcement is actually holding. Each
-- bypass records one row here. Unlike edit_observations this is NOT reset on
-- refresh: the override record is durable per-repo history, since whether a
-- convention is fighting the team is a question that spans many profile
-- revisions. `blanket` is 1 when the directive named no rule (a bare
-- `chameleon-ignore` that downgrades every block-eligible rule on the file),
-- 0 when it targeted this specific rule by name.
CREATE TABLE IF NOT EXISTS rule_overrides (
  id INTEGER PRIMARY KEY,
  rel_path TEXT,
  rule TEXT NOT NULL,
  archetype TEXT,
  session_id TEXT,
  blanket INTEGER NOT NULL DEFAULT 0,  -- 0 or 1
  observed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_overrides_at ON rule_overrides(observed_at);
CREATE INDEX IF NOT EXISTS idx_rule_overrides_rule ON rule_overrides(rule, observed_at);
-- The Stop-path attestation embeds this session's overrides, so the lookup
-- keys by session rather than by time window.
CREATE INDEX IF NOT EXISTS idx_rule_overrides_session ON rule_overrides(session_id);

-- Per-edit decision log. When a defect escapes, a postmortem needs to
-- reconstruct what chameleon knew and did when that file was last edited:
-- which archetype matched, at what quality, what rules stood, and whether the
-- gate stayed silent. Each governed edit records one row here AFTER its outcome
-- is resolved. Like rule_overrides and unlike edit_observations, this is NOT
-- reset on refresh: closing a coverage gap (refresh/teach) must not destroy the
-- record of the escape being diagnosed, and the history a postmortem reads
-- spans many profile revisions.
--
-- `rel_path` is a true repo-relative path (relative_to the repo root), so the
-- log keys consistently across clones rather than on an absolute home path.
-- `match_quality` is none/fallback/exact/ast as resolved at archetype-match
-- time; none/fallback marks a coverage gap directly. `blockable_rules` is a
-- comma-joined list of the block-eligible rules that still stood on the file
-- (empty when none). `outcome` is one of advised / would-block / blocked /
-- overridden / clean. `content_digest` (16-hex sha256 of the verified content
-- window) is the replay key: it pins the row to the exact bytes the verifier
-- saw, so a snapshot reader resolves the decision that governed THIS content
-- rather than whatever edit came last. NULL on rows written before the column
-- existed; those are intentionally never matched by digest queries.
CREATE TABLE IF NOT EXISTS decision_log (
  id INTEGER PRIMARY KEY,
  rel_path TEXT NOT NULL,
  archetype TEXT,
  match_quality TEXT,
  confidence_band TEXT,
  violations_raised INTEGER NOT NULL DEFAULT 0,
  blockable_rules TEXT,
  content_digest TEXT,
  outcome TEXT NOT NULL,
  session_id TEXT,
  observed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_log_at ON decision_log(observed_at);
CREATE INDEX IF NOT EXISTS idx_decision_log_path ON decision_log(rel_path, observed_at);
CREATE INDEX IF NOT EXISTS idx_decision_log_digest ON decision_log(rel_path, content_digest);

-- RETIRED: nothing reads or writes this table anymore (see below). Was the
-- surfaced-finding ledger (the finding->fix loop): the correctness judge and
-- the multi-lens review can
-- never block; nothing tracked whether a surfaced advisory was ever acted
-- on, so a dropped high-severity finding was simply lost with zero telemetry
-- on advisory efficacy. Each surfaced finding wrote one row here at Stop; the
-- next Stop re-checked the anchor (the reviewed file's content digest) to
-- classify it addressed (the cited content changed) vs still-open, and an
-- unaddressed high-severity finding re-surfaced ONCE. `fingerprint` was the
-- per-(lens, file, locus) dedup key so the same finding across turns was one
-- logical row; `anchor_digest` was the 16-hex content digest of the reviewed
-- file at review time (the addressed/ignored proxy); `status` walked open /
-- addressed / ignored / resurfaced. `ws_root` was the absolute workspace root
-- that persisted the row, scoping a monorepo's shared-repo_id workspaces to
-- their own findings.
--
-- The async-first cutover moved the finding->fix loop to `review_ledger.py`'s
-- `findings_ledger.json` (one JSON row per repo, not a drift.db table); this
-- table's writer (`record_judge_finding`) and reader
-- (`open_judge_findings`/`mark_judge_finding`) were retired with it. Any HIGH
-- finding left open here from before the cutover is not migrated -- a
-- documented, low-impact gap (unlike the `.judge_pending.<session>.json`
-- queue, which IS migrated via `review_ledger.migrate_pending_queue`). The
-- DDL is left in place rather than dropped, since drift.db is a cache a
-- schema bump can drop-and-recreate freely anyway.
CREATE TABLE IF NOT EXISTS judge_findings (
  id INTEGER PRIMARY KEY,
  session_id TEXT,
  lens TEXT NOT NULL,
  severity TEXT,
  rel_path TEXT,
  line INTEGER,
  anchor_digest TEXT,
  fingerprint TEXT NOT NULL,
  ws_root TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  observed_at INTEGER NOT NULL,
  resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_judge_findings_session ON judge_findings(session_id);
CREATE INDEX IF NOT EXISTS idx_judge_findings_fp ON judge_findings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_judge_findings_status ON judge_findings(status, ws_root);
CREATE INDEX IF NOT EXISTS idx_judge_findings_at ON judge_findings(observed_at);
"""


def _migrate_decision_log(conn: sqlite3.Connection) -> None:
    """Add decision_log.content_digest in place on databases created before it.

    decision_log (like rule_overrides) is durable postmortem history, so the
    module's drop-and-recreate cache policy does not apply here: an ALTER
    preserves the existing rows where a rebuild would destroy the record of
    past escapes. Legacy rows keep a NULL digest on purpose -- a digest query
    must never mis-join old content onto a new edit, so NULL rows are only
    reachable through the explicit same-session fallback in the reader.

    Runs under the short init busy_timeout. A sqlite3.OperationalError is
    treated as done-or-skipped: "duplicate column name" means a concurrent
    process won the ALTER race (which is success), and lock contention leaves
    the column for a later open to add (the fail-open drift writers skip rows
    until then).
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(decision_log)").fetchall()}
        if not cols or "content_digest" in cols:
            # Fresh database (SCHEMA_DDL creates the table with the column) or
            # already migrated.
            return
        with conn:
            conn.execute("ALTER TABLE decision_log ADD COLUMN content_digest TEXT")
    except sqlite3.OperationalError:
        return


def _open_and_init(db_path: Path) -> sqlite3.Connection:
    """Open drift.db and run the schema-init write under a short busy_timeout."""
    conn = open_hardened(db_path)
    conn.execute(f"PRAGMA busy_timeout={_INIT_BUSY_TIMEOUT_MS}")
    # Migrate BEFORE the DDL script: SCHEMA_DDL indexes
    # decision_log(content_digest), and CREATE INDEX against a pre-migration
    # table that still lacks the column would fail every open of a legacy db.
    _migrate_decision_log(conn)
    conn.executescript(SCHEMA_DDL)
    # Commit before returning. Under isolation_level="" this INSERT opens an
    # implicit write transaction; left pending, a connection that only ever
    # reads afterward (the long-lived MCP server) holds the single WAL writer
    # lock for its whole lifetime and starves every hook-process write at its
    # 200ms busy_timeout — silently, because drift writes are fail-open.
    with conn:
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
