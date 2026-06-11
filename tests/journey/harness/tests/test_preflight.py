"""Unit tests for preflight checks (the parts that don't need claude on PATH)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.journey.harness.preflight import PreflightError, fixtures_present


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
