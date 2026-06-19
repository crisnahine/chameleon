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
            # A transaction left pending on the cached connection pins the
            # single WAL writer lock and starves every other process's write.
            # No caller holds a transaction across acquisitions (writes are
            # `with conn:` scoped), so anything pending here is a leak — drop
            # it before handing the connection out.
            if conn.in_transaction:
                conn.rollback()
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
            (count,) = conn.execute("SELECT COUNT(*) FROM edit_observations").fetchone()
            if count > _EDIT_OBS_HARD_CAP:
                ninety_days_ago = ts - 90 * 24 * 3600
                conn.execute(
                    "DELETE FROM edit_observations WHERE observed_at < ?",
                    (ninety_days_ago,),
                )
                (count_after,) = conn.execute("SELECT COUNT(*) FROM edit_observations").fetchone()
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


def record_override(
    repo_id: str,
    rule: str,
    *,
    rel_path: str | None = None,
    archetype: str | None = None,
    session_id: str | None = None,
    blanket: bool = False,
    observed_at: int | None = None,
) -> None:
    """Append one row to rule_overrides.

    An inline ``chameleon-ignore`` directive that drops a block-eligible rule is
    otherwise invisible after the turn. This records the bypass so the override
    rate is auditable per repo. ``blanket`` distinguishes a bare directive (no
    rule named, downgrades every block-eligible rule) from a targeted one.

    Fail-open: any sqlite error is swallowed (override logging is advisory, not
    load-bearing). ``repo_id`` and ``rule`` are required; a missing either is a
    no-op.
    """
    if not repo_id or not rule:
        return
    ts = observed_at if observed_at is not None else int(time.time())

    try:
        conn = _get_drift_conn(repo_id)
    except (sqlite3.Error, OSError):
        return

    try:
        with conn:
            conn.execute(
                """
                INSERT INTO rule_overrides
                  (rel_path, rule, archetype, session_id, blanket, observed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (rel_path, rule, archetype, session_id, 1 if blanket else 0, ts),
            )
            (count,) = conn.execute("SELECT COUNT(*) FROM rule_overrides").fetchone()
            if count > _EDIT_OBS_HARD_CAP:
                # Same two-stage trim as edit_observations: shed rows older than
                # the retention window first, then hard-cap by recency if the
                # repo overrides so heavily that age alone does not bound it.
                ninety_days_ago = ts - 90 * 24 * 3600
                conn.execute(
                    "DELETE FROM rule_overrides WHERE observed_at < ?",
                    (ninety_days_ago,),
                )
                (count_after,) = conn.execute("SELECT COUNT(*) FROM rule_overrides").fetchone()
                if count_after > _EDIT_OBS_SOFT_CAP:
                    conn.execute(
                        """
                        DELETE FROM rule_overrides
                        WHERE id NOT IN (
                            SELECT id FROM rule_overrides
                            ORDER BY observed_at DESC LIMIT ?
                        )
                        """,
                        (_EDIT_OBS_SOFT_CAP,),
                    )
    except sqlite3.Error:
        return


def record_decision(
    repo_id: str,
    rel_path: str,
    *,
    archetype: str | None,
    match_quality: str | None,
    confidence_band: str | None,
    violations_raised: int,
    blockable_rules: list[str] | None = None,
    outcome: str,
    session_id: str | None = None,
    observed_at: int | None = None,
    content_digest: str | None = None,
) -> None:
    """Append one row to decision_log: what chameleon knew and did for an edit.

    Written once per governed edit, after the outcome is resolved. A postmortem
    replays the most-recent row for a file to classify a miss as a coverage gap
    (``match_quality`` none/fallback) versus an in-scope miss (an ast/exact match
    that raised nothing). ``rel_path`` must be a true repo-relative path so the
    log keys consistently across clones; ``blockable_rules`` is the set of
    block-eligible rules that still stood on the file (stored comma-joined).
    ``content_digest`` pins the row to the exact content the verifier saw (the
    16-hex digest of the verified window); callers without it store NULL, which
    digest queries deliberately never match.

    Fail-open: any sqlite error is swallowed (decision logging is advisory, not
    load-bearing). ``repo_id``, ``rel_path``, and ``outcome`` are required; a
    missing one is a no-op.
    """
    if not repo_id or not rel_path or not outcome:
        return
    ts = observed_at if observed_at is not None else int(time.time())
    rules_joined = ",".join(sorted(r for r in (blockable_rules or []) if r)) or None

    try:
        conn = _get_drift_conn(repo_id)
    except (sqlite3.Error, OSError):
        return

    try:
        from chameleon_mcp._thresholds import threshold_int

        hard_cap = threshold_int("DECISION_LOG_HARD_CAP")
        soft_cap = threshold_int("DECISION_LOG_SOFT_CAP")
        age_days = threshold_int("DECISION_LOG_AGE_DAYS")
    except Exception:
        hard_cap, soft_cap, age_days = 100_000, 50_000, 180

    try:
        with conn:
            conn.execute(
                """
                INSERT INTO decision_log
                  (rel_path, archetype, match_quality, confidence_band, violations_raised,
                   blockable_rules, content_digest, outcome, session_id, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rel_path,
                    archetype,
                    match_quality,
                    confidence_band,
                    int(violations_raised),
                    rules_joined,
                    content_digest,
                    outcome,
                    session_id,
                    ts,
                ),
            )
            (count,) = conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()
            if count > hard_cap:
                # Same two-stage trim as edit_observations: shed rows older than
                # the retention window first, then hard-cap by recency if the
                # repo edits so heavily that age alone does not bound the table.
                cutoff = ts - age_days * 24 * 3600
                conn.execute("DELETE FROM decision_log WHERE observed_at < ?", (cutoff,))
                (count_after,) = conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()
                if count_after > soft_cap:
                    conn.execute(
                        """
                        DELETE FROM decision_log
                        WHERE id NOT IN (
                            SELECT id FROM decision_log
                            ORDER BY observed_at DESC LIMIT ?
                        )
                        """,
                        (soft_cap,),
                    )
    except sqlite3.Error:
        return


def latest_decision(repo_id: str, rel_path: str) -> dict | None:
    """Most-recent decision_log row for a file, or None when there is no record.

    The replay read behind the post-incident gap analysis: returns what
    chameleon last knew and did for ``rel_path`` (archetype, match quality,
    confidence band, violations raised, the block-eligible rules that stood, the
    resolved outcome, and when). ``rel_path`` must be the same repo-relative form
    the write used.

    Fail-open: a missing drift.db or any sqlite/OS error returns None.
    """
    if not repo_id or not rel_path:
        return None
    db_path = _drift_db_path(repo_id)
    if not db_path.is_file():
        return None
    try:
        conn = _get_drift_conn(repo_id)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = conn.execute(
            """
            SELECT rel_path, archetype, match_quality, confidence_band,
                   violations_raised, blockable_rules, outcome, session_id, observed_at
            FROM decision_log
            WHERE rel_path = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (rel_path,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    rules = [r for r in (row[5] or "").split(",") if r]
    return {
        "rel_path": row[0],
        "archetype": row[1],
        "match_quality": row[2],
        "confidence_band": row[3],
        "violations_raised": int(row[4] or 0),
        "blockable_rules": rules,
        "outcome": row[6],
        "session_id": row[7],
        "observed_at": int(row[8] or 0),
    }


_DECISION_SNAPSHOT_SELECT = (
    "SELECT id, rel_path, archetype, match_quality, confidence_band, violations_raised,"
    " blockable_rules, outcome, session_id, observed_at, content_digest FROM decision_log "
)


def decision_snapshot_for(
    repo_id: str,
    rel_path: str,
    content_digest: str,
    *,
    session_id: str | None = None,
) -> dict | None:
    """Newest decision_log row for exactly this (rel_path, content digest) pair.

    The (content digest, file) pair is the replay key: a snapshot reader pins
    the decision that governed the bytes it actually saw. Resolving by rel_path
    alone shows whatever edit came LAST, which is exactly what post-incident
    replay must never do. When no digest row matches and a ``session_id`` is
    given, falls back to the newest NULL-digest row for (rel_path, session_id)
    -- rows this same session wrote before the digest column existed -- and
    returns None otherwise. Legacy NULL-digest rows are never matched by the
    digest query itself, so old content cannot mis-join onto a new edit.

    Dict shape mirrors ``latest_decision`` plus ``id`` and ``content_digest``.
    Fail-open: any sqlite/OS error returns None.
    """
    if not repo_id or not rel_path:
        return None
    db_path = _drift_db_path(repo_id)
    if not db_path.is_file():
        return None
    try:
        conn = _get_drift_conn(repo_id)
    except (sqlite3.Error, OSError):
        return None
    try:
        row = None
        if content_digest:
            row = conn.execute(
                _DECISION_SNAPSHOT_SELECT
                + "WHERE rel_path = ? AND content_digest = ? "
                + "ORDER BY observed_at DESC, id DESC LIMIT 1",
                (rel_path, content_digest),
            ).fetchone()
        if row is None and session_id:
            row = conn.execute(
                _DECISION_SNAPSHOT_SELECT
                + "WHERE rel_path = ? AND session_id = ? AND content_digest IS NULL "
                + "ORDER BY observed_at DESC, id DESC LIMIT 1",
                (rel_path, session_id),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    rules = [r for r in (row[6] or "").split(",") if r]
    return {
        "id": int(row[0]),
        "rel_path": row[1],
        "archetype": row[2],
        "match_quality": row[3],
        "confidence_band": row[4],
        "violations_raised": int(row[5] or 0),
        "blockable_rules": rules,
        "outcome": row[7],
        "session_id": row[8],
        "observed_at": int(row[9] or 0),
        "content_digest": row[10],
    }


def session_override_rows(repo_id: str, session_id: str, *, limit: int) -> list[dict]:
    """Per-(rule, file, blanket) override tallies for one session, newest first.

    Powers the attestation's override evidence: every inline chameleon-ignore
    bypass recorded for this session shows up grouped with a count, capped at
    ``limit``. Fail-open: any sqlite/OS error (or a missing db) returns [].
    """
    if not repo_id or not session_id:
        return []
    db_path = _drift_db_path(repo_id)
    if not db_path.is_file():
        return []
    try:
        conn = _get_drift_conn(repo_id)
        rows = conn.execute(
            """
            SELECT rule, rel_path, blanket, COUNT(*)
            FROM rule_overrides
            WHERE session_id = ?
            GROUP BY rule, rel_path, blanket
            ORDER BY MAX(observed_at) DESC
            LIMIT ?
            """,
            (session_id, int(limit)),
        ).fetchall()
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return []
    return [
        {"rule": str(r[0]), "file": r[1], "blanket": bool(r[2]), "count": int(r[3] or 0)}
        for r in rows
    ]


def session_override_group_count(repo_id: str, session_id: str) -> int:
    """Total distinct (rule, file, blanket) override groups for one session.

    Lets the attestation report exactly how many groups its embed cap dropped,
    instead of a saturated "more existed" flag. Fail-open: 0 on any error.
    """
    if not repo_id or not session_id:
        return 0
    db_path = _drift_db_path(repo_id)
    if not db_path.is_file():
        return 0
    try:
        conn = _get_drift_conn(repo_id)
        (count,) = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT 1 FROM rule_overrides
                WHERE session_id = ?
                GROUP BY rule, rel_path, blanket
            )
            """,
            (session_id,),
        ).fetchone()
        return int(count or 0)
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return 0


def override_counts(
    repo_id: str,
    *,
    window_days: int = 21,
) -> dict[str, dict] | None:
    """Per-rule override tallies from rule_overrides within the trailing window.

    Returns ``{rule: {"overrides", "blanket", "distinct_files",
    "distinct_sessions"}}`` or None when the drift.db is missing or no rows
    match. ``blanket`` counts how many of the overrides came from a bare
    directive (no rule named): a bare directive is recorded once per
    block-eligible rule it downgraded, and a high bare share signals someone
    stamping past the gate wholesale rather than annotating a specific
    intentional deviation.

    Fail-open: any sqlite/OS error returns None.
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
        rows = conn.execute(
            """
            SELECT rule,
                   COUNT(*),
                   SUM(blanket),
                   COUNT(DISTINCT rel_path),
                   COUNT(DISTINCT session_id)
            FROM rule_overrides
            WHERE observed_at >= ?
            GROUP BY rule
            """,
            (cutoff,),
        ).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    out: dict[str, dict] = {}
    for rule, total, blanket, files, sessions in rows:
        out[str(rule)] = {
            "overrides": int(total or 0),
            "blanket": int(blanket or 0),
            "distinct_files": int(files or 0),
            "distinct_sessions": int(sessions or 0),
        }
    return out


def archetype_antipattern_signals(
    repo_id: str,
    *,
    window_days: int = 30,
    min_count: int = 3,
    max_rules_per_archetype: int = 5,
) -> dict[str, dict]:
    """Per-archetype recurring-violation signals from drift history.

    For each archetype, surfaces the rules edits there repeatedly bumped against
    (``rule_overrides``, the per-(archetype, rule) signal) plus the archetype's
    off-pattern edit count (``decision_log`` rows with ``violations_raised > 0``).
    It points a deriver at where a drift-derived counterexample is worth capturing;
    it carries NO wrong-way code, since drift.db stores none -- only the rule and
    frequency. An archetype is included only when its top rule reaches ``min_count``
    overrides OR its violation-edit count reaches ``min_count``, so a one-off bump
    never surfaces.

    Returns ``{archetype: {"rules": [{"rule","count","distinct_files"}],
    "violation_edits": int, "total_edits": int}}``. Fail-open: a missing db or any
    sqlite/OS error returns ``{}``.
    """
    db_path = _drift_db_path(repo_id)
    if not repo_id or not db_path.is_file():
        return {}
    cutoff = int(time.time()) - max(1, window_days) * 86_400
    try:
        conn = _get_drift_conn(repo_id)
    except (sqlite3.Error, OSError):
        return {}
    try:
        override_rows = conn.execute(
            """
            SELECT archetype, rule, COUNT(*), COUNT(DISTINCT rel_path)
            FROM rule_overrides
            WHERE observed_at >= ? AND archetype IS NOT NULL AND archetype != ''
            GROUP BY archetype, rule
            """,
            (cutoff,),
        ).fetchall()
        decision_rows = conn.execute(
            """
            SELECT archetype,
                   SUM(CASE WHEN violations_raised > 0 THEN 1 ELSE 0 END),
                   COUNT(*)
            FROM decision_log
            WHERE observed_at >= ? AND archetype IS NOT NULL AND archetype != ''
            GROUP BY archetype
            """,
            (cutoff,),
        ).fetchall()
    except sqlite3.Error:
        return {}

    def _blank() -> dict:
        return {"rules": [], "violation_edits": 0, "total_edits": 0}

    per_arch: dict[str, dict] = {}
    for archetype, rule, count, files in override_rows:
        entry = per_arch.setdefault(str(archetype), _blank())
        entry["rules"].append(
            {"rule": str(rule), "count": int(count or 0), "distinct_files": int(files or 0)}
        )
    for archetype, viol, total in decision_rows:
        entry = per_arch.setdefault(str(archetype), _blank())
        entry["violation_edits"] = int(viol or 0)
        entry["total_edits"] = int(total or 0)

    out: dict[str, dict] = {}
    for arch, entry in per_arch.items():
        entry["rules"].sort(key=lambda r: (-r["count"], r["rule"]))
        entry["rules"] = entry["rules"][: max(0, max_rules_per_archetype)]
        top = entry["rules"][0]["count"] if entry["rules"] else 0
        if top >= min_count or entry["violation_edits"] >= min_count:
            out[arch] = entry
    return out


def record_bootstrap_baseline(
    repo_id: str,
    clustered_files: list[tuple[str, str | None, str | None]],
) -> int:
    """No-op retained for call-site compatibility.

    This previously populated the `files` table, which had no readers anywhere
    (drift detection runs entirely off ``edit_observations``). The table and
    its writers were removed; this stub keeps the signature so the orchestrator
    caller is unchanged. Returns 0.
    """
    del repo_id, clustered_files
    return 0


def reset_drift_baseline(repo_id: str) -> int:
    """Clear all edit observations for a repo, re-baselining the drift window.

    The drift score is ``1 - mean(confidence_observed)`` over edits made since
    the profile was last derived. Once the profile is re-derived (a refresh /
    re-bootstrap), those observations were scored against the superseded
    profile, so the signal must reset -- otherwise ``get_drift_status`` keeps
    recommending ``/chameleon-refresh`` after a refresh already ran. Returns the
    number of rows deleted. Fail-open: any sqlite/OS error returns 0.
    """
    if not repo_id:
        return 0
    db_path = _drift_db_path(repo_id)
    if not db_path.is_file():
        return 0
    try:
        conn = _get_drift_conn(repo_id)
    except (sqlite3.Error, OSError):
        return 0
    try:
        with conn:
            (before,) = conn.execute("SELECT COUNT(*) FROM edit_observations").fetchone()
            conn.execute("DELETE FROM edit_observations")
        return int(before or 0)
    except sqlite3.Error:
        return 0


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
