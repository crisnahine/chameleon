"""Unit tests for JourneyContext."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.journey.harness.context import JourneyContext, build_context


def test_build_context_creates_run_dir(tmp_path: Path) -> None:
    """build_context must create the run directory and all subdirs."""
    plugin_root = tmp_path / "chameleon"
    plugin_root.mkdir()
    results_root = tmp_path / "results"

    ctx = build_context(plugin_root, results_root)

    assert ctx.run_dir.exists()
    assert ctx.run_dir.parent == results_root
    assert (ctx.run_dir / "chameleon_data").exists()
    assert (ctx.run_dir / "tmp").exists()
    assert (ctx.run_dir / "working").exists()
    assert (ctx.run_dir / "checkpoints").exists()
    assert (ctx.run_dir / "transcripts").exists()
    assert (ctx.run_dir / "snapshots").exists()


def test_env_overrides_point_under_run_dir(tmp_path: Path) -> None:
    """All four env overrides must point under the run_dir."""
    plugin_root = tmp_path / "chameleon"
    plugin_root.mkdir()
    results_root = tmp_path / "results"

    ctx = build_context(plugin_root, results_root)

    assert ctx.env["CHAMELEON_PLUGIN_DATA"].startswith(str(ctx.run_dir))
    assert ctx.env["CHAMELEON_HMAC_KEY_PATH"].startswith(str(ctx.run_dir))
    assert ctx.env["TMPDIR"].startswith(str(ctx.run_dir))
    assert ctx.env["CHAMELEON_HOOK_ERROR_LOG"].startswith(str(ctx.run_dir))


def test_fast_forward_marker_ages_mtime(tmp_path: Path) -> None:
    """fast_forward_marker must rewind both atime and mtime."""
    plugin_root = tmp_path / "chameleon"
    plugin_root.mkdir()
    ctx = build_context(plugin_root, tmp_path / "results")

    marker = tmp_path / "marker.txt"
    marker.write_text("hi")

    ctx.fast_forward_marker(marker, age_seconds=3600)

    age = ctx.now() - marker.stat().st_mtime
    assert age >= 3600, f"expected mtime aged >= 3600s, got {age}"
