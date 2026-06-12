"""Per-(task, arm, repeat) git worktree lifecycle.

Each cell runs in a fresh detached worktree of the bootstrapped fixture repo
so arms can never contaminate each other. Arm config flip + task setup are
committed as an "arm setup" commit BEFORE the session, so changed_files()
sees only what the session itself did. Worktrees live under the run dir
(gitignored, per-run ephemeral). Committed-fixture clones keep their
worktrees afterwards — they are the forensic record run.md points at — but
cells on env-pointed REAL repos are unregistered on success via
remove_cell_worktree(), so a run never accumulates registrations in the
user's own repo.
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
) -> str:
    """Create the cell's worktree, apply arm + setup, commit.

    Returns the baseline commit SHA (post-setup HEAD). Trust is granted by
    the runner (which needs the repo_id for scoring), AFTER this returns —
    the config flip must precede the grant because config.json is part of
    the trust hash.
    """
    _git(fixture_repo, f'worktree add --detach "{dest}" HEAD')
    apply_arm_config(arm, dest)
    if setup_fn is not None:
        setup_fn(dest)
    _git(dest, f"{_GIT_ID} add -A")
    _git(dest, f'{_GIT_ID} commit -q --allow-empty -m "arm setup: {arm.name}"')
    return _git(dest, "rev-parse HEAD").strip()


def remove_cell_worktree(fixture_repo: Path, dest: Path) -> None:
    """Unregister and delete one cell worktree from its fixture repo.

    Env-pointed (tier-full) repos are the user's REAL repos; a leaked
    registration would sit in them until a manual `git worktree prune`.
    Committed-fixture clones never call this — their worktrees are retained
    as the forensic record.
    """
    _git(fixture_repo, f'worktree remove --force "{dest}"')


def changed_files(worktree: Path, baseline_sha: str) -> list[str]:
    """Repo-relative files the session changed: tracked diffs + untracked.

    `.chameleon/` paths are excluded — profile artifacts are harness state,
    not task output (a session that edits them is visible in the raw diff
    artifact, but scorers must not lint them). `.claude/` paths are excluded
    for the same harness-state reason plus blinding: session-runtime files
    there (statusline cache, local settings) carry the cell name, which
    encodes the arm.
    """
    tracked = _git(worktree, f"diff --name-only {baseline_sha}").splitlines()
    untracked = _git(worktree, "ls-files --others --exclude-standard").splitlines()
    merged = {p.strip() for p in tracked + untracked if p.strip()}
    return sorted(p for p in merged if not p.startswith((".chameleon/", ".claude/")))


def session_diff(worktree: Path, baseline_sha: str) -> str:
    """Unified diff of everything the session did (for artifacts + the panel).

    `.claude/` is excluded for the same reason changed_files() excludes it:
    its files name the cell (and therefore the arm), and this diff is what
    the blind judge panel reads. `.chameleon/` stays visible here as the
    forensic record of profile edits; scorers never read this diff.
    """
    _git(worktree, f"{_GIT_ID} add -A")  # stage untracked so diff covers them
    diff = _git(worktree, f"diff --cached {baseline_sha} -- . ':(exclude).claude'", timeout_s=120)
    _git(worktree, "reset -q")  # leave the tree as the session left it
    return diff
