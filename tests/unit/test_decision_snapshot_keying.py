"""decision_log content-digest column: migration + the (file, digest) replay key.

The decision snapshot embedded in a session attestation must resolve to the row
that governed the exact bytes the attestation saw, never to whatever edit came
last. That requires a content_digest column on decision_log (migrated in place;
the table is durable postmortem history and is never dropped), a digest-keyed
lookup, and the rule that NULL-digest legacy rows are only reachable through
the explicit same-session fallback.

Isolation mirrors test_decision_log.py: CHAMELEON_PLUGIN_DATA points at a fresh
tmp_path and the cached connection dict is cleared around each test.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chameleon_mcp.drift import observations as obs
from chameleon_mcp.drift.observations import (
    decision_snapshot_for,
    record_decision,
    session_override_rows,
)
from chameleon_mcp.drift.schema import init_drift_db

REPO = "d" * 64


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


# The decision_log/rule_overrides DDL as it stood before the content_digest
# column existed, for building a pre-migration database.
_LEGACY_DDL = """
CREATE TABLE decision_log (
  id INTEGER PRIMARY KEY,
  rel_path TEXT NOT NULL,
  archetype TEXT,
  match_quality TEXT,
  confidence_band TEXT,
  violations_raised INTEGER NOT NULL DEFAULT 0,
  blockable_rules TEXT,
  outcome TEXT NOT NULL,
  session_id TEXT,
  observed_at INTEGER NOT NULL
);
CREATE TABLE rule_overrides (
  id INTEGER PRIMARY KEY,
  rel_path TEXT,
  rule TEXT NOT NULL,
  archetype TEXT,
  session_id TEXT,
  blanket INTEGER NOT NULL DEFAULT 0,
  observed_at INTEGER NOT NULL
);
"""


def _make_legacy_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_LEGACY_DDL)
        conn.execute(
            "INSERT INTO decision_log (rel_path, archetype, match_quality, confidence_band,"
            " violations_raised, blockable_rules, outcome, session_id, observed_at)"
            " VALUES ('src/a.ts', 'x', 'ast', 'high', 0, NULL, 'clean', 'legacy-s', 100)"
        )
        conn.commit()
    finally:
        conn.close()


def _db_path(repo_id: str) -> Path:
    return obs._drift_db_path(repo_id)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_digest_column_and_indexes():
    db = _db_path(REPO)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = init_drift_db(db)
    try:
        assert "content_digest" in _columns(conn, "decision_log")
        dl_indexes = {row[1] for row in conn.execute("PRAGMA index_list(decision_log)")}
        assert "idx_decision_log_digest" in dl_indexes
        ov_indexes = {row[1] for row in conn.execute("PRAGMA index_list(rule_overrides)")}
        assert "idx_rule_overrides_session" in ov_indexes
    finally:
        conn.close()


def test_legacy_db_migrated_in_place_rows_preserved():
    db = _db_path(REPO)
    _make_legacy_db(db)
    conn = init_drift_db(db)
    try:
        assert "content_digest" in _columns(conn, "decision_log")
        rows = conn.execute("SELECT rel_path, outcome, content_digest FROM decision_log").fetchall()
        # The pre-migration row survived the ALTER with a NULL digest.
        assert [tuple(r) for r in rows] == [("src/a.ts", "clean", None)]
    finally:
        conn.close()


def test_migration_idempotent_and_tolerates_duplicate_column_race():
    db = _db_path(REPO)
    _make_legacy_db(db)
    # Simulate a concurrent process winning the ALTER race.
    pre = sqlite3.connect(str(db))
    pre.execute("ALTER TABLE decision_log ADD COLUMN content_digest TEXT")
    pre.commit()
    pre.close()

    conn = init_drift_db(db)
    conn.close()
    conn = init_drift_db(db)  # repeated init: still fine
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(decision_log)")]
        assert cols.count("content_digest") == 1
    finally:
        conn.close()


def test_record_decision_digest_round_trip_and_null_default():
    record_decision(
        REPO,
        "src/a.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
        session_id="s1",
        observed_at=100,
        content_digest="abcd1234abcd1234",
    )
    record_decision(
        REPO,
        "src/b.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
        session_id="s1",
        observed_at=101,
    )
    conn = sqlite3.connect(str(_db_path(REPO)))
    try:
        rows = dict(conn.execute("SELECT rel_path, content_digest FROM decision_log").fetchall())
    finally:
        conn.close()
    assert rows["src/a.ts"] == "abcd1234abcd1234"
    assert rows["src/b.ts"] is None


def test_snapshot_resolves_by_digest_never_the_later_edit():
    record_decision(
        REPO,
        "src/a.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=1,
        outcome="advised",
        session_id="s1",
        observed_at=100,
        content_digest="digest-one-00000",
    )
    record_decision(
        REPO,
        "src/a.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
        session_id="s1",
        observed_at=200,
        content_digest="digest-two-00000",
    )
    first = decision_snapshot_for(REPO, "src/a.ts", "digest-one-00000")
    second = decision_snapshot_for(REPO, "src/a.ts", "digest-two-00000")
    assert first is not None and second is not None
    # The FIRST digest resolves to the first row's id, never the later edit.
    assert first["id"] < second["id"]
    assert first["outcome"] == "advised"
    assert first["content_digest"] == "digest-one-00000"
    assert second["outcome"] == "clean"


def test_null_digest_rows_never_match_digest_query_session_fallback_applies():
    # Legacy row: NULL digest (record_decision without the kwarg).
    record_decision(
        REPO,
        "src/a.ts",
        archetype="x",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
        session_id="s-old",
        observed_at=100,
    )
    # A digest query must not mis-join the legacy row to new content...
    assert decision_snapshot_for(REPO, "src/a.ts", "fresh-digest-0000") is None
    # ...but the same-session fallback recovers it.
    snap = decision_snapshot_for(REPO, "src/a.ts", "fresh-digest-0000", session_id="s-old")
    assert snap is not None
    assert snap["session_id"] == "s-old"
    assert snap["content_digest"] is None
    # Neither a digest row nor a same-session NULL row: None.
    assert decision_snapshot_for(REPO, "src/a.ts", "fresh-digest-0000", session_id="s-new") is None
    assert decision_snapshot_for(REPO, "src/other.ts", "fresh-digest-0000") is None


def test_session_override_rows_groups_and_limits():
    from chameleon_mcp.drift.observations import record_override

    for _ in range(3):
        record_override(REPO, "eval-call", rel_path="a.rb", session_id="s1", observed_at=100)
    record_override(
        REPO, "eval-call", rel_path="b.rb", session_id="s1", blanket=True, observed_at=200
    )
    record_override(
        REPO, "secret-detected-in-content", rel_path="c.rb", session_id="s1", observed_at=300
    )
    record_override(REPO, "eval-call", rel_path="z.rb", session_id="other", observed_at=400)

    rows = session_override_rows(REPO, "s1", limit=10)
    assert {(r["rule"], r["file"], r["blanket"], r["count"]) for r in rows} == {
        ("eval-call", "a.rb", False, 3),
        ("eval-call", "b.rb", True, 1),
        ("secret-detected-in-content", "c.rb", False, 1),
    }
    # Newest-first ordering and the limit cap.
    limited = session_override_rows(REPO, "s1", limit=1)
    assert len(limited) == 1
    assert limited[0]["rule"] == "secret-detected-in-content"


def test_session_override_rows_failopen_missing_db():
    assert session_override_rows("e" * 64, "s1", limit=10) == []
    assert session_override_rows("", "s1", limit=10) == []
