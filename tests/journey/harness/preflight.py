"""Runner-side preflight checks. Abort before any Claude spawn if missing.

Checked:
  - claude CLI on PATH
  - git --version >= 2.28
  - committed seed fixtures present
  - plugin/mcp/.venv/bin/python present
  - no concurrent runner (lockfile in run_dir parent)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from tests.journey.harness.fixtures import check_git_version


class PreflightError(Exception):
    pass


def claude_on_path() -> Path:
    p = shutil.which("claude")
    if not p:
        raise PreflightError(
            "`claude` CLI not on PATH; install Claude Code or unset CHAMELEON_TEST_NO_CLAUDE"
        )
    return Path(p)


def python_venv_present(plugin_dir: Path) -> Path:
    """``plugin_dir`` is the installable plugin dir (``<repo>/plugin``)."""
    p = plugin_dir / "mcp" / ".venv" / "bin" / "python"
    if not p.is_file():
        raise PreflightError(
            f"missing {p}; run `cd plugin/mcp && uv sync` from the chameleon repo first"
        )
    return p


def fixtures_present(
    repo_root: Path,
    fixtures_root: Path | None = None,
    required: list[str] | None = None,
) -> dict[str, Path]:
    """Check committed seed fixtures exist and are non-empty.

    Defaults preserve the journey behavior; the effectiveness runner passes its
    own fixtures_root + required list.
    """
    if fixtures_root is None:
        fixtures_root = repo_root / "tests" / "journey" / "fixtures"
    if required is None:
        required = ["ts_basic", "rails_basic", "ts_monorepo", "ts_with_rails_sidecar"]
    found: dict[str, Path] = {}
    missing: list[str] = []
    for name in required:
        path = fixtures_root / name
        if not path.is_dir() or not any(path.iterdir()):
            missing.append(name)
        else:
            found[name] = path
    if missing:
        raise PreflightError(f"missing fixtures: {missing}; expected under {fixtures_root}")
    return found


def acquire_lock(run_dir: Path) -> Path:
    """Acquire an exclusive lock for the current run_dir. Returns path."""
    lock_path = run_dir / ".lock"
    if lock_path.exists():
        raise PreflightError(f"another runner has acquired {lock_path}; aborting")
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    return lock_path


def run_all(repo_root: Path, run_dir: Path, plugin_dir: Path | None = None) -> dict:
    """Run every preflight check. Returns a dict of resolved paths.

    ``repo_root`` locates the committed fixtures under tests/; ``plugin_dir``
    locates the MCP venv and defaults to ``repo_root / "plugin"``.
    """
    if plugin_dir is None:
        plugin_dir = repo_root / "plugin"
    return {
        "claude": claude_on_path(),
        "git_version": check_git_version((2, 28)),
        "python_venv": python_venv_present(plugin_dir),
        "fixtures": fixtures_present(repo_root),
        "lock_path": acquire_lock(run_dir),
    }
