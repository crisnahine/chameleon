"""Per-(task, arm, repeat) git worktree lifecycle.

Each cell runs in a fresh detached worktree of the bootstrapped fixture repo
so arms can never contaminate each other. Arm config flip + task setup are
committed as an "arm setup" commit BEFORE the session, so changed_files()
sees only what the session itself did. Worktrees live under the run dir
(gitignored, per-run ephemeral) and are deliberately NOT removed afterwards:
they are the forensic record run.md points at.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.effectiveness.arms import ArmSpec, apply_arm_config
from tests.journey.harness.bash import run_bash

_GIT_ID = "-c user.name=effectiveness -c user.email=eff@local"


class WorktreeError(Exception):
    pass


def _git(cwd: Path, args: str, timeout_s: int = 60) -> str:
    r = run_bash(f"git {args}", cwd=cwd, timeout_s=timeout_s)
    if r.returncode != 0:
        raise WorktreeError(f"git {args} failed in {cwd}: {r.stderr.strip()}")
    return r.stdout


def prepare_cell(
    *,
    fixture_repo: Path,
    dest: Path,
    arm: ArmSpec,
    setup_fn: Callable[[Path], None] | None,
    trust_fn: Callable[[Path], str],
) -> str:
    """Create the cell's worktree, apply arm + setup, commit, grant trust.

    Returns the baseline commit SHA (post-setup HEAD). Trust is granted for
    EVERY arm — the off arm's session ignores the profile via
    CHAMELEON_DISABLE, but post-session scoring still needs trusted reads.
    """
    _git(fixture_repo, f'worktree add --detach "{dest}" HEAD')
    apply_arm_config(arm, dest)
    if setup_fn is not None:
        setup_fn(dest)
    _git(dest, f"{_GIT_ID} add -A")
    _git(dest, f'{_GIT_ID} commit -q --allow-empty -m "arm setup: {arm.name}"')
    baseline_sha = _git(dest, "rev-parse HEAD").strip()
    trust_fn(dest)
    return baseline_sha


def changed_files(worktree: Path, baseline_sha: str) -> list[str]:
    """Repo-relative files the session changed: tracked diffs + untracked.

    `.chameleon/` paths are excluded — profile artifacts are harness state,
    not task output (a session that edits them is visible in the raw diff
    artifact, but scorers must not lint them).
    """
    tracked = _git(worktree, f"diff --name-only {baseline_sha}").splitlines()
    untracked = _git(worktree, "ls-files --others --exclude-standard").splitlines()
    merged = {p.strip() for p in tracked + untracked if p.strip()}
    return sorted(p for p in merged if not p.startswith(".chameleon/"))


def session_diff(worktree: Path, baseline_sha: str) -> str:
    """Unified diff of everything the session did (for artifacts + the panel)."""
    _git(worktree, f"{_GIT_ID} add -A")  # stage untracked so diff covers them
    diff = _git(worktree, f"diff --cached {baseline_sha}", timeout_s=120)
    _git(worktree, "reset -q")  # leave the tree as the session left it
    return diff
