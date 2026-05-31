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


def test_exception_cleans_up_txn_dir(tmp_path: Path):
    target = tmp_path / ".chameleon"

    with pytest.raises(ValueError, match="boom"):
        with atomic_profile_commit(target) as txn:
            (txn / "profile.json").write_text("partial")
            raise ValueError("boom")

    tmp_root = tmp_path / ".chameleon.tmp"
    if tmp_root.exists():
        assert list(tmp_root.iterdir()) == []
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


def test_empty_txn_dir_raises(tmp_path: Path):
    target = tmp_path / ".chameleon"

    with pytest.raises(RuntimeError, match="no artifacts written"):
        with atomic_profile_commit(target) as _txn:
            pass


def test_sibling_files_preserved_across_commit(tmp_path: Path):
    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / COMMITTED_SENTINEL).write_text("old")
    (target / "profile.json").write_text('{"old": true}')
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


def test_pid_alive_current_process():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_dead_pid():
    assert _pid_alive(99999999) is False


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


def test_commit_uses_single_dot_tmp_root(tmp_path: Path):
    """The transaction tmp root is a single-dot sibling cleanup can find."""
    target = tmp_path / ".chameleon"
    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text("{}")
        siblings = {p.name for p in tmp_path.iterdir()}
        assert ".chameleon.tmp" in siblings
        assert "..chameleon.tmp" not in siblings


def test_recover_interrupted_refresh_restores_backup(tmp_path: Path):
    """A crash after moving the live profile aside restores it from backup."""
    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / "profile.json").write_text('{"v": 1}')
    (target / COMMITTED_SENTINEL).write_text("done")

    backup = tmp_path / ".chameleon.backup-123-deadbeef-1700000000"
    os.rename(target, backup)
    assert not target.exists()

    handled = cleanup_orphan_tmp_dirs(tmp_path)
    assert handled == 1
    assert target.is_dir()
    assert is_committed(target)
    assert (target / "profile.json").read_text() == '{"v": 1}'


def test_stray_backup_removed_when_target_intact(tmp_path: Path):
    """A post-swap backup is discarded when the live profile is intact."""
    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / COMMITTED_SENTINEL).write_text("live")

    backup = tmp_path / ".chameleon.backup-999-cafe1234-1700000000"
    backup.mkdir()
    (backup / COMMITTED_SENTINEL).write_text("old")

    handled = cleanup_orphan_tmp_dirs(tmp_path)
    assert handled == 1
    assert not backup.exists()
    assert is_committed(target)


def test_recovery_skips_while_rename_lock_held(tmp_path: Path):
    """A backup left by a live commit (rename lock held) must not be touched."""
    import fcntl

    backup = tmp_path / ".chameleon.backup-123-abcdef12-1700000000"
    backup.mkdir()
    (backup / COMMITTED_SENTINEL).write_text("old")

    # The rename lock is the exclusive flock on the parent directory fd.
    fd = os.open(str(tmp_path), os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        handled = cleanup_orphan_tmp_dirs(tmp_path)
        assert handled == 0
        assert backup.exists()
        assert not (tmp_path / ".chameleon").exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    handled = cleanup_orphan_tmp_dirs(tmp_path)
    assert handled == 1
    assert (tmp_path / ".chameleon").is_dir()


def test_cleanup_sweeps_stray_rename_locks(tmp_path: Path):
    """Legacy *.rename.lock files (pre dir-fd-flock) are swept as debris."""
    single = tmp_path / ".chameleon.rename.lock"
    legacy = tmp_path / "..chameleon.rename.lock"
    single.write_text("")
    legacy.write_text("")

    handled = cleanup_orphan_tmp_dirs(tmp_path)
    assert handled >= 2
    assert not single.exists()
    assert not legacy.exists()


def test_recover_legacy_double_dot_tmp(tmp_path: Path):
    """Orphans from the pre-1.2.1 double-dot tmp naming are swept too."""
    legacy_root = tmp_path / "..chameleon.tmp"
    legacy_root.mkdir()
    orphan = legacy_root / "99999999-abc12345-1700000000"
    orphan.mkdir()

    handled = cleanup_orphan_tmp_dirs(tmp_path)
    assert handled == 1
    assert not orphan.exists()
