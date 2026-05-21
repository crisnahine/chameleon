"""Unit tests for fixture setup + loopback origin."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.journey.harness.bash import run_bash
from tests.journey.harness.fixtures import (
    GitVersionError,
    check_git_version,
    setup_fixture,
)


def test_check_git_version_accepts_recent() -> None:
    """check_git_version returns the parsed version tuple on >= 2.28."""
    major, minor = check_git_version(min_version=(2, 28))
    assert major >= 2
    if major == 2:
        assert minor >= 28


def test_setup_fixture_copies_and_inits(tmp_path: Path) -> None:
    """setup_fixture copies seed, runs git init, sets up loopback origin."""
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "hello.txt").write_text("hi\n")

    working_root = tmp_path / "working"
    working_root.mkdir()

    work_dir, origin_dir = setup_fixture("myfix", seed, working_root)

    # Working copy has the seed content
    assert (work_dir / "hello.txt").read_text() == "hi\n"
    # Working copy is a git repo on branch 'main'
    result = run_bash("git branch --show-current", cwd=work_dir)
    assert result.stdout.strip() == "main"
    # origin/main is reachable
    result = run_bash("git show origin/main:hello.txt", cwd=work_dir)
    assert result.stdout == "hi\n"


def test_setup_fixture_origin_is_bare(tmp_path: Path) -> None:
    """The loopback origin is a bare repo."""
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "file.txt").write_text("x\n")

    work_dir, origin_dir = setup_fixture("myfix", seed, tmp_path / "working")

    assert origin_dir.name.endswith(".git")
    # Bare repos have no working tree
    assert not (origin_dir / "file.txt").exists()
    # But have HEAD
    assert (origin_dir / "HEAD").exists()
