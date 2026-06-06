"""Unit tests for the drift.db decision_log table.

Exercises the real sqlite path (no mocks): records per-edit decisions, reads
the most-recent row back via ``latest_decision``, and pins the durability
invariant -- ``reset_drift_baseline`` (the refresh hook) wipes edit_observations
but leaves decision_log intact, because a postmortem must still be able to
reconstruct an escape after the gap that caused it was closed by a refresh.

Isolation mirrors test_drift_overrides.py: each test points CHAMELEON_PLUGIN_DATA
at a fresh tmp_path and clears the cached connection dict.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from chameleon_mcp.drift import observations as obs
from chameleon_mcp.drift.observations import (
    latest_decision,
    record_decision,
    record_edit_observation,
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


def _read_decision_rows(repo_id: str) -> list[tuple]:
    db_path = obs._drift_db_path(repo_id)
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT rel_path, archetype, match_quality, confidence_band, "
            "violations_raised, blockable_rules, outcome, session_id, observed_at "
            "FROM decision_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_record_decision_writes_one_row():
    record_decision(
        REPO_A,
        "src/a.ts",
        archetype="react-component",
        match_quality="ast",
        confidence_band="high",
        violations_raised=2,
        blockable_rules=["import-preference-violation", "jsx-presence-mismatch"],
        outcome="blocked",
        session_id="sess-1",
        observed_at=1000,
    )
    rows = _read_decision_rows(REPO_A)
    assert rows == [
        (
            "src/a.ts",
            "react-component",
            "ast",
            "high",
            2,
            # comma-joined and sorted
            "import-preference-violation,jsx-presence-mismatch",
            "blocked",
            "sess-1",
            1000,
        )
    ]


def test_blockable_rules_none_stored_as_null():
    record_decision(
        REPO_A,
        "src/a.ts",
        archetype="query",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        blockable_rules=None,
        outcome="clean",
        observed_at=10,
    )
    rows = _read_decision_rows(REPO_A)
    assert rows[0][5] is None  # blockable_rules column


def test_latest_decision_returns_most_recent_row():
    record_decision(
        REPO_A,
        "src/a.ts",
        archetype="react-component",
        match_quality="fallback",
        confidence_band="low",
        violations_raised=0,
        outcome="advised",
        observed_at=100,
    )
    record_decision(
        REPO_A,
        "src/a.ts",
        archetype="react-component",
        match_quality="ast",
        confidence_band="high",
        violations_raised=1,
        blockable_rules=["import-preference-violation"],
        outcome="would-block",
        observed_at=200,
    )
    latest = latest_decision(REPO_A, "src/a.ts")
    assert latest is not None
    assert latest["match_quality"] == "ast"
    assert latest["outcome"] == "would-block"
    assert latest["violations_raised"] == 1
    assert latest["blockable_rules"] == ["import-preference-violation"]
    assert latest["observed_at"] == 200


def test_latest_decision_none_for_unknown_file():
    record_decision(
        REPO_A,
        "src/a.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
    )
    assert latest_decision(REPO_A, "src/other.ts") is None


def test_latest_decision_none_when_no_db():
    assert latest_decision(REPO_B, "src/a.ts") is None


def test_missing_required_fields_is_noop():
    record_decision(
        "",
        "src/a.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
    )
    record_decision(
        REPO_A,
        "",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
    )
    record_decision(
        REPO_A,
        "src/a.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="",
    )
    assert latest_decision(REPO_A, "src/a.ts") is None


def test_refresh_baseline_does_not_wipe_decision_log():
    # The durability invariant: refresh resets the drift window
    # (edit_observations) but the per-edit decision history must survive, or a
    # postmortem cannot reconstruct an escape after the gap was closed.
    record_edit_observation(REPO_A, "src/a.ts", "react-component", "high")
    record_decision(
        REPO_A,
        "src/a.ts",
        archetype="react-component",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
    )

    deleted = reset_drift_baseline(REPO_A)
    assert deleted == 1  # the one edit observation

    # Decision log untouched.
    latest = latest_decision(REPO_A, "src/a.ts")
    assert latest is not None
    assert latest["outcome"] == "clean"


def test_record_decision_failopen_on_bad_conn(monkeypatch):
    # A connection failure must be swallowed (decision logging is advisory).
    def _boom(_repo_id):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(obs, "_get_drift_conn", _boom)
    record_decision(
        REPO_A,
        "src/a.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
    )  # must not raise


def test_hard_cap_trims_by_age_then_recency(monkeypatch):
    # Drive a tiny cap so the two-stage trim is observable without 100k inserts.
    monkeypatch.setenv("CHAMELEON_DECISION_LOG_HARD_CAP", "3")
    monkeypatch.setenv("CHAMELEON_DECISION_LOG_SOFT_CAP", "2")
    monkeypatch.setenv("CHAMELEON_DECISION_LOG_AGE_DAYS", "30")
    now = int(time.time())
    old = now - 40 * 86400  # past the age window
    for i in range(3):
        record_decision(
            REPO_A,
            f"old-{i}.ts",
            archetype="x",
            match_quality="ast",
            confidence_band="high",
            violations_raised=0,
            outcome="clean",
            observed_at=old + i,
        )
    # This insert pushes count to 4 > hard cap 3, triggering the trim: the three
    # age-window-expired rows are deleted first, leaving only the fresh row.
    record_decision(
        REPO_A,
        "fresh.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
        observed_at=now,
    )
    rows = _read_decision_rows(REPO_A)
    paths = {r[0] for r in rows}
    assert "fresh.ts" in paths
    assert not any(p.startswith("old-") for p in paths)


class TestLongLivedReaderDoesNotStarveHookWrites:
    """Regression: a reader-only connection must never hold the WAL writer lock.

    The MCP server opens its drift.db connection through ``_get_drift_conn``
    and may only ever read from it (explain_edit, get_shadow_report). If init
    leaves an uncommitted write pending, that connection pins the single WAL
    writer slot, and every hook-process write (200ms busy_timeout, fail-open)
    silently drops — the shadow report and decision log record nothing for as
    long as the server lives. Reproduced live during the 2026-06-06 gitlabhq
    QA campaign: 56 verified edits, zero decision rows.
    """

    def test_server_read_conn_holds_no_transaction(self):
        server_conn = obs._get_drift_conn(REPO_A)
        server_conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()
        assert server_conn.in_transaction is False

    def test_hook_write_lands_while_server_reader_conn_stays_open(self):
        # Simulate the long-lived server: open + read, keep the connection open.
        server_conn = obs._get_drift_conn(REPO_A)
        server_conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()

        # Simulate a hook in another process: force a fresh connection while
        # the server's stays open (drop it from the cache WITHOUT closing it).
        obs._DRIFT_CONN.clear()
        try:
            record_decision(
                REPO_A,
                "app/finders/users_finder.rb",
                archetype="class-finders",
                match_quality="ast",
                confidence_band="medium",
                violations_raised=1,
                outcome="advised",
            )
            rows = _read_decision_rows(REPO_A)
            assert len(rows) == 1, "hook write was starved by the long-lived reader connection"
        finally:
            try:
                server_conn.close()
            except sqlite3.Error:
                pass
