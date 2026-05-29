"""Per-edit drift observation recording.

Each PreToolUse Edit/Write/NotebookEdit invocation produces an observation:
"file <rel_path> matched archetype <X> with confidence <C> at <ts>". These
accumulate in drift.db's `edit_observations` table and power
`get_drift_status` — when too many recent edits land on archetypes with low
confidence, the repo's profile has drifted and `/chameleon-refresh` is
recommended.

Per docs/architecture.md "drift.db" subsection.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from chameleon_mcp.drift.schema import init_drift_db
from chameleon_mcp.profile.trust import plugin_data_dir

_DRIFT_CONN: dict[str, sqlite3.Connection] = {}


def _get_drift_conn(repo_id: str) -> sqlite3.Connection:
    """Return a cached connection to the repo's drift.db, creating if needed.

    Health-checks the cached connection with ``SELECT 1``. If the check
    fails (e.g. database was deleted and recreated, or the connection
    went stale), drops the cache entry and opens a fresh one via
    ``init_drift_db``.

    BUG-031: after opening, overrides ``busy_timeout`` to 200ms. The
    hardened default (30s) is appropriate for long-lived MCP server
    processes that can afford to wait, but drift writes happen on the
    hook hot path where the total budget is 3 seconds. A stale MCP
    server process holding the WAL lock would block the hook for the
    full 30s, causing a timeout kill. 200ms is enough for brief
    contention; if the lock is truly stuck, we skip the write (drift
    is advisory, not load-bearing).

    Raises sqlite3.Error or OSError on failure — callers already handle
    those (fail-open).
    """
    db_path = _drift_db_path(repo_id)
    key = str(db_path)
    conn = _DRIFT_CONN.get(key)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            try:
                conn.close()
            except Exception:
                pass
            _DRIFT_CONN.pop(key, None)
    conn = init_drift_db(db_path)
    conn.execute("PRAGMA busy_timeout=200")
    _DRIFT_CONN[key] = conn
    return conn


_CONFIDENCE_BAND_TO_FLOAT = {
    "high": 0.95,
    "medium": 0.7,
    "low": 0.3,
    None: 0.0,
}

_EDIT_OBS_HARD_CAP = 50_000
_EDIT_OBS_SOFT_CAP = 10_000


def _drift_db_path(repo_id: str) -> Path:
    return plugin_data_dir() / repo_id / "drift.db"


def record_edit_observation(
    repo_id: str,
    rel_path: str,
    archetype: str | None,
    confidence_band: str | None,
    *,
    matched_canonical: bool = False,
    observed_at: int | None = None,
) -> None:
    """Append one row to edit_observations + upsert files.

    Fail-open: any sqlite error is swallowed (drift logging is advisory,
    not load-bearing). Caller must already be in a hook context — repo_id
    is required.
    """
    if not repo_id:
        return
    confidence = _CONFIDENCE_BAND_TO_FLOAT.get(confidence_band, 0.0)
    ts = observed_at if observed_at is not None else int(time.time())

    try:
        conn = _get_drift_conn(repo_id)
    except (sqlite3.Error, OSError):
        return

    try:
        with conn:
            conn.execute(
                """
                INSERT INTO edit_observations
                  (rel_path, archetype, confidence_observed, matched_canonical, observed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (rel_path, archetype, confidence, 1 if matched_canonical else 0, ts),
            )
            conn.execute(
                """
                INSERT INTO files
                  (rel_path, mtime_ns, size, sha_hint, archetype, cached_sig,
                   last_observed_confidence, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET
                  archetype = excluded.archetype,
                  last_observed_confidence = excluded.last_observed_confidence,
                  last_seen_at = excluded.last_seen_at
                """,
                (rel_path, 0, None, None, archetype, None, confidence, ts),
            )
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM edit_observations"
            ).fetchone()
            if count > _EDIT_OBS_HARD_CAP:
                ninety_days_ago = ts - 90 * 24 * 3600
                conn.execute(
                    "DELETE FROM edit_observations WHERE observed_at < ?",
                    (ninety_days_ago,),
                )
                (count_after,) = conn.execute(
                    "SELECT COUNT(*) FROM edit_observations"
                ).fetchone()
                if count_after > _EDIT_OBS_SOFT_CAP:
                    conn.execute(
                        """
                        DELETE FROM edit_observations
                        WHERE id NOT IN (
                            SELECT id FROM edit_observations
                            ORDER BY observed_at DESC LIMIT ?
                        )
                        """,
                        (_EDIT_OBS_SOFT_CAP,),
                    )
    except sqlite3.Error:
        return


def record_bootstrap_baseline(
    repo_id: str,
    clustered_files: list[tuple[str, str | None, str | None]],
) -> int:
    """Populate the `files` table with one row per clustered file.

    BUG-NEW-021 (v0.5.7): pre-fix the `files` table only got rows from
    PreToolUse hook fires. Bootstrap on a 4 799-file repo left the table
    with 1 row (the file the model later touched). refresh_drift / drift
    detection has no baseline — every "is this file new" check sees a
    miss and the cache is effectively useless.

    Args:
        repo_id: the canonical repo id (hex digest).
        clustered_files: list of (rel_path, archetype, confidence_band) tuples,
            one per file the clusterer assigned (sparse-dropped files included
            with archetype=None).

    Returns:
        Number of rows written.

    Fail-open: any sqlite error is swallowed.
    """
    if not repo_id or not clustered_files:
        return 0
    ts = int(time.time())
    try:
        conn = _get_drift_conn(repo_id)
    except (sqlite3.Error, OSError):
        return 0
    written = 0
    try:
        with conn:
            for rel_path, archetype, band in clustered_files:
                confidence = _CONFIDENCE_BAND_TO_FLOAT.get(band, 0.0)
                try:
                    conn.execute(
                        """
                        INSERT INTO files
                          (rel_path, mtime_ns, size, sha_hint, archetype, cached_sig,
                           last_observed_confidence, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(rel_path) DO UPDATE SET
                          archetype = excluded.archetype,
                          last_observed_confidence = excluded.last_observed_confidence,
                          last_seen_at = excluded.last_seen_at
                        """,
                        (rel_path, 0, None, None, archetype, None, confidence, ts),
                    )
                    written += 1
                except sqlite3.Error:
                    continue
    except sqlite3.Error:
        return written
    return written


def compute_drift_score(repo_id: str, *, window_days: int = 14) -> float | None:
    """Compute observed_drift_score from recent edit_observations.

    Returns a 0.0–1.0 score where higher means more drift. Score is
    1 - mean(confidence_observed) over the trailing `window_days`. Returns
    None if no observations exist.
    """
    stats = compute_drift_stats(repo_id, window_days=window_days)
    if stats is None:
        return None
    return stats["score"]


def compute_drift_stats(
    repo_id: str,
    *,
    window_days: int = 14,
) -> dict | None:
    """Like ``compute_drift_score`` but returns ``{"score", "count"}``.

    Rec 4: SessionStart needs the observation count to apply a
    minimum-observations floor before surfacing the drift banner — a
    single low-confidence edit produces score=0.7, which would otherwise
    fire a banner on a repo that has barely been touched. Returns None
    when the drift.db is missing or no rows match the window.
    """
    db_path = _drift_db_path(repo_id)
    if not db_path.is_file():
        return None
    cutoff = int(time.time()) - window_days * 86_400
    try:
        conn = _get_drift_conn(repo_id)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = conn.execute(
            "SELECT AVG(confidence_observed), COUNT(*) FROM edit_observations "
            "WHERE observed_at >= ?",
            (cutoff,),
        ).fetchone()
    except sqlite3.Error:
        return None
    avg_conf, count = row
    if not count:
        return None
    score = max(0.0, min(1.0, 1.0 - float(avg_conf or 0.0)))
    return {"score": score, "count": int(count)}
