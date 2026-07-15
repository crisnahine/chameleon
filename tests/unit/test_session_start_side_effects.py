"""Unit tests for session_start() side effects in hook_helper.py.

Covers the four side-effect clusters that were previously stubbed/untested:

  (a) _drift_banner_for_repo  — the min-observations / score-threshold /
      cooldown-marker gates, the exact banner text, and the cooldown
      marker write.
  (b) _maybe_auto_refresh     — the enabled / drift / age / cooldown gates
      and the (mocked) subprocess.Popen spawn decision + argv.
  (c) statusline auto-wire     — the positive settings.local.json write, the
      defer-to-global-statusLine path, the stale-chameleon-command rewrite,
      and the preserve-user-command path.
  (d) enforcement-marker GC    — 24h-stale .enforcement.*.{json,lock} cleanup.

Isolation: there is no conftest.py in this tree. Every test that touches
plugin state points CHAMELEON_PLUGIN_DATA at a per-test tmp_path and resets
the drift-connection cache so a real drift.db opened here does not leak into
a sibling test (mirrors the autouse-fixture contract the project uses).
"""

from __future__ import annotations

import importlib
import io
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import chameleon_mcp.hook_helper as hh

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugin"


def _reset_drift_conn_cache() -> None:
    """Drop cached sqlite handles so a tmp drift.db cannot leak across tests."""
    try:
        from chameleon_mcp.drift import observations as _obs

        for c in list(_obs._DRIFT_CONN.values()):
            try:
                c.close()
            except Exception:
                pass
        _obs._DRIFT_CONN.clear()
    except Exception:
        pass


def _seed_drift_db(repo_id: str, n: int, confidence: float, *, age_step: int = 10) -> None:
    """Insert ``n`` observations at the given confidence into the repo's drift.db.

    Assumes CHAMELEON_PLUGIN_DATA is already pointed at the test data dir.
    """
    from chameleon_mcp.drift.observations import _drift_db_path
    from chameleon_mcp.drift.schema import init_drift_db

    db_path = _drift_db_path(repo_id)
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = init_drift_db(db_path)
    now = int(time.time())
    with conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO edit_observations "
                "(rel_path, archetype, confidence_observed, matched_canonical, observed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"f{i}.ts", "component", confidence, 0, now - i * age_step),
            )
    conn.close()


# --------------------------------------------------------------------------- #
# (a) _drift_banner_for_repo
# --------------------------------------------------------------------------- #


def test_drift_banner_fires_when_all_gates_pass(tmp_path, monkeypatch):
    """All three gates hold (count >= 10, score >= 0.4, no fresh marker):
    banner fires with the exact text and the cooldown marker is written."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_fire"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_stats",
            return_value={"score": 0.7, "count": 12},
        ),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is not None
    assert banner.startswith("[🦎 chameleon: structural conformance]")
    # The blind-spots disclaimer leads so the score is never read as a quality bar.
    assert "does NOT cover: logic, dataflow, cross-file, auth checks" in banner
    # score formatted to two decimals, observation count and the refresh nudge
    assert "Structural-conformance drift is 0.70 over the last 14 days (N=12 edits)" in banner
    assert "**/chameleon-refresh**" in banner

    # cooldown marker written under PLUGIN_DATA/<repo_id>/.drift_banner.last
    marker = tmp_path / "data" / "rid_fire" / hh._DRIFT_BANNER_FILENAME
    assert marker.is_file()
    # marker content is an int epoch second
    assert int(marker.read_text().strip()) > 0
    _reset_drift_conn_cache()


def test_drift_banner_score_is_formatted_to_two_decimals(tmp_path, monkeypatch):
    """A 0.456 score renders as 0.46 (round-half-to-even via %.2f)."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_fmt"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_stats",
            return_value={"score": 0.456, "count": 11},
        ),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is not None
    assert "drift is 0.46 over the last 14 days (N=11 edits)" in banner
    _reset_drift_conn_cache()


def test_drift_banner_suppressed_below_min_observations(tmp_path, monkeypatch):
    """count < DRIFT_BANNER_MIN_OBSERVATIONS (10) -> no banner, no marker."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_few"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_stats",
            return_value={"score": 0.99, "count": 9},
        ),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is None
    # gate failed before the marker write
    assert not (tmp_path / "data" / "rid_few" / hh._DRIFT_BANNER_FILENAME).exists()
    _reset_drift_conn_cache()


def test_drift_banner_suppressed_below_score_threshold(tmp_path, monkeypatch):
    """score < DRIFT_BANNER_THRESHOLD (0.4) -> no banner, no marker."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_lowscore"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_stats",
            return_value={"score": 0.39, "count": 50},
        ),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is None
    assert not (tmp_path / "data" / "rid_lowscore" / hh._DRIFT_BANNER_FILENAME).exists()
    _reset_drift_conn_cache()


def test_drift_banner_score_threshold_is_inclusive(tmp_path, monkeypatch):
    """score exactly at the 0.4 threshold fires (gate is `< threshold` -> suppress)."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_eq"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_stats",
            return_value={"score": 0.4, "count": 10},
        ),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is not None
    assert "drift is 0.40" in banner
    _reset_drift_conn_cache()


def test_drift_banner_cooldown_suppresses_second_call(tmp_path, monkeypatch):
    """A fresh cooldown marker from the first call suppresses the second."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_cd"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_stats",
            return_value={"score": 0.7, "count": 12},
        ),
    ):
        first = hh._drift_banner_for_repo(repo, session_id="s1")
        second = hh._drift_banner_for_repo(repo, session_id="s1")

    assert first is not None
    assert second is None
    _reset_drift_conn_cache()


def test_drift_banner_stale_cooldown_marker_fires_again(tmp_path, monkeypatch):
    """A cooldown marker older than the TTL (7d) lets the banner fire again."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()

    # pre-seed a stale marker (8 days old)
    marker = tmp_path / "data" / "rid_stale" / hh._DRIFT_BANNER_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(int(time.time())))
    old = time.time() - 8 * 24 * 3600
    os.utime(marker, (old, old))

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_stale"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_stats",
            return_value={"score": 0.7, "count": 12},
        ),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is not None
    # marker was refreshed (mtime now recent)
    assert (time.time() - marker.stat().st_mtime) < 5
    _reset_drift_conn_cache()


def test_drift_banner_suppressed_when_chameleon_opted_out(tmp_path, monkeypatch):
    """is_chameleon_suppressed returning a reason short-circuits the banner."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()

    stats_mock = MagicMock(return_value={"score": 0.99, "count": 99})
    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_optout"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value="repo_skip"),
        patch("chameleon_mcp.drift.observations.compute_drift_stats", stats_mock),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is None
    # short-circuit happens before drift stats are even computed
    stats_mock.assert_not_called()
    assert not (tmp_path / "data" / "rid_optout" / hh._DRIFT_BANNER_FILENAME).exists()
    _reset_drift_conn_cache()


def test_drift_banner_none_when_no_observations(tmp_path, monkeypatch):
    """compute_drift_stats returning None (no drift.db) -> no banner."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_empty"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.drift.observations.compute_drift_stats", return_value=None),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is None
    _reset_drift_conn_cache()


def test_drift_banner_integration_real_db(tmp_path, monkeypatch):
    """End-to-end through a real drift.db: 12 low-confidence (0.3) edits
    produce score 0.70 and the banner fires with that exact value."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_drift_db("rid_real", n=12, confidence=0.3)

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_real"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is not None
    # 1 - mean(0.3) = 0.7
    assert "drift is 0.70 over the last 14 days (N=12 edits)" in banner
    _reset_drift_conn_cache()


def test_drift_banner_integration_real_db_below_min_obs(tmp_path, monkeypatch):
    """Real drift.db with only 9 edits stays under the min-observations floor."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_drift_db("rid_real_few", n=9, confidence=0.3)

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_real_few"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
    ):
        banner = hh._drift_banner_for_repo(repo, session_id="s1")

    assert banner is None
    assert not (tmp_path / "data" / "rid_real_few" / hh._DRIFT_BANNER_FILENAME).exists()
    _reset_drift_conn_cache()


# --------------------------------------------------------------------------- #
# (b) _maybe_auto_refresh
# --------------------------------------------------------------------------- #


def _make_config(*, enabled=True, drift_threshold=0.2, max_age_hours=168):
    from chameleon_mcp.profile.config import AutoRefreshConfig, ChameleonConfig

    return ChameleonConfig(
        auto_refresh=AutoRefreshConfig(
            enabled=enabled,
            drift_threshold=drift_threshold,
            max_age_hours=max_age_hours,
        )
    )


def _run_auto_refresh(
    tmp_path,
    *,
    config,
    stats,
    repo_id,
    profile_age_hours: float | None = 0.0,
    with_enforcement: bool = True,
):
    """Drive _maybe_auto_refresh with a real .chameleon/profile.json and a
    mocked subprocess.Popen; return the Popen mock for inspection."""
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True, exist_ok=True)
    profile_json = repo / ".chameleon" / "profile.json"
    profile_json.write_text("{}", encoding="utf-8")
    # A migration-complete profile (calibration present) so the engine/enforcement
    # migration trigger stays inert and these tests isolate the drift/age gates.
    if with_enforcement:
        (repo / ".chameleon" / "enforcement.json").write_text(
            '{"block_rules": {}}', encoding="utf-8"
        )
    if profile_age_hours is not None:
        when = time.time() - profile_age_hours * 3600
        os.utime(profile_json, (when, when))

    popen = MagicMock()
    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.profile.config.load_config", return_value=config),
        patch("chameleon_mcp.drift.observations.compute_drift_stats", return_value=stats),
        patch("subprocess.Popen", popen),
    ):
        hh._maybe_auto_refresh(repo)
    return popen, repo


def test_auto_refresh_spawns_on_drift(tmp_path, monkeypatch):
    """drift score >= drift_threshold -> spawn refresh_repo, write cooldown marker,
    and the spawned argv targets refresh_repo with the resolved root."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    popen, repo = _run_auto_refresh(
        tmp_path,
        config=_make_config(drift_threshold=0.2),
        stats={"score": 0.5, "count": 20},
        repo_id="rid_ar_drift",
    )

    assert popen.called
    argv = popen.call_args[0][0]
    # [python, "-c", "...refresh_repo(<root>)"]
    assert argv[1] == "-c"
    assert "refresh_repo" in argv[2]
    assert str(repo.resolve()) in argv[2]
    # spawn detaches into its own session
    assert popen.call_args.kwargs.get("start_new_session") is True
    # cooldown marker written
    marker = tmp_path / "data" / "rid_ar_drift" / hh._AUTO_REFRESH_COOLDOWN_FILENAME
    assert marker.is_file()
    _reset_drift_conn_cache()


def test_auto_refresh_spawns_on_old_profile(tmp_path, monkeypatch):
    """No drift, but profile.json older than max_age_hours -> spawn by age gate."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    popen, _ = _run_auto_refresh(
        tmp_path,
        config=_make_config(drift_threshold=0.9, max_age_hours=168),
        stats={"score": 0.1, "count": 99},
        repo_id="rid_ar_old",
        profile_age_hours=200,  # > 168
    )
    assert popen.called
    _reset_drift_conn_cache()


def test_auto_refresh_skips_when_disabled(tmp_path, monkeypatch):
    """auto_refresh.enabled = False -> never spawn, even with extreme drift."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    popen, _ = _run_auto_refresh(
        tmp_path,
        config=_make_config(enabled=False),
        stats={"score": 0.99, "count": 99},
        repo_id="rid_ar_disabled",
    )
    assert not popen.called
    # no cooldown marker either (returned before the spawn block)
    assert not (tmp_path / "data" / "rid_ar_disabled" / hh._AUTO_REFRESH_COOLDOWN_FILENAME).exists()
    _reset_drift_conn_cache()


def test_auto_refresh_skips_when_no_drift_and_young_profile(tmp_path, monkeypatch):
    """drift below threshold AND profile younger than max_age -> no spawn."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    popen, _ = _run_auto_refresh(
        tmp_path,
        config=_make_config(drift_threshold=0.9, max_age_hours=168),
        stats={"score": 0.1, "count": 99},
        repo_id="rid_ar_quiet",
        profile_age_hours=1,  # well under 168
    )
    assert not popen.called


def test_auto_refresh_migration_fires_on_missing_enforcement(tmp_path, monkeypatch):
    """An existing user's pre-upgrade profile (no enforcement.json) auto-upgrades
    on the next session even with no drift and a young profile."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    popen, _ = _run_auto_refresh(
        tmp_path,
        config=_make_config(drift_threshold=0.9, max_age_hours=168),
        stats={"score": 0.1, "count": 99},
        repo_id="rid_ar_migrate",
        profile_age_hours=1,
        with_enforcement=False,
    )
    assert popen.called
    _reset_drift_conn_cache()
    _reset_drift_conn_cache()


def _prewrite_cooldown(tmp_path, repo_id, age_seconds):
    """Pre-write the auto-refresh cooldown marker for repo_id at a controlled age."""
    d = tmp_path / "data" / repo_id
    d.mkdir(parents=True, exist_ok=True)
    marker = d / hh._AUTO_REFRESH_COOLDOWN_FILENAME
    marker.write_text("", encoding="utf-8")
    when = time.time() - age_seconds
    os.utime(marker, (when, when))
    return marker


def test_auto_refresh_migration_bypasses_stale_general_cooldown(tmp_path, monkeypatch):
    # A pre-upgrade refresh's cooldown marker (up to ~42h) must NOT suppress the
    # engine-upgrade/missing-calibration migration: it caps the effective cooldown
    # at the short floor. Marker aged 2h (> 1h floor, < 42h general) -> fires.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    _prewrite_cooldown(tmp_path, "rid_mig_bypass", age_seconds=2 * 3600)
    popen, _ = _run_auto_refresh(
        tmp_path,
        config=_make_config(drift_threshold=0.9, max_age_hours=168),
        stats={"score": 0.0, "count": 0},
        repo_id="rid_mig_bypass",
        profile_age_hours=1,
        with_enforcement=False,  # migration due (no calibration)
    )
    assert popen.called
    _reset_drift_conn_cache()


def test_auto_refresh_migration_still_respects_short_floor(tmp_path, monkeypatch):
    # Storm guard: a very fresh marker (< 1h floor) suppresses even a migration, so
    # the async refresh is not re-spawned every session before it completes.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    _prewrite_cooldown(tmp_path, "rid_mig_floor", age_seconds=10 * 60)  # 10 min < 1h
    popen, _ = _run_auto_refresh(
        tmp_path,
        config=_make_config(drift_threshold=0.9, max_age_hours=168),
        stats={"score": 0.0, "count": 0},
        repo_id="rid_mig_floor",
        profile_age_hours=1,
        with_enforcement=False,
    )
    assert not popen.called
    _reset_drift_conn_cache()


def test_auto_refresh_general_cooldown_unchanged_for_nonmigration(tmp_path, monkeypatch):
    # A non-migration (drift/age) refresh is still gated by the FULL general
    # cooldown: a 2h-old marker suppresses a drift-triggered refresh (~42h window),
    # so the migration bypass did not widen the general firing.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    _prewrite_cooldown(tmp_path, "rid_gen_cd", age_seconds=2 * 3600)
    popen, _ = _run_auto_refresh(
        tmp_path,
        config=_make_config(drift_threshold=0.2, max_age_hours=168),
        stats={"score": 0.9, "count": 20},  # drift present, but cooldown still fresh
        repo_id="rid_gen_cd",
        profile_age_hours=1,
        with_enforcement=True,  # not a migration
    )
    assert not popen.called
    _reset_drift_conn_cache()


def _run_auto_refresh_prodtip(tmp_path, *, repo_id, resolved_sha, recorded_sha):
    """Drive _maybe_auto_refresh for a production-pinned repo with no drift and a
    young profile, so the locked-tip comparison is the ONLY live trigger. Returns
    the mocked Popen. resolved_sha is the locked ref's current tip; recorded_sha
    is the SHA the profile was last derived from."""
    from chameleon_mcp.production_ref import ResolvedRef

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True, exist_ok=True)
    profile_json = repo / ".chameleon" / "profile.json"
    profile_json.write_text("{}", encoding="utf-8")
    # enforcement.json present + empty engine stamp -> the migration trigger stays
    # inert, isolating the production-tip gate.
    (repo / ".chameleon" / "enforcement.json").write_text('{"block_rules": {}}', encoding="utf-8")
    now = time.time()
    os.utime(profile_json, (now, now))  # young profile -> age gate inert

    popen = MagicMock()
    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch(
            "chameleon_mcp.profile.config.load_config",
            return_value=_make_config(drift_threshold=0.9, max_age_hours=168),
        ),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_stats",
            return_value={"score": 0.0, "count": 0},  # no drift -> drift gate inert
        ),
        patch("chameleon_mcp.tools._persisted_production_ref", return_value="main"),
        patch(
            "chameleon_mcp.production_ref.resolve_production_ref",
            return_value=ResolvedRef(ref="origin/main", sha=resolved_sha),
        ),
        patch("chameleon_mcp.tools._recorded_derivation_sha", return_value=recorded_sha),
        patch("subprocess.Popen", popen),
    ):
        hh._maybe_auto_refresh(repo)
    return popen


def test_auto_refresh_spawns_on_production_tip_moved(tmp_path, monkeypatch):
    """Production-pinned repo whose locked tip moved past the recorded derivation
    SHA (a teammate merged to the production branch) -> spawn refresh even with no
    working-tree drift and a young profile. This is THE freshness signal for
    pinned repos, and it fires hands-off at the next session start."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    popen = _run_auto_refresh_prodtip(
        tmp_path,
        repo_id="rid_ar_tipmoved",
        resolved_sha="newtipsha1234",
        recorded_sha="oldderivedsha99",
    )
    assert popen.called
    argv = popen.call_args[0][0]
    assert argv[1] == "-c"
    assert "refresh_repo" in argv[2]
    assert popen.call_args.kwargs.get("start_new_session") is True
    _reset_drift_conn_cache()


def test_auto_refresh_skips_when_production_tip_unchanged(tmp_path, monkeypatch):
    """Production-pinned repo whose locked tip still matches the recorded
    derivation SHA, with no drift and a young profile -> no spawn. A feature-branch
    session never moves a production-pinned profile, so chameleon stays quiet."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    popen = _run_auto_refresh_prodtip(
        tmp_path,
        repo_id="rid_ar_tipsame",
        resolved_sha="sametip5678",
        recorded_sha="sametip5678",
    )
    assert not popen.called
    _reset_drift_conn_cache()


def test_auto_refresh_skips_without_chameleon_dir(tmp_path, monkeypatch):
    """No .chameleon/ profile dir -> return before reading config or spawning."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    bare = tmp_path / "bare"
    bare.mkdir()

    popen = MagicMock()
    load_cfg = MagicMock()
    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=bare),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_ar_nodir"),
        patch("chameleon_mcp.profile.config.load_config", load_cfg),
        patch("subprocess.Popen", popen),
    ):
        hh._maybe_auto_refresh(bare)

    assert not popen.called
    load_cfg.assert_not_called()  # bailed before load_config
    _reset_drift_conn_cache()


def test_auto_refresh_cooldown_suppresses_second_call(tmp_path, monkeypatch):
    """A fresh cooldown marker from the first spawn suppresses the next."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "profile.json").write_text("{}", encoding="utf-8")
    cfg = _make_config(drift_threshold=0.2)

    popen = MagicMock()
    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_ar_cd"),
        patch("chameleon_mcp.profile.config.load_config", return_value=cfg),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_stats",
            return_value={"score": 0.9, "count": 50},
        ),
        patch("subprocess.Popen", popen),
    ):
        hh._maybe_auto_refresh(repo)
        first_count = popen.call_count
        hh._maybe_auto_refresh(repo)
        second_count = popen.call_count

    assert first_count == 1
    assert second_count == 1  # second call did not spawn
    _reset_drift_conn_cache()


# --------------------------------------------------------------------------- #
# (c) statusline auto-wire  (exercised via session_start)
# --------------------------------------------------------------------------- #

_STATUSLINE_SCRIPT = str(_PLUGIN_ROOT / "bin" / "chameleon-statusline.sh")


def _run_session_start_for_statusline(project_dir, home_dir, monkeypatch):
    """Drive session_start with cwd=project_dir and home=home_dir.

    Drift banner + auto-refresh are stubbed out (covered above) and the repo
    has no profile, so only the statusline-wire block does work.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN_ROOT))
    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"}))),
        patch("sys.stdout") as mock_stdout,
        patch("pathlib.Path.cwd", return_value=project_dir),
        patch("pathlib.Path.home", return_value=home_dir),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None),
        patch("chameleon_mcp.hook_helper._drift_banner_for_repo", return_value=None),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=None),
    ):
        mock_stdout.write = lambda s: captured.append(s)
        rc = hh.session_start()
    return rc, "".join(captured)


def test_statusline_positive_write(tmp_path, monkeypatch):
    """No prior settings + no global statusLine -> write settings.local.json
    pointing at the plugin's chameleon-statusline.sh."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    rc, _ = _run_session_start_for_statusline(project, home, monkeypatch)
    assert rc == 0

    sl = project / ".claude" / "settings.local.json"
    assert sl.is_file()
    data = json.loads(sl.read_text(encoding="utf-8"))
    assert data["statusLine"] == {"type": "command", "command": _STATUSLINE_SCRIPT}


def test_statusline_defers_to_global_statusline(tmp_path, monkeypatch):
    """A statusLine in ~/.claude/settings.json means no local write
    (settings.local.json would silently override the user's global choice)."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "my-global-statusline"}}),
        encoding="utf-8",
    )

    rc, _ = _run_session_start_for_statusline(project, home, monkeypatch)
    assert rc == 0
    assert not (project / ".claude" / "settings.local.json").exists()


def test_statusline_defers_to_project_settings_statusline(tmp_path, monkeypatch):
    """A statusLine in the project .claude/settings.json suppresses the
    local write too (project settings own the statusline)."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "proj-statusline"}}),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()

    rc, _ = _run_session_start_for_statusline(project, home, monkeypatch)
    assert rc == 0
    assert not (project / ".claude" / "settings.local.json").exists()


def test_statusline_rewrites_stale_chameleon_command(tmp_path, monkeypatch):
    """A stale chameleon statusline command in settings.local.json gets
    rewritten to the current path; unrelated keys are preserved."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    sl = project / ".claude" / "settings.local.json"
    sl.write_text(
        json.dumps(
            {
                "statusLine": {
                    "type": "command",
                    "command": "/old/install/bin/chameleon-statusline.sh",
                },
                "permissions": {"allow": ["X"]},
            }
        ),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()

    rc, _ = _run_session_start_for_statusline(project, home, monkeypatch)
    assert rc == 0
    data = json.loads(sl.read_text(encoding="utf-8"))
    assert data["statusLine"]["command"] == _STATUSLINE_SCRIPT
    # unrelated key untouched
    assert data["permissions"] == {"allow": ["X"]}


def test_statusline_preserves_user_custom_command(tmp_path, monkeypatch):
    """A non-chameleon user statusline command in settings.local.json is NOT
    overwritten (rewrite only fires when 'chameleon' is in the old command)."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    sl = project / ".claude" / "settings.local.json"
    sl.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "/usr/local/bin/my-line"}}),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()

    rc, _ = _run_session_start_for_statusline(project, home, monkeypatch)
    assert rc == 0
    data = json.loads(sl.read_text(encoding="utf-8"))
    assert data["statusLine"]["command"] == "/usr/local/bin/my-line"


def test_statusline_idempotent_when_already_current(tmp_path, monkeypatch):
    """If settings.local.json already has the current chameleon command,
    the value is left unchanged (old_cmd == current_cmd, no rewrite)."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    sl = project / ".claude" / "settings.local.json"
    sl.write_text(
        json.dumps({"statusLine": {"type": "command", "command": _STATUSLINE_SCRIPT}, "k": 1}),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()

    rc, _ = _run_session_start_for_statusline(project, home, monkeypatch)
    assert rc == 0
    data = json.loads(sl.read_text(encoding="utf-8"))
    assert data["statusLine"]["command"] == _STATUSLINE_SCRIPT
    assert data["k"] == 1


# --------------------------------------------------------------------------- #
# (d) enforcement-marker GC  (exercised via session_start)
# --------------------------------------------------------------------------- #


def test_enforcement_marker_gc_removes_stale_keeps_fresh(tmp_path, monkeypatch):
    """24h-stale .enforcement.*.json/.lock markers are unlinked; fresh markers
    and non-enforcement files (drift.db) survive."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN_ROOT))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()

    repo_id = "rid_gc"
    repo_data = tmp_path / "data" / repo_id
    repo_data.mkdir(parents=True)

    stale_json = repo_data / ".enforcement.sessA.json"
    stale_lock = repo_data / ".enforcement.sessA.lock"
    fresh_json = repo_data / ".enforcement.sessB.json"
    untouched = repo_data / "drift.db"
    for f in (stale_json, stale_lock, fresh_json, untouched):
        f.write_text("x", encoding="utf-8")
    old = time.time() - 86400 - 600  # > 24h
    for f in (stale_json, stale_lock):
        os.utime(f, (old, old))

    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"}))),
        patch("sys.stdout") as mock_stdout,
        patch("pathlib.Path.cwd", return_value=project),
        patch("pathlib.Path.home", return_value=home),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None),
        patch("chameleon_mcp.hook_helper._drift_banner_for_repo", return_value=None),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=project),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
    ):
        mock_stdout.write = lambda s: captured.append(s)
        rc = hh.session_start()

    assert rc == 0
    assert not stale_json.exists()
    assert not stale_lock.exists()
    assert fresh_json.exists()
    assert untouched.exists()
    _reset_drift_conn_cache()


def test_enforcement_marker_gc_noop_when_no_repo_data_dir(tmp_path, monkeypatch):
    """When the repo has no data dir under PLUGIN_DATA, GC is a no-op and
    session_start still completes cleanly."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN_ROOT))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()

    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"}))),
        patch("sys.stdout") as mock_stdout,
        patch("pathlib.Path.cwd", return_value=project),
        patch("pathlib.Path.home", return_value=home),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None),
        patch("chameleon_mcp.hook_helper._drift_banner_for_repo", return_value=None),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=project),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_gc_absent"),
    ):
        mock_stdout.write = lambda s: captured.append(s)
        rc = hh.session_start()

    assert rc == 0
    # the data dir was never created (no repo_data.is_dir())
    assert not (tmp_path / "data" / "rid_gc_absent").exists()
    _reset_drift_conn_cache()


# --------------------------------------------------------------------------- #
# env-var-at-import threshold override (DRIFT_BANNER_MIN_OBSERVATIONS)
# --------------------------------------------------------------------------- #


def test_drift_banner_respects_min_observations_env_override(tmp_path, monkeypatch):
    """CHAMELEON_DRIFT_BANNER_MIN_OBSERVATIONS=20 raises the floor: 12 edits
    (which fire at the default 10) are now suppressed.

    Thresholds are read per-call via threshold_int, not at import, so the env
    override takes effect without reloading. We still reload _thresholds to
    document the import-time-env pattern and prove it's harmless here."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_MIN_OBSERVATIONS", "20")
    _reset_drift_conn_cache()

    import chameleon_mcp._thresholds as _th

    importlib.reload(_th)
    try:
        assert _th.threshold_int("DRIFT_BANNER_MIN_OBSERVATIONS") == 20
        repo = tmp_path / "repo"
        repo.mkdir()
        with (
            patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
            patch("chameleon_mcp.tools._compute_repo_id", return_value="rid_env"),
            patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
            patch(
                "chameleon_mcp.drift.observations.compute_drift_stats",
                return_value={"score": 0.9, "count": 12},
            ),
        ):
            banner = hh._drift_banner_for_repo(repo, session_id="s1")
        assert banner is None
    finally:
        monkeypatch.delenv("CHAMELEON_DRIFT_BANNER_MIN_OBSERVATIONS", raising=False)
        importlib.reload(_th)
    _reset_drift_conn_cache()


# --------------------------------------------------------------------------- #
# (e) _idiom_candidates_note -- SessionStart "learned from usage" surfacing
# --------------------------------------------------------------------------- #


def test_idiom_candidates_note_fires_with_candidate_count(tmp_path):
    from chameleon_mcp.core.idiom_candidates import write_candidate

    profile_dir = tmp_path / "repo" / ".chameleon"
    profile_dir.mkdir(parents=True)
    write_candidate(
        profile_dir,
        slug="prefer-api-client",
        title="Prefer apiClient",
        rationale="r",
        source="learned",
        evidence="e",
    )
    write_candidate(
        profile_dir,
        slug="ban-console-log",
        title="Ban console.log",
        rationale="r",
        source="learned",
        evidence="e",
    )

    note = hh._idiom_candidates_note(profile_dir)

    assert note is not None
    assert "learned 2 idiom candidate(s) from usage" in note
    assert "/chameleon-auto-idiom" in note
    assert "nothing is adopted without your approval" in note


def test_idiom_candidates_note_none_when_no_candidates(tmp_path):
    profile_dir = tmp_path / "repo" / ".chameleon"
    profile_dir.mkdir(parents=True)
    assert hh._idiom_candidates_note(profile_dir) is None


def test_idiom_candidates_note_none_when_profile_dir_absent(tmp_path):
    # A repo that was never bootstrapped: profile_dir doesn't exist at all.
    profile_dir = tmp_path / "repo" / ".chameleon"
    assert hh._idiom_candidates_note(profile_dir) is None


def test_idiom_candidates_note_none_and_no_crash_on_corrupt_candidates_dir(tmp_path):
    # idiom-candidates exists as a FILE instead of a directory: load_candidates
    # fails open to [] (glob raises NotADirectoryError, an OSError subclass), so
    # the note is silently absent rather than raising.
    profile_dir = tmp_path / "repo" / ".chameleon"
    profile_dir.mkdir(parents=True)
    (profile_dir / "idiom-candidates").write_text("not a directory", encoding="utf-8")

    assert hh._idiom_candidates_note(profile_dir) is None


def test_idiom_candidates_note_none_when_load_candidates_raises(tmp_path):
    profile_dir = tmp_path / "repo" / ".chameleon"
    profile_dir.mkdir(parents=True)
    with patch(
        "chameleon_mcp.core.idiom_candidates.load_candidates",
        side_effect=RuntimeError("boom"),
    ):
        assert hh._idiom_candidates_note(profile_dir) is None


def test_idiom_candidates_note_suppressed_by_miner_kill_switch(tmp_path, monkeypatch):
    from chameleon_mcp.core.idiom_candidates import write_candidate

    profile_dir = tmp_path / "repo" / ".chameleon"
    profile_dir.mkdir(parents=True)
    write_candidate(
        profile_dir, slug="a-slug", title="t", rationale="r", source="learned", evidence="e"
    )
    monkeypatch.setenv("CHAMELEON_IDIOM_MINER", "0")

    assert hh._idiom_candidates_note(profile_dir) is None


def _run_session_start_for_idiom_note(repo, tmp_path, session_id="s1", repo_id="rid_idiom") -> str:
    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps({"session_id": session_id}))),
        patch("sys.stdout") as mock_stdout,
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None),
        patch("chameleon_mcp.hook_helper._wire_statusline_settings", lambda *a, **k: None),
        patch("chameleon_mcp.hook_helper._drift_banner_for_repo", return_value=None),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
    ):
        mock_stdout.write = lambda s: captured.append(s)
        rc = hh.session_start()
    assert rc == 0
    return "".join(captured)


def test_session_start_includes_idiom_candidates_note_when_present(tmp_path, monkeypatch):
    """End-to-end: a profile with >=1 candidate file surfaces the note in the
    real session_start() output, naming the count and pointing at the skill."""
    from chameleon_mcp.core.idiom_candidates import write_candidate

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN_ROOT))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    write_candidate(
        repo / ".chameleon",
        slug="a-slug",
        title="t",
        rationale="r",
        source="learned",
        evidence="e",
    )

    out = _run_session_start_for_idiom_note(repo, tmp_path, repo_id="rid_idiom_note")

    assert "learned 1 idiom candidate(s) from usage" in out
    assert "/chameleon-auto-idiom" in out
    _reset_drift_conn_cache()


def test_session_start_omits_idiom_candidates_note_when_zero_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN_ROOT))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)

    out = _run_session_start_for_idiom_note(repo, tmp_path, repo_id="rid_idiom_note_zero")

    assert "idiom candidate(s) from usage" not in out
    _reset_drift_conn_cache()


def test_session_start_omits_idiom_candidates_note_on_corrupt_candidates_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN_ROOT))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _reset_drift_conn_cache()

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "idiom-candidates").write_text("not a directory", encoding="utf-8")

    out = _run_session_start_for_idiom_note(repo, tmp_path, repo_id="rid_idiom_note_corrupt")

    assert "idiom candidate(s) from usage" not in out
    _reset_drift_conn_cache()


def test_session_start_suppresses_idiom_candidates_note_under_miner_kill_switch(
    tmp_path, monkeypatch
):
    from chameleon_mcp.core.idiom_candidates import write_candidate

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN_ROOT))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_IDIOM_MINER", "0")
    _reset_drift_conn_cache()

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    write_candidate(
        repo / ".chameleon",
        slug="a-slug",
        title="t",
        rationale="r",
        source="learned",
        evidence="e",
    )

    out = _run_session_start_for_idiom_note(repo, tmp_path, repo_id="rid_idiom_note_kill")

    assert "idiom candidate(s) from usage" not in out
    _reset_drift_conn_cache()
