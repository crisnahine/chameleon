"""Unit tests for the drift.db rule_overrides table.

Exercises the real sqlite path (no mocks): records inline-override events,
reads them back via ``override_counts``, asserts the bare-vs-targeted
``blanket`` split, and pins the key durability invariant -- that
``reset_drift_baseline`` (the refresh hook) wipes edit_observations but leaves
rule_overrides intact, because the override record is durable per-repo history
that spans profile revisions.

Isolation mirrors test_drift_observations.py: each test points
CHAMELEON_PLUGIN_DATA at a fresh tmp_path and clears the cached connection dict.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from chameleon_mcp.drift import observations as obs
from chameleon_mcp.drift.observations import (
    override_counts,
    record_edit_observation,
    record_override,
    reset_drift_baseline,
)

REPO_A = "a" * 64
REPO_B = "b" * 64


def _close_drift_conns() -> None:
    for conn in list(obs._DRIFT_CONN.values()):
        try:
            conn.close()
        except Exception:
            pass
    obs._DRIFT_CONN.clear()


@pytest.fixture(autouse=True)
def _isolate_drift(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    _close_drift_conns()
    yield
    _close_drift_conns()


def _read_override_rows(repo_id: str) -> list[tuple]:
    db_path = obs._drift_db_path(repo_id)
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT rel_path, rule, archetype, session_id, blanket, observed_at "
            "FROM rule_overrides ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_record_override_writes_one_row():
    record_override(
        REPO_A,
        "import-preference-violation",
        rel_path="src/a.ts",
        archetype="react-component",
        session_id="sess-1",
        observed_at=1000,
    )
    rows = _read_override_rows(REPO_A)
    assert rows == [
        ("src/a.ts", "import-preference-violation", "react-component", "sess-1", 0, 1000)
    ]


def test_blanket_flag_recorded():
    record_override(REPO_A, "jsx-presence-mismatch", blanket=True, observed_at=2000)
    rows = _read_override_rows(REPO_A)
    assert rows[0][4] == 1  # blanket column


def test_missing_repo_or_rule_is_noop():
    record_override("", "import-preference-violation")
    record_override(REPO_A, "")
    # No db should have been touched for the missing-rule case beyond schema
    # init; override_counts returns None when there are no rows.
    assert override_counts(REPO_A) is None


def test_override_counts_groups_by_rule_and_splits_blanket():
    record_override(REPO_A, "import-preference-violation", rel_path="a.ts", session_id="s1")
    record_override(REPO_A, "import-preference-violation", rel_path="b.ts", session_id="s2")
    record_override(REPO_A, "import-preference-violation", rel_path="c.ts", blanket=True)
    record_override(REPO_A, "jsx-presence-mismatch", rel_path="d.tsx")

    counts = override_counts(REPO_A)
    assert counts is not None
    imp = counts["import-preference-violation"]
    assert imp["overrides"] == 3
    assert imp["blanket"] == 1
    assert imp["distinct_files"] == 3
    assert imp["distinct_sessions"] == 2  # s1, s2 (the blanket row had no session)
    assert counts["jsx-presence-mismatch"]["overrides"] == 1


def test_override_counts_window_excludes_old_rows():
    now = int(time.time())
    record_override(REPO_A, "import-preference-violation", observed_at=now)
    record_override(REPO_A, "import-preference-violation", observed_at=now - 40 * 86400)
    counts = override_counts(REPO_A, window_days=21)
    assert counts is not None
    assert counts["import-preference-violation"]["overrides"] == 1


def test_override_counts_none_when_no_db():
    assert override_counts(REPO_B) is None


def test_refresh_baseline_does_not_wipe_overrides():
    # The durability invariant: refresh resets the drift window
    # (edit_observations) but the override audit history must survive.
    record_edit_observation(REPO_A, "src/a.ts", "react-component", "high")
    record_override(REPO_A, "import-preference-violation", rel_path="src/a.ts")

    deleted = reset_drift_baseline(REPO_A)
    assert deleted == 1  # the one edit observation

    # Overrides untouched.
    counts = override_counts(REPO_A)
    assert counts is not None
    assert counts["import-preference-violation"]["overrides"] == 1


def test_record_override_failopen_on_bad_conn(monkeypatch):
    # A connection failure must be swallowed (override logging is advisory).
    def _boom(_repo_id):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(obs, "_get_drift_conn", _boom)
    record_override(REPO_A, "import-preference-violation")  # must not raise
