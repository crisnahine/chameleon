"""Unit tests for chameleon_mcp.bootstrap.transaction."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from chameleon_mcp.bootstrap.transaction import (
    COMMITTED_SENTINEL,
    _pid_alive,
    _txn_dir_pid,
    atomic_profile_commit,
    cleanup_orphan_tmp_dirs,
    is_committed,
)


# ---------------------------------------------------------------------------
# is_committed
# ---------------------------------------------------------------------------


def test_is_committed_true_when_sentinel_exists(tmp_path: Path):
    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / COMMITTED_SENTINEL).write_text("committed-at=1\npid=1\n")
    assert is_committed(target) is True


def test_is_committed_false_when_sentinel_missing(tmp_path: Path):
    target = tmp_path / ".chameleon"
    target.mkdir()
    assert is_committed(target) is False


def test_is_committed_false_when_dir_missing(tmp_path: Path):
    assert is_committed(tmp_path / "no-such-dir") is False


# ---------------------------------------------------------------------------
# atomic_profile_commit — happy path
# ---------------------------------------------------------------------------


def test_commit_replaces_target_dir(tmp_path: Path):
    target = tmp_path / ".chameleon"

    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text('{"v": 1}')

    assert target.is_dir()
    assert (target / "profile.json").read_text() == '{"v": 1}'
    assert is_committed(target)


def test_commit_overwrites_existing_target(tmp_path: Path):
    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / "profile.json").write_text('{"old": true}')
    (target / COMMITTED_SENTINEL).write_text("old")

    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text('{"new": true}')

    assert (target / "profile.json").read_text() == '{"new": true}'
    assert is_committed(target)


# ---------------------------------------------------------------------------
# atomic_profile_commit — exception during context
# ---------------------------------------------------------------------------


def test_exception_cleans_up_txn_dir(tmp_path: Path):
    target = tmp_path / ".chameleon"

    with pytest.raises(ValueError, match="boom"):
        with atomic_profile_commit(target) as txn:
            (txn / "profile.json").write_text("partial")
            raise ValueError("boom")

    # txn_dir should be gone
    tmp_root = tmp_path / ".chameleon.tmp"
    if tmp_root.exists():
        assert list(tmp_root.iterdir()) == []
    # target_dir should not have been created
    assert not target.exists()


def test_exception_leaves_existing_target_untouched(tmp_path: Path):
    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / "profile.json").write_text('{"original": true}')
    (target / COMMITTED_SENTINEL).write_text("original")

    with pytest.raises(RuntimeError, match="kaboom"):
        with atomic_profile_commit(target) as txn:
            (txn / "profile.json").write_text("bad")
            raise RuntimeError("kaboom")

    assert (target / "profile.json").read_text() == '{"original": true}'
    assert is_committed(target)


# ---------------------------------------------------------------------------
# atomic_profile_commit — empty txn_dir
# ---------------------------------------------------------------------------


def test_empty_txn_dir_raises(tmp_path: Path):
    target = tmp_path / ".chameleon"

    with pytest.raises(RuntimeError, match="no artifacts written"):
        with atomic_profile_commit(target) as _txn:
            pass  # write nothing


# ---------------------------------------------------------------------------
# atomic_profile_commit — sibling preservation
# ---------------------------------------------------------------------------


def test_sibling_files_preserved_across_commit(tmp_path: Path):
    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / COMMITTED_SENTINEL).write_text("old")
    (target / "profile.json").write_text('{"old": true}')
    # User-authored siblings that should survive
    (target / ".skip").write_text("opted-out")
    (target / ".gitignore").write_text("*.log")

    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text('{"new": true}')

    assert (target / "profile.json").read_text() == '{"new": true}'
    assert (target / ".skip").read_text() == "opted-out"
    assert (target / ".gitignore").read_text() == "*.log"
    assert is_committed(target)


def test_sibling_subdir_preserved(tmp_path: Path):
    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / COMMITTED_SENTINEL).write_text("old")
    (target / "profile.json").write_text("{}")
    notes = target / "team-notes"
    notes.mkdir()
    (notes / "readme.txt").write_text("hello")

    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text('{"refreshed": true}')

    assert (target / "team-notes" / "readme.txt").read_text() == "hello"


def test_protocol_file_sibling_not_copied_over_txn(tmp_path: Path):
    """Protocol files written by the txn always win, even if the old
    target_dir had a stale copy."""
    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / COMMITTED_SENTINEL).write_text("old")
    (target / "profile.json").write_text('{"stale": true}')

    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text('{"fresh": true}')

    assert (target / "profile.json").read_text() == '{"fresh": true}'


# ---------------------------------------------------------------------------
# _txn_dir_pid
# ---------------------------------------------------------------------------


def test_txn_dir_pid_parses_pid(tmp_path: Path):
    d = tmp_path / "12345-abcdef01-1700000000"
    d.mkdir()
    assert _txn_dir_pid(d) == 12345


def test_txn_dir_pid_returns_none_for_legacy(tmp_path: Path):
    d = tmp_path / "abcdef01-1700000000"
    d.mkdir()
    assert _txn_dir_pid(d) is None


def test_txn_dir_pid_returns_none_for_garbage(tmp_path: Path):
    d = tmp_path / "not-a-number"
    d.mkdir()
    assert _txn_dir_pid(d) is None


# ---------------------------------------------------------------------------
# _pid_alive
# ---------------------------------------------------------------------------


def test_pid_alive_current_process():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_dead_pid():
    # 99999999 is extremely unlikely to be a real PID
    assert _pid_alive(99999999) is False


# ---------------------------------------------------------------------------
# cleanup_orphan_tmp_dirs
# ---------------------------------------------------------------------------


def test_cleanup_removes_dead_pid_dirs(tmp_path: Path):
    tmp_root = tmp_path / ".chameleon.tmp"
    tmp_root.mkdir()
    dead = tmp_root / "99999999-abc12345-1700000000"
    dead.mkdir()
    (dead / "profile.json").write_text("{}")

    cleaned = cleanup_orphan_tmp_dirs(tmp_path)
    assert cleaned == 1
    assert not dead.exists()


def test_cleanup_preserves_alive_pid_dirs(tmp_path: Path):
    tmp_root = tmp_path / ".chameleon.tmp"
    tmp_root.mkdir()
    alive = tmp_root / f"{os.getpid()}-abc12345-1700000000"
    alive.mkdir()
    (alive / "profile.json").write_text("{}")

    cleaned = cleanup_orphan_tmp_dirs(tmp_path)
    assert cleaned == 0
    assert alive.exists()


def test_cleanup_removes_committed_dirs_not(tmp_path: Path):
    """Dirs with COMMITTED sentinel are left alone (they're valid, just
    haven't been renamed yet)."""
    tmp_root = tmp_path / ".chameleon.tmp"
    tmp_root.mkdir()
    committed = tmp_root / "99999999-abc12345-1700000000"
    committed.mkdir()
    (committed / COMMITTED_SENTINEL).write_text("done")

    cleaned = cleanup_orphan_tmp_dirs(tmp_path)
    assert cleaned == 0
    assert committed.exists()


def test_cleanup_removes_legacy_no_pid_dirs(tmp_path: Path):
    """Legacy dirs without a PID prefix are cleaned unconditionally."""
    tmp_root = tmp_path / ".chameleon.tmp"
    tmp_root.mkdir()
    legacy = tmp_root / "abc12345-1700000000"
    legacy.mkdir()

    cleaned = cleanup_orphan_tmp_dirs(tmp_path)
    assert cleaned == 1
    assert not legacy.exists()


def test_cleanup_noop_when_no_tmp_root(tmp_path: Path):
    cleaned = cleanup_orphan_tmp_dirs(tmp_path)
    assert cleaned == 0
