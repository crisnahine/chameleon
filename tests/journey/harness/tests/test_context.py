"""Unit tests for JourneyContext."""

from __future__ import annotations

from pathlib import Path

from tests.journey.harness.context import build_context


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


def test_build_context_honors_run_prefix(tmp_path: Path) -> None:
    """A non-journey caller gets its own run-dir prefix; default stays journey_."""
    plugin_root = tmp_path / "chameleon"
    plugin_root.mkdir()

    ctx_default = build_context(plugin_root, tmp_path / "r1")
    assert ctx_default.run_dir.name.startswith("journey_")

    ctx_eff = build_context(plugin_root, tmp_path / "r2", run_prefix="effectiveness")
    assert ctx_eff.run_dir.name.startswith("effectiveness_")
    assert (ctx_eff.run_dir / "chameleon_data").exists()
    assert ctx_eff.env["CHAMELEON_PLUGIN_DATA"].startswith(str(ctx_eff.run_dir))
