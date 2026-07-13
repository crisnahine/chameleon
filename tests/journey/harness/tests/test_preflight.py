"""Unit tests for preflight checks (the parts that don't need claude on PATH)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.journey.harness.preflight import PreflightError, acquire_lock, fixtures_present


def test_fixtures_present_default_journey_list() -> None:
    """Default args still check the journey seeds in the real repo."""
    plugin_root = Path(__file__).resolve().parents[4]
    found = fixtures_present(plugin_root)
    assert set(found) == {"ts_basic", "rails_basic", "ts_monorepo", "ts_with_rails_sidecar"}


def test_fixtures_present_custom_root_and_required(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    (root / "eff_ts").mkdir(parents=True)
    (root / "eff_ts" / "f.txt").write_text("x")
    found = fixtures_present(tmp_path, fixtures_root=root, required=["eff_ts"])
    assert found == {"eff_ts": root / "eff_ts"}


def test_fixtures_present_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(PreflightError):
        fixtures_present(tmp_path, fixtures_root=tmp_path / "nope", required=["eff_ts"])


def test_acquire_lock_scoped_to_results_root(tmp_path: Path) -> None:
    """The lock lives directly under results_root, not inside any run_dir:
    it must be acquirable BEFORE a run_dir exists, so a genuinely concurrent
    invocation trips it before either side creates its own uniquely-
    timestamped run_dir (which would never contend on its own)."""
    results_root = tmp_path / "results"
    results_root.mkdir(parents=True)

    lock_path = acquire_lock(results_root)

    assert lock_path == results_root / ".journey_runner.lock"


def test_acquire_lock_rejects_concurrent_invocation(tmp_path: Path) -> None:
    """A lock already held for a results root blocks a second invocation
    against that SAME root with a clean PreflightError -- the
    concurrent-runner case this lock exists to catch. Acquired before either
    side's run_dir exists, matching the real caller's ordering."""
    results_root = tmp_path / "results"
    results_root.mkdir(parents=True)

    acquire_lock(results_root)

    with pytest.raises(PreflightError):
        acquire_lock(results_root)
