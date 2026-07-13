"""Runner-side preflight checks. Abort before any Claude spawn if missing.

Checked:
  - claude CLI on PATH
  - git --version >= 2.28
  - committed seed fixtures present
  - plugin/mcp/.venv/bin/python present
  - no concurrent runner (lockfile in run_dir parent)
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tests.journey.harness.fixtures import check_git_version


class PreflightError(Exception):
    pass


# Advisory-lock context managers held for this process's lifetime (see
# acquire_lock below). Kept alive here so GC never closes the generator and
# releases the flock early.
_HELD_LOCKS: list = []


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


def acquire_lock(results_root: Path) -> Path:
    """Acquire an exclusive, cross-invocation lock. Returns the lock path.

    Scoped to the shared results root, not a per-invocation run_dir -- a
    run_dir is already uniquely timestamped per invocation, so a lock file
    living inside one could never actually be contended by a second runner.
    Takes results_root directly (not a run_dir to derive it from) so the lock
    can be acquired BEFORE a run_dir is created: the caller must acquire this
    lock first and only then create its run_dir, or a second invocation
    racing to create ITS OWN uniquely-timestamped run_dir would never trip
    this lock at all. Uses the repo's own flock-based advisory lock
    (chameleon_mcp.locks) rather than a plain exists()-then-write check, so a
    crashed prior runner's hold is released by the OS at process exit instead
    of leaving a stale marker file that would wedge every later invocation.
    """
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    lock_path = results_root / ".journey_runner.lock"
    cm = acquire_advisory_lock(lock_path)
    try:
        cm.__enter__()
    except LockHeldError as e:
        raise PreflightError(f"another runner has acquired {lock_path}: {e}") from e
    # Held for this process's entire lifetime, deliberately never __exit__'d:
    # the point is to block a concurrent runner for as long as this run is in
    # progress, not just for the instant of this check. The OS releases the
    # underlying flock when the process exits (including a crash), so a dead
    # runner never wedges a later one. Keep a reference so GC never closes the
    # generator-backed context manager and releases the lock early.
    _HELD_LOCKS.append(cm)
    return lock_path


def run_all(repo_root: Path, results_root: Path, plugin_dir: Path | None = None) -> dict:
    """Run every preflight check. Returns a dict of resolved paths.

    ``repo_root`` locates the committed fixtures under tests/; ``plugin_dir``
    locates the MCP venv and defaults to ``repo_root / "plugin"``. ``results_root``
    is the shared results directory the lock is scoped to -- call this BEFORE
    creating a per-invocation run_dir under it, so a genuinely concurrent
    invocation trips the lock instead of each one silently creating its own
    uniquely-timestamped run_dir and never contending.
    """
    if plugin_dir is None:
        plugin_dir = repo_root / "plugin"
    return {
        "claude": claude_on_path(),
        "git_version": check_git_version((2, 28)),
        "python_venv": python_venv_present(plugin_dir),
        "fixtures": fixtures_present(repo_root),
        "lock_path": acquire_lock(results_root),
    }
