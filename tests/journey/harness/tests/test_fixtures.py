"""Unit tests for fixture setup + loopback origin."""

from __future__ import annotations

from pathlib import Path

from tests.journey.harness.bash import run_bash
from tests.journey.harness.fixtures import (
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

    assert (work_dir / "hello.txt").read_text() == "hi\n"
    result = run_bash("git branch --show-current", cwd=work_dir)
    assert result.stdout.strip() == "main"
    result = run_bash("git show origin/main:hello.txt", cwd=work_dir)
    assert result.stdout == "hi\n"


def test_setup_fixture_origin_is_bare(tmp_path: Path) -> None:
    """The loopback origin is a bare repo."""
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "file.txt").write_text("x\n")

    work_dir, origin_dir = setup_fixture("myfix", seed, tmp_path / "working")

    assert origin_dir.name.endswith(".git")
    assert not (origin_dir / "file.txt").exists()
    assert (origin_dir / "HEAD").exists()


def test_setup_fixture_survives_spaces_in_the_working_root(tmp_path):
    """The checkout path is the caller's, not ours, and may contain spaces.

    An unquoted interpolation made `git clone --bare . <dest>` read one
    destination as several arguments, so every eval and journey run died in
    fixture prep with "Too many arguments" the moment the repo lived under a
    directory like "Chameleon + Superpowers".
    """
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "a.ts").write_text("export const a = 1;\n")

    working_root = tmp_path / "work root with spaces"
    working_root.mkdir()

    work_dir, origin_dir = setup_fixture("fx", seed, working_root)

    assert (work_dir / "src" / "a.ts").is_file()
    assert origin_dir.is_dir()
    # the loopback origin must really be wired, not just created
    assert "origin" in run_bash("git remote", cwd=work_dir).stdout
