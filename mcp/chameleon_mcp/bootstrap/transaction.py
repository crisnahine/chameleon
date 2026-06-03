"""Atomic multi-file commit pattern for chameleon profile writes.

Per docs/architecture.md "Atomicity & Crash Safety" → "Multi-file transactional commit":

  1. Write all artifacts into .chameleon.tmp/<txn-id>/ (sibling of .chameleon/)
  2. Verify each artifact (fsync, schema-validate, secret-scan)
  3. Write COMMITTED sentinel file last
  4. Atomic rename: .chameleon.tmp/<txn-id>/ → .chameleon/

Loaders refuse to read .chameleon/ if COMMITTED is missing.
Per-PID temp subdir prevents collision when two refresh processes run simultaneously.

Round 4 distributed-systems hardening — addresses one of the 6 BLOCKING items.
"""

from __future__ import annotations

import errno
import os
import random
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from chameleon_mcp.locks import (
    open_dir_lock_fd,
    pid_alive,
    portable_flock,
    portable_funlock,
)

COMMITTED_SENTINEL = "COMMITTED"

# How long recovery blocks for the rename lock before falling back to a guarded
# restore. Long enough to outlast a normal commit's swap, short enough not to
# stall a bootstrap/refresh if a writer wedges the lock.
RECOVERY_LOCK_TIMEOUT_SECONDS = 10.0

_PROTOCOL_FILES = frozenset(
    {
        COMMITTED_SENTINEL,
        "profile.json",
        "archetypes.json",
        "canonicals.json",
        "conventions.json",
        "principles.md",
        "rules.json",
        "idioms.md",
        "profile.summary.md",
        "renames.json",
    }
)


def _open_rename_lock_fd(lock_dir: Path) -> int:
    return open_dir_lock_fd(lock_dir)


def _fsync_dir(path: Path) -> None:
    """fsync a directory fd so a rename/create within it is durable."""
    try:
        dfd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _fsync_file(path: Path) -> None:
    """fsync a file's data to disk, portably.

    Windows ``os.fsync`` maps to ``_commit`` and requires a writable fd, so the
    file is reopened ``O_RDWR`` rather than read-only (a read-only fd raises
    EBADF there). Failures are swallowed: durability is best-effort and must
    never abort the commit.
    """
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _acquire_rename_lock(lock_dir: Path, *, timeout_seconds: float = 30.0) -> int:
    """Block-and-retry until an exclusive flock on ``lock_dir`` is held.

    Serializes the txn_dir → target_dir rename across concurrent processes.
    The lock is taken on the directory's own fd — a stable inode that is
    never created or unlinked — so every process contends on the same inode
    and no stray lock file is left in the repo. Returns the open fd; the
    caller closes it to release.
    """
    fd = _open_rename_lock_fd(lock_dir)
    deadline = time.time() + timeout_seconds
    while True:
        try:
            portable_flock(fd, nonblocking=True)
            return fd
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                os.close(fd)
                raise
            if time.time() >= deadline:
                os.close(fd)
                raise TimeoutError(
                    f"could not acquire rename lock on {lock_dir} within {timeout_seconds}s"
                ) from e
            time.sleep(0.05 + random.random() * 0.05)


@contextmanager
def atomic_profile_commit(target_dir: Path):
    """Context manager for atomic multi-file profile writes.

    Usage:
        with atomic_profile_commit(repo_root / ".chameleon") as txn_dir:
            (txn_dir / "profile.json").write_text(...)
            (txn_dir / "archetypes.json").write_text(...)
            # ... etc; all writes go to txn_dir, never directly to target_dir
        # On exit: COMMITTED sentinel written, txn_dir atomically renamed to target_dir.
        # On exception: txn_dir is removed; target_dir untouched.

    Args:
        target_dir: the .chameleon/ directory to atomically replace.
    """
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    txn_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}-{int(time.time())}"
    tmp_root = target_dir.parent / f"{target_dir.name}.tmp"
    tmp_root.mkdir(exist_ok=True)
    txn_dir = tmp_root / txn_id
    txn_dir.mkdir()

    try:
        yield txn_dir

        if not any(txn_dir.iterdir()):
            raise RuntimeError("atomic_profile_commit: no artifacts written")

        if target_dir.is_dir():
            for sibling in target_dir.iterdir():
                if sibling.name in _PROTOCOL_FILES:
                    continue
                dest = txn_dir / sibling.name
                if dest.exists():
                    continue
                try:
                    if sibling.is_dir():
                        shutil.copytree(sibling, dest, symlinks=True)
                    else:
                        shutil.copy2(sibling, dest, follow_symlinks=False)
                except OSError:
                    continue

        # Durability: fsync every artifact, then write + fsync the COMMITTED
        # sentinel last, then fsync the txn dir, so a power loss can never
        # surface a COMMITTED profile whose data artifacts are truncated.
        for artifact in txn_dir.iterdir():
            if artifact.is_file():
                _fsync_file(artifact)

        sentinel = txn_dir / COMMITTED_SENTINEL
        # Write and fsync through one writable handle: a read-only fd cannot be
        # fsync'd on Windows.
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(f"committed-at={time.time()}\npid={os.getpid()}\n")
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(txn_dir)

        backup_dir = target_dir.parent / f"{target_dir.name}.backup-{txn_id}"
        rename_lock_fd = _acquire_rename_lock(target_dir.parent)
        try:
            if target_dir.exists():
                os.rename(target_dir, backup_dir)
            try:
                os.rename(txn_dir, target_dir)
            except OSError:
                if backup_dir.exists():
                    os.rename(backup_dir, target_dir)
                raise
            # Persist the directory entry swap so the rename survives a crash.
            _fsync_dir(target_dir.parent)
            if backup_dir.exists() or backup_dir.is_symlink():
                if backup_dir.is_symlink():
                    try:
                        backup_dir.unlink()
                    except OSError:
                        pass
                else:
                    shutil.rmtree(backup_dir, ignore_errors=True)
        finally:
            portable_funlock(rename_lock_fd)
            os.close(rename_lock_fd)
            try:
                tmp_root.rmdir()
            except OSError:
                pass
    except Exception:
        if txn_dir.exists():
            shutil.rmtree(txn_dir, ignore_errors=True)
        raise


def is_committed(target_dir: Path) -> bool:
    """Return True iff target_dir contains a valid COMMITTED sentinel.

    Loaders use this to refuse incomplete profiles per docs/architecture.md.
    """
    if not target_dir.is_dir():
        return False
    sentinel = target_dir / COMMITTED_SENTINEL
    return sentinel.is_file()


def _txn_dir_pid(txn_dir: Path) -> int | None:
    """Extract the writer PID from a txn dir name (`<pid>-<uuid8>-<epoch>`).

    Returns None when the name doesn't conform to that pattern (e.g.,
    legacy directories pre-PID-prefix, or hand-created junk).
    """
    head = txn_dir.name.split("-", 1)[0]
    try:
        return int(head)
    except (TypeError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Whether a writer PID is still running. Delegates to the cross-platform probe."""
    return pid_alive(pid)


def _acquire_recovery_lock(
    target_parent: Path, *, timeout_seconds: float | None = None
) -> tuple[int | None, bool]:
    """Block-and-retry for the rename lock used during recovery.

    Returns (fd, holds_lock). The fd is always returned (closed by the caller)
    so its open file description, and any lock on it, lives exactly as long as
    the caller needs. holds_lock is False if the directory could not be opened
    or the lock stayed held for the whole timeout.
    """
    if timeout_seconds is None:
        timeout_seconds = RECOVERY_LOCK_TIMEOUT_SECONDS
    try:
        fd = _open_rename_lock_fd(target_parent)
    except OSError:
        return None, False
    deadline = time.time() + timeout_seconds
    while True:
        try:
            portable_flock(fd, nonblocking=True)
            return fd, True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                return fd, False
            if time.time() >= deadline:
                return fd, False
            time.sleep(0.05 + random.random() * 0.05)


def _list_backups(target_parent: Path, profile_dir_name: str) -> list[Path]:
    """All backup dirs for this profile, newest first by mtime."""
    backups: set[Path] = set()
    for pattern in (
        f".{profile_dir_name}.backup-*",
        f"..{profile_dir_name}.backup-*",
    ):
        backups.update(target_parent.glob(pattern))

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(backups, key=_mtime, reverse=True)


def _recover_backups(target_parent: Path, target_dir: Path, profile_dir_name: str) -> int:
    """Restore the newest committed backup if the live profile is gone, sweep rest.

    Runs only while holding the rename lock. Returns the number of backup dirs
    restored or removed.
    """
    handled = 0
    restored = False
    for backup_dir in _list_backups(target_parent, profile_dir_name):
        if not backup_dir.is_dir():
            continue
        backup_committed = (backup_dir / COMMITTED_SENTINEL).is_file()
        target_committed = (target_dir / COMMITTED_SENTINEL).is_file()
        if not restored and backup_committed and not target_committed and not target_dir.exists():
            try:
                os.rename(backup_dir, target_dir)
                restored = True
                handled += 1
                continue
            except OSError:
                pass
        shutil.rmtree(backup_dir, ignore_errors=True)
        handled += 1
    return handled


def _restore_committed_backup_if_target_missing(
    target_parent: Path, target_dir: Path, profile_dir_name: str
) -> int:
    """Lock-free rescue for the only profile-stranding state.

    Restores the newest committed backup when no live profile exists. Does not
    touch stray backups, since without the lock those could belong to an
    in-flight swap. Returns 1 if a backup was restored, else 0.
    """
    if target_dir.exists():
        return 0
    for backup_dir in _list_backups(target_parent, profile_dir_name):
        if not backup_dir.is_dir():
            continue
        if not (backup_dir / COMMITTED_SENTINEL).is_file():
            continue
        try:
            os.rename(backup_dir, target_dir)
            return 1
        except OSError:
            return 0
    return 0


def cleanup_orphan_tmp_dirs(target_parent: Path, profile_dir_name: str = "chameleon") -> int:
    """Sweep orphaned transaction dirs and recover interrupted commits.

    Called before every bootstrap/refresh. Returns the count of directories
    cleaned or recovered.

    Recovery: a hard crash between "move the live profile aside" and "move
    the new transaction into place" leaves the committed profile only in a
    ``.{name}.backup-<txn>`` dir. When the live profile is missing and a
    committed backup exists, the backup is restored; otherwise the stray
    backup (post-swap debris, or an uncommitted partial) is removed. Backup
    handling takes the same rename lock the commit uses, acquired
    block-and-retry so a concurrent commit is serialized behind rather than
    abandoning a crashed profile. If the lock stays held past the timeout, a
    guarded lock-free restore still rescues the one state that strands a repo
    (committed backup, no live profile); stray-backup sweeping is deferred to a
    later run that does hold the lock.

    Orphan sweep: ``.{name}.tmp/<txn-id>/`` dirs that lack a COMMITTED
    sentinel are removed, unless the PID prefix is still alive (a concurrent
    writer mid-commit). Legacy dirs with no PID prefix are cleaned
    unconditionally. The legacy double-dot tmp/backup names emitted by
    chameleon <= 1.2.0 are swept too.
    """
    target_dir = target_parent / f".{profile_dir_name}"
    handled = 0

    # Block-and-retry for the rename lock instead of probing once and skipping.
    # A concurrent commit holds this lock only across its swap, so blocking
    # serializes recovery behind it rather than abandoning a crashed profile
    # because some unrelated refresh happened to hold the lock at that instant.
    lock_fd, holds_lock = _acquire_recovery_lock(target_parent)
    try:
        if holds_lock:
            handled += _recover_backups(target_parent, target_dir, profile_dir_name)
        else:
            # The lock stayed held past the timeout. Do not sweep stray backups
            # here (that could race a live swap), but still rescue the one state
            # that strands a repo: a committed backup with no live profile. The
            # restore is an atomic rename onto a missing target, safe to run even
            # without the lock because a concurrent committer would itself land a
            # valid committed profile (last-writer-wins, never an empty target).
            handled += _restore_committed_backup_if_target_missing(
                target_parent, target_dir, profile_dir_name
            )
    finally:
        if lock_fd is not None:
            if holds_lock:
                portable_funlock(lock_fd)
            os.close(lock_fd)

    for tmp_root in (
        target_parent / f".{profile_dir_name}.tmp",
        target_parent / f"..{profile_dir_name}.tmp",
    ):
        if not tmp_root.is_dir():
            continue
        for txn_dir in tmp_root.iterdir():
            if not txn_dir.is_dir():
                continue
            if (txn_dir / COMMITTED_SENTINEL).exists():
                continue
            pid = _txn_dir_pid(txn_dir)
            if pid is not None and _pid_alive(pid):
                continue
            shutil.rmtree(txn_dir, ignore_errors=True)
            handled += 1
        try:
            tmp_root.rmdir()
        except OSError:
            pass

    # On POSIX the commit path flocks the parent-directory fd and creates no lock
    # file, so any *.rename.lock is legacy debris from chameleon <= 1.2.0; sweep
    # it. (The Windows sidecar is named .winlock and is left alone here.)
    for lock_name in (
        f".{profile_dir_name}.rename.lock",
        f"..{profile_dir_name}.rename.lock",
    ):
        lock_path = target_parent / lock_name
        if lock_path.is_file():
            try:
                lock_path.unlink()
                handled += 1
            except OSError:
                pass

    return handled
