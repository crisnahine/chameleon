"""Per-edit drift observation recording.

Each PreToolUse Edit/Write/NotebookEdit invocation produces an observation:
"file <rel_path> matched archetype <X> with confidence <C> at <ts>". These
accumulate in drift.db's `edit_observations` table and power
`get_drift_status` — when too many recent edits land on archetypes with low
confidence, the repo's profile has drifted and `/chameleon-refresh` is
recommended.

Per ARCHITECTURE.md "drift.db" subsection.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from chameleon_mcp.drift.schema import init_drift_db
from chameleon_mcp.profile.trust import plugin_data_dir


_CONFIDENCE_BAND_TO_FLOAT = {
    "high": 0.95,
    "medium": 0.7,
    "low": 0.3,
    None: 0.0,
}


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
    db_path = _drift_db_path(repo_id)
    confidence = _CONFIDENCE_BAND_TO_FLOAT.get(confidence_band, 0.0)
    ts = observed_at if observed_at is not None else int(time.time())

    try:
        conn = init_drift_db(db_path)
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
    except sqlite3.Error:
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass


def compute_drift_score(repo_id: str, *, window_days: int = 14) -> float | None:
    """Compute observed_drift_score from recent edit_observations.

    Returns a 0.0–1.0 score where higher means more drift. Score is
    1 - mean(confidence_observed) over the trailing `window_days`. Returns
    None if no observations exist.
    """
    db_path = _drift_db_path(repo_id)
    if not db_path.is_file():
        return None
    cutoff = int(time.time()) - window_days * 86_400
    try:
        conn = init_drift_db(db_path)
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
    finally:
        try:
            conn.close()
        except Exception:
            pass
    avg_conf, count = row
    if not count:
        return None
    return max(0.0, min(1.0, 1.0 - float(avg_conf or 0.0)))
