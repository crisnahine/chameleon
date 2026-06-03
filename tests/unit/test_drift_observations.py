"""Unit tests for chameleon_mcp.drift.observations.

Exercises the real sqlite drift.db path (no mocks): records edit observations,
reads them back, and asserts the aggregation in ``compute_drift_stats`` /
``compute_drift_score``. Also drives ``get_drift_status`` (in tools.py) with
real observation data plus a trust record present, which existing tests do not
cover (they only hit the empty-db case via the QA batteries). Covers the
edit-observation hard/soft cap pruning by lowering the module caps.

Isolation: no conftest.py exists. Each test points CHAMELEON_PLUGIN_DATA at a
fresh tmp_path and clears the module's cached sqlite connection dict
(``_DRIFT_CONN``) before and after, mirroring the inline-isolation pattern used
by test_index_db.py.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from chameleon_mcp.drift import observations as obs
from chameleon_mcp.drift.observations import (
    compute_drift_score,
    compute_drift_stats,
    record_bootstrap_baseline,
    record_edit_observation,
)


def _close_drift_conns() -> None:
    """Close and drop every cached drift.db connection."""
    for conn in list(obs._DRIFT_CONN.values()):
        try:
            conn.close()
        except Exception:
            pass
    obs._DRIFT_CONN.clear()


@pytest.fixture(autouse=True)
def _isolate_drift(tmp_path: Path, monkeypatch):
    """Point CHAMELEON_PLUGIN_DATA at tmp_path and reset the conn cache."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    _close_drift_conns()
    yield
    _close_drift_conns()


def _read_rows(repo_id: str) -> list[tuple]:
    """Read all edit_observations rows via an independent connection."""
    db_path = obs._drift_db_path(repo_id)
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT rel_path, archetype, confidence_observed, matched_canonical, observed_at "
            "FROM edit_observations ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# 64-char lowercase hex repo_ids (the shape get_drift_status expects).
REPO_A = "a" * 64
REPO_B = "b" * 64


class TestRecordAndReadBack:
    def test_single_observation_round_trips_all_columns(self, tmp_path: Path):
        ts = 1_700_000_000
        record_edit_observation(
            REPO_A,
            "src/foo.ts",
            "component",
            "high",
            matched_canonical=True,
            observed_at=ts,
        )
        rows = _read_rows(REPO_A)
        assert rows == [("src/foo.ts", "component", 0.95, 1, ts)]

    def test_db_file_created_under_repo_id_dir(self, tmp_path: Path):
        record_edit_observation(REPO_A, "x.ts", "comp", "high", observed_at=1)
        db_path = obs._drift_db_path(REPO_A)
        assert db_path == tmp_path / REPO_A / "drift.db"
        assert db_path.is_file()

    def test_confidence_band_mapping(self, tmp_path: Path):
        ts = 1_700_000_000
        record_edit_observation(REPO_A, "high.ts", "c", "high", observed_at=ts)
        record_edit_observation(REPO_A, "medium.ts", "c", "medium", observed_at=ts)
        record_edit_observation(REPO_A, "low.ts", "c", "low", observed_at=ts)
        record_edit_observation(REPO_A, "none.ts", "c", None, observed_at=ts)
        record_edit_observation(REPO_A, "bogus.ts", "c", "not-a-band", observed_at=ts)

        by_path = {r[0]: r[2] for r in _read_rows(REPO_A)}
        assert by_path["high.ts"] == 0.95
        assert by_path["medium.ts"] == 0.7
        assert by_path["low.ts"] == 0.3
        # None and any unrecognized band both fall to the 0.0 default.
        assert by_path["none.ts"] == 0.0
        assert by_path["bogus.ts"] == 0.0

    def test_matched_canonical_stored_as_zero_or_one(self, tmp_path: Path):
        record_edit_observation(REPO_A, "t.ts", "c", "high", matched_canonical=True, observed_at=1)
        record_edit_observation(REPO_A, "f.ts", "c", "high", matched_canonical=False, observed_at=1)
        by_path = {r[0]: r[3] for r in _read_rows(REPO_A)}
        assert by_path["t.ts"] == 1
        assert by_path["f.ts"] == 0

    def test_archetype_none_persists_as_null(self, tmp_path: Path):
        record_edit_observation(REPO_A, "u.ts", None, "high", observed_at=1)
        rows = _read_rows(REPO_A)
        assert rows[0][1] is None

    def test_default_observed_at_is_current_time(self, tmp_path: Path):
        before = int(time.time())
        record_edit_observation(REPO_A, "now.ts", "c", "high")
        after = int(time.time())
        rows = _read_rows(REPO_A)
        assert before <= rows[0][4] <= after

    def test_empty_repo_id_is_noop(self, tmp_path: Path):
        record_edit_observation("", "x.ts", "c", "high", observed_at=1)
        # No repo dir, no db written.
        assert not obs._drift_db_path("").is_file()
        assert not (tmp_path / "drift.db").exists()

    def test_appends_rather_than_overwrites(self, tmp_path: Path):
        for i in range(4):
            record_edit_observation(REPO_A, f"f{i}.ts", "c", "high", observed_at=1 + i)
        assert len(_read_rows(REPO_A)) == 4


class TestRecordBootstrapBaseline:
    def test_is_noop_returns_zero(self, tmp_path: Path):
        result = record_bootstrap_baseline(REPO_A, [("a.ts", "comp", "high")])
        assert result == 0
        # The stub must not create a drift.db or any rows.
        assert not obs._drift_db_path(REPO_A).is_file()


class TestComputeDriftStats:
    def test_score_is_one_minus_mean_confidence(self, tmp_path: Path):
        ts = int(time.time())
        # mean of 0.95, 0.95, 0.3 = 0.7333... -> score 0.2666...
        record_edit_observation(REPO_A, "a.ts", "c", "high", observed_at=ts)
        record_edit_observation(REPO_A, "b.ts", "c", "high", observed_at=ts)
        record_edit_observation(REPO_A, "c.ts", "c", "low", observed_at=ts)

        stats = compute_drift_stats(REPO_A)
        assert stats is not None
        assert stats["count"] == 3
        expected = 1.0 - (0.95 + 0.95 + 0.3) / 3.0
        assert stats["score"] == pytest.approx(expected)
        # compute_drift_score is the same number.
        assert compute_drift_score(REPO_A) == pytest.approx(expected)

    def test_all_high_confidence_gives_low_score(self, tmp_path: Path):
        ts = int(time.time())
        for i in range(5):
            record_edit_observation(REPO_A, f"f{i}.ts", "c", "high", observed_at=ts)
        # 1 - 0.95 = 0.05.
        assert compute_drift_score(REPO_A) == pytest.approx(0.05)

    def test_all_low_confidence_gives_high_score(self, tmp_path: Path):
        ts = int(time.time())
        for i in range(5):
            record_edit_observation(REPO_A, f"f{i}.ts", "c", "low", observed_at=ts)
        # 1 - 0.3 = 0.7.
        assert compute_drift_score(REPO_A) == pytest.approx(0.7)

    def test_missing_db_returns_none(self, tmp_path: Path):
        # Never recorded anything for this repo -> no drift.db on disk.
        assert compute_drift_stats(REPO_B) is None
        assert compute_drift_score(REPO_B) is None

    def test_observations_outside_window_excluded(self, tmp_path: Path):
        # Only an observation 20 days old; default window is 14 days.
        old_ts = int(time.time()) - 20 * 86_400
        record_edit_observation(REPO_A, "old.ts", "c", "low", observed_at=old_ts)
        # db exists but nothing matches the trailing window.
        assert obs._drift_db_path(REPO_A).is_file()
        assert compute_drift_stats(REPO_A) is None
        assert compute_drift_score(REPO_A) is None

    def test_window_days_argument_widens_inclusion(self, tmp_path: Path):
        old_ts = int(time.time()) - 20 * 86_400
        record_edit_observation(REPO_A, "old.ts", "c", "low", observed_at=old_ts)
        # With a 30-day window the 20-day-old row is now in range.
        stats = compute_drift_stats(REPO_A, window_days=30)
        assert stats is not None
        assert stats["count"] == 1
        assert stats["score"] == pytest.approx(0.7)

    def test_score_clamped_to_unit_interval(self, tmp_path: Path):
        # A None band -> confidence 0.0 -> score 1.0 (upper clamp boundary).
        ts = int(time.time())
        record_edit_observation(REPO_A, "z.ts", "c", None, observed_at=ts)
        score = compute_drift_score(REPO_A)
        assert score == 1.0


def _write_trust_record(repo_id: str, *, granted_at: str) -> None:
    """Write a minimal .trust record under the repo's plugin-data dir."""
    from chameleon_mcp.profile.trust import repo_data_dir

    rd = repo_data_dir(repo_id)
    record = {
        "granted_at": granted_at,
        "granted_by_user": "tester",
        "profile_sha256": "deadbeef",
        "repo_root": "/some/repo/root",
    }
    (rd / ".trust").write_text(json.dumps(record), encoding="utf-8")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_days_ago(days: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86_400))


class TestGetDriftStatusWithData:
    """get_drift_status with real observations + a trust record present."""

    def test_high_drift_recommends_refresh(self, tmp_path: Path):
        from chameleon_mcp.tools import get_drift_status

        _write_trust_record(REPO_A, granted_at=_now_iso())
        ts = int(time.time())
        for i in range(5):
            record_edit_observation(REPO_A, f"f{i}.ts", "svc", "low", observed_at=ts)

        env = get_drift_status(REPO_A)
        assert env["api_version"] == "1"
        data = env["data"]
        assert data["repo_id"] == REPO_A
        assert data["days_since_refresh"] == 0
        # score 0.7 > 0.5 threshold.
        assert data["observed_drift_score"] == pytest.approx(0.7)
        assert data["recommended_action"] == "observed drift is high (0.70); run /chameleon-refresh"

    def test_fresh_repo_low_drift(self, tmp_path: Path):
        from chameleon_mcp.tools import get_drift_status

        _write_trust_record(REPO_A, granted_at=_now_iso())
        ts = int(time.time())
        for i in range(5):
            record_edit_observation(REPO_A, f"f{i}.ts", "svc", "high", observed_at=ts)

        data = get_drift_status(REPO_A)["data"]
        assert data["days_since_refresh"] == 0
        assert data["observed_drift_score"] == pytest.approx(0.05)
        # Below threshold and recently granted -> fresh.
        assert data["recommended_action"] == "fresh"

    def test_stale_grant_no_observations(self, tmp_path: Path):
        from chameleon_mcp.tools import get_drift_status

        _write_trust_record(REPO_A, granted_at=_iso_days_ago(100))
        # No observations recorded -> drift score is None.
        data = get_drift_status(REPO_A)["data"]
        assert data["days_since_refresh"] == 100
        assert data["observed_drift_score"] is None
        assert data["recommended_action"] == "profile may be stale; run /chameleon-refresh"

    def test_grant_between_30_and_90_days_suggests_consideration(self, tmp_path: Path):
        from chameleon_mcp.tools import get_drift_status

        _write_trust_record(REPO_A, granted_at=_iso_days_ago(45))
        ts = int(time.time())
        record_edit_observation(REPO_A, "f.ts", "svc", "high", observed_at=ts)

        data = get_drift_status(REPO_A)["data"]
        assert data["days_since_refresh"] == 45
        assert data["recommended_action"] == (
            "consider /chameleon-refresh if codebase has materially changed"
        )

    def test_high_drift_beats_stale_grant(self, tmp_path: Path):
        """drift>0.5 takes priority over the days-since-refresh thresholds."""
        from chameleon_mcp.tools import get_drift_status

        _write_trust_record(REPO_A, granted_at=_iso_days_ago(100))
        ts = int(time.time())
        for i in range(5):
            record_edit_observation(REPO_A, f"f{i}.ts", "svc", "low", observed_at=ts)

        data = get_drift_status(REPO_A)["data"]
        assert data["observed_drift_score"] == pytest.approx(0.7)
        # Drift branch wins even though days_since_refresh > 90.
        assert "observed drift is high" in data["recommended_action"]

    def test_no_trust_grant_message(self, tmp_path: Path):
        from chameleon_mcp.tools import get_drift_status

        # Record observations but write NO trust record.
        ts = int(time.time())
        record_edit_observation(REPO_A, "f.ts", "svc", "low", observed_at=ts)

        data = get_drift_status(REPO_A)["data"]
        assert data["days_since_refresh"] is None
        assert data["recommended_action"] == "no trust grant found; run /chameleon-trust first"

    def test_empty_repo_arg_errors(self, tmp_path: Path):
        from chameleon_mcp.tools import get_drift_status

        env = get_drift_status("")
        assert env["data"]["status"] == "failed"
        assert "expected repo path or repo_id" in env["data"]["error"]


class TestHardCapPruning:
    """The edit-observation hard cap (>50k) triggers a tiered prune."""

    def test_hard_cap_deletes_rows_older_than_90_days(self, tmp_path: Path, monkeypatch):
        # Lower caps so we don't have to insert 50k rows.
        monkeypatch.setattr(obs, "_EDIT_OBS_HARD_CAP", 10)
        monkeypatch.setattr(obs, "_EDIT_OBS_SOFT_CAP", 4)

        now = int(time.time())
        old_ts = now - 200 * 86_400  # older than the 90-day retention floor
        for i in range(8):
            record_edit_observation(REPO_A, f"old{i}.ts", "a", "high", observed_at=old_ts)
        # count is 8, still <= hard cap (10), so no prune yet.
        assert len(_read_rows(REPO_A)) == 8

        # Three recent inserts push count to 11 (> 10) and trip the prune,
        # which drops every row older than 90 days.
        for i in range(3):
            record_edit_observation(REPO_A, f"new{i}.ts", "a", "high", observed_at=now)

        rels = [r[0] for r in _read_rows(REPO_A)]
        assert rels == ["new0.ts", "new1.ts", "new2.ts"]

    def test_soft_cap_keeps_only_newest_when_all_recent(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(obs, "_EDIT_OBS_HARD_CAP", 10)
        monkeypatch.setattr(obs, "_EDIT_OBS_SOFT_CAP", 4)

        now = int(time.time())
        # 11 recent rows: the 90-day delete removes nothing, so the second
        # stage trims down to the newest SOFT_CAP (4) by observed_at DESC.
        for i in range(11):
            record_edit_observation(REPO_A, f"r{i:02d}.ts", "a", "high", observed_at=now + i)

        rows = _read_rows(REPO_A)
        assert len(rows) == 4
        kept = sorted(r[0] for r in rows)
        assert kept == ["r07.ts", "r08.ts", "r09.ts", "r10.ts"]

    def test_below_hard_cap_keeps_everything(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(obs, "_EDIT_OBS_HARD_CAP", 10)
        monkeypatch.setattr(obs, "_EDIT_OBS_SOFT_CAP", 4)

        now = int(time.time())
        for i in range(10):  # exactly at the cap, not over it
            record_edit_observation(REPO_A, f"r{i:02d}.ts", "a", "high", observed_at=now)
        # count == 10 is not > 10, so no pruning fires.
        assert len(_read_rows(REPO_A)) == 10


class TestConnectionCache:
    def test_busy_timeout_overridden_to_200ms(self, tmp_path: Path):
        conn = obs._get_drift_conn(REPO_A)
        # BUG-031: drift writes are on the hook hot path; timeout pinned at 200ms.
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 200

    def test_conn_is_cached_by_db_path(self, tmp_path: Path):
        c1 = obs._get_drift_conn(REPO_A)
        c2 = obs._get_drift_conn(REPO_A)
        assert c1 is c2
        assert str(obs._drift_db_path(REPO_A)) in obs._DRIFT_CONN

    def test_health_check_recovers_after_close(self, tmp_path: Path):
        c1 = obs._get_drift_conn(REPO_A)
        c1.close()  # cached conn is now dead; SELECT 1 will raise
        c2 = obs._get_drift_conn(REPO_A)
        assert c2 is not c1
        assert c2.execute("SELECT 1").fetchone()[0] == 1


class TestResetDriftBaseline:
    """Re-deriving the profile must re-baseline drift, else the drift banner's
    own recommended remediation (/chameleon-refresh) never clears the signal."""

    def test_reset_drift_baseline_clears_all_observations(self, tmp_path: Path):
        ts = 1_700_000_000
        for i in range(20):
            record_edit_observation(
                REPO_A, f"f{i}.ts", "component", "low", matched_canonical=False, observed_at=ts
            )
        assert len(_read_rows(REPO_A)) == 20

        deleted = obs.reset_drift_baseline(REPO_A)

        assert deleted == 20
        assert _read_rows(REPO_A) == []

    def test_reset_drift_baseline_is_scoped_to_one_repo(self, tmp_path: Path):
        ts = 1_700_000_000
        record_edit_observation(REPO_A, "a.ts", "component", "low", observed_at=ts)
        record_edit_observation(REPO_B, "b.ts", "component", "low", observed_at=ts)

        obs.reset_drift_baseline(REPO_A)

        assert _read_rows(REPO_A) == []
        assert len(_read_rows(REPO_B)) == 1

    def test_reset_drift_baseline_on_empty_db_returns_zero(self, tmp_path: Path):
        assert obs.reset_drift_baseline(REPO_A) == 0
