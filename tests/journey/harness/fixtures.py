"""Fixture setup: copy committed seed to <run_dir>/working, init git, set up loopback origin.

Committed seeds under tests/journey/fixtures/<name>/ are SOURCE-CODE-ONLY (no .git/).
This module initializes them as git repos with a bare loopback origin so
`git show origin/main:<artifact>` works offline.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from tests.journey.harness.bash import run_bash


class GitVersionError(Exception):
    pass


def check_git_version(min_version: tuple[int, int] = (2, 28)) -> tuple[int, int]:
    """Verify git --version >= min_version. Returns (major, minor)."""
    result = run_bash("git --version")
    if result.returncode != 0:
        raise GitVersionError(f"git not found: {result.stderr}")
    match = re.search(r"git version (\d+)\.(\d+)", result.stdout)
    if not match:
        raise GitVersionError(f"could not parse git version: {result.stdout!r}")
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) < min_version:
        raise GitVersionError(
            f"git {major}.{minor} found, but >= {min_version[0]}.{min_version[1]} required "
            f"(--initial-branch flag unavailable)"
        )
    return major, minor


def setup_fixture(name: str, seed: Path, working_root: Path) -> tuple[Path, Path]:
    """Copy seed to working_root/name, init git, set up loopback origin.

    Returns (work_dir, origin_dir).

    work_dir = working_root/name with a fresh git repo on branch 'main'.
    origin_dir = working_root/origin_<name>.git (bare clone, set as origin).
    """
    work_dir = working_root / name
    origin_dir = working_root / f"origin_{name}.git"

    # Copy seed to work_dir
    shutil.copytree(seed, work_dir)

    # Initialize git with explicit main branch
    cmds = [
        "git init --initial-branch=main -q",
        "git config user.name 'journey harness'",
        "git config user.email 'harness@journey.local'",
        "git add -A",
        "git commit -q -m 'seed'",
    ]
    for cmd in cmds:
        r = run_bash(cmd, cwd=work_dir)
        if r.returncode != 0:
            raise RuntimeError(f"fixture setup failed at {cmd!r}: {r.stderr}")

    # Create bare loopback origin
    r = run_bash(f"git clone --bare . {origin_dir}", cwd=work_dir)
    if r.returncode != 0:
        raise RuntimeError(f"bare clone failed: {r.stderr}")

    # Wire origin
    r = run_bash(f"git remote add origin {origin_dir}", cwd=work_dir)
    if r.returncode != 0:
        raise RuntimeError(f"remote add failed: {r.stderr}")
    r = run_bash("git fetch -q origin", cwd=work_dir)
    if r.returncode != 0:
        raise RuntimeError(f"git fetch failed: {r.stderr}")
    r = run_bash("git branch --set-upstream-to=origin/main main", cwd=work_dir)
    if r.returncode != 0:
        raise RuntimeError(f"upstream setup failed: {r.stderr}")

    return work_dir, origin_dir
