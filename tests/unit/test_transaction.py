"""Unit tests for chameleon_mcp.bootstrap.transaction."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chameleon_mcp.bootstrap.transaction import (
    COMMITTED_SENTINEL,
    ProfileCommitError,
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


def test_cleanup_keeps_committed_dir_while_writer_alive(tmp_path: Path):
    """A COMMITTED txn whose writer is still alive is mid-swap; leave it."""
    import os as _os

    tmp_root = tmp_path / ".chameleon.tmp"
    tmp_root.mkdir()
    committed = tmp_root / f"{_os.getpid()}-abc12345-1700000000"
    committed.mkdir()
    (committed / COMMITTED_SENTINEL).write_text("done")

    cleaned = cleanup_orphan_tmp_dirs(tmp_path)
    assert cleaned == 0
    assert committed.exists()


def test_cleanup_sweeps_committed_dir_once_writer_is_dead(tmp_path: Path):
    """qa25 P3 — a COMMITTED txn that was never swapped in is permanent
    debris once its writer is gone: only that process could finish the
    rename, so the sweep must reclaim it instead of leaking it forever."""
    tmp_root = tmp_path / ".chameleon.tmp"
    tmp_root.mkdir()
    committed = tmp_root / "99999999-abc12345-1700000000"
    committed.mkdir()
    (committed / COMMITTED_SENTINEL).write_text("done")

    cleaned = cleanup_orphan_tmp_dirs(tmp_path)
    assert cleaned == 1
    assert not committed.exists()


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


def test_stray_backup_not_swept_while_rename_lock_held(tmp_path: Path):
    """A stray backup beside an intact live profile is left alone while locked.

    With the live profile present, the backup could belong to a commit mid-swap,
    so a lock holder must not sweep it. Once the lock is free, it is swept.
    """
    import fcntl

    target = tmp_path / ".chameleon"
    target.mkdir()
    (target / COMMITTED_SENTINEL).write_text("live")

    backup = tmp_path / ".chameleon.backup-123-abcdef12-1700000000"
    backup.mkdir()
    (backup / COMMITTED_SENTINEL).write_text("old")

    fd = os.open(str(tmp_path), os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        # Short timeout so the test does not block the full recovery window.
        import chameleon_mcp.bootstrap.transaction as txn_mod

        orig = txn_mod.RECOVERY_LOCK_TIMEOUT_SECONDS
        txn_mod.RECOVERY_LOCK_TIMEOUT_SECONDS = 0.2
        try:
            handled = cleanup_orphan_tmp_dirs(tmp_path)
        finally:
            txn_mod.RECOVERY_LOCK_TIMEOUT_SECONDS = orig
        # Target intact, so the guarded restore is a no-op and the stray backup
        # is preserved (it may belong to the in-flight commit holding the lock).
        assert handled == 0
        assert backup.exists()
        assert is_committed(target)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    handled = cleanup_orphan_tmp_dirs(tmp_path)
    assert handled == 1
    assert not backup.exists()
    assert is_committed(target)


def test_recovery_restores_backup_even_when_rename_lock_held(tmp_path: Path):
    """A stranded profile is rescued even if a concurrent holder keeps the lock.

    This is the audit repro: a crashed commit left a committed backup with no
    live profile, and a concurrent refresh holds the rename lock. Recovery must
    not be silently skipped, or the repo is left with no profile at all.
    """
    import fcntl

    backup = tmp_path / ".chameleon.backup-123-abcdef12-1700000000"
    backup.mkdir()
    (backup / "profile.json").write_text('{"v": 1}')
    (backup / COMMITTED_SENTINEL).write_text("old")
    assert not (tmp_path / ".chameleon").exists()

    fd = os.open(str(tmp_path), os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        import chameleon_mcp.bootstrap.transaction as txn_mod

        orig = txn_mod.RECOVERY_LOCK_TIMEOUT_SECONDS
        txn_mod.RECOVERY_LOCK_TIMEOUT_SECONDS = 0.2
        try:
            handled = cleanup_orphan_tmp_dirs(tmp_path)
        finally:
            txn_mod.RECOVERY_LOCK_TIMEOUT_SECONDS = orig
        assert handled == 1
        target = tmp_path / ".chameleon"
        assert target.is_dir()
        assert is_committed(target)
        assert (target / "profile.json").read_text() == '{"v": 1}'
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_recovery_blocks_then_recovers_when_lock_freed(tmp_path: Path):
    """When the holder releases within the window, recovery acquires and restores."""
    import subprocess
    import sys

    backup = tmp_path / ".chameleon.backup-123-abcdef12-1700000000"
    backup.mkdir()
    (backup / "profile.json").write_text('{"v": 2}')
    (backup / COMMITTED_SENTINEL).write_text("old")

    holder_src = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
        "time.sleep(0.5)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_src, str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "locked"
        # Default recovery window (10s) easily outlasts the 0.5s hold, so the
        # blocking acquire succeeds once the holder exits, then recovery runs.
        handled = cleanup_orphan_tmp_dirs(tmp_path)
        assert handled == 1
        target = tmp_path / ".chameleon"
        assert is_committed(target)
        assert (target / "profile.json").read_text() == '{"v": 2}'
    finally:
        proc.terminate()
        proc.wait(timeout=5)


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


def test_commit_on_readonly_parent_raises_typed_error(tmp_path: Path):
    """A read-only repo root surfaces the typed commit error, not a bare OSError.

    Callers translate ProfileCommitError into a clean failed envelope. A raw
    PermissionError from the tmp-dir mkdir would escape that channel and break
    the fail-open contract of bootstrap/refresh.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / ".chameleon"

    os.chmod(repo, 0o500)
    try:
        with pytest.raises(ProfileCommitError):
            with atomic_profile_commit(target) as txn:
                (txn / "profile.json").write_text("{}")
    finally:
        os.chmod(repo, 0o700)


def test_profile_commit_error_is_not_bare_oserror_for_callers(tmp_path: Path):
    """The typed error is distinct from a bare PermissionError/OSError.

    The orchestrator catches ProfileCommitError explicitly (mirroring the
    TooManyFilesError channel); a bare PermissionError would not be caught
    there and would propagate as a traceback.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / ".chameleon"

    os.chmod(repo, 0o500)
    try:
        raised: Exception | None = None
        try:
            with atomic_profile_commit(target) as txn:
                (txn / "profile.json").write_text("{}")
        except Exception as exc:  # noqa: BLE001
            raised = exc
    finally:
        os.chmod(repo, 0o700)

    assert isinstance(raised, ProfileCommitError)
    assert type(raised) is not PermissionError


def test_recover_legacy_double_dot_tmp(tmp_path: Path):
    """Orphans from the pre-1.2.1 double-dot tmp naming are swept too."""
    legacy_root = tmp_path / "..chameleon.tmp"
    legacy_root.mkdir()
    orphan = legacy_root / "99999999-abc12345-1700000000"
    orphan.mkdir()

    handled = cleanup_orphan_tmp_dirs(tmp_path)
    assert handled == 1
    assert not orphan.exists()


# --------------------------------------------------------------------------
# qa25 P2 — an unresolved git merge leaves conflict markers inside the
# COMMITTED sentinel (it is tracked in committed-profile repos); a marker-laden
# sentinel means the profile state is indeterminate and must read as
# uncommitted, never half-work.


class TestConflictMarkedSentinel:
    def test_marker_laden_sentinel_reads_uncommitted(self, tmp_path: Path):
        pd = tmp_path / ".chameleon"
        pd.mkdir()
        (pd / "COMMITTED").write_text(
            "<<<<<<< HEAD\ncommitted-at=1.0\npid=1\n=======\n"
            "committed-at=2.0\npid=2\n>>>>>>> feature\n",
            encoding="utf-8",
        )
        assert is_committed(pd) is False

    def test_healthy_sentinel_reads_committed(self, tmp_path: Path):
        pd = tmp_path / ".chameleon"
        pd.mkdir()
        (pd / "COMMITTED").write_text("committed-at=1.0\npid=1\n", encoding="utf-8")
        assert is_committed(pd) is True

    def test_missing_sentinel_reads_uncommitted(self, tmp_path: Path):
        pd = tmp_path / ".chameleon"
        pd.mkdir()
        assert is_committed(pd) is False
