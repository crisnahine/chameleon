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
import fcntl
import os
import random
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

COMMITTED_SENTINEL = "COMMITTED"

_PROTOCOL_FILES = frozenset({
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
})


def _open_rename_lock_fd(lock_dir: Path) -> int:
    return os.open(str(lock_dir), os.O_RDONLY)


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
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                try:
                    with open(artifact, "rb") as af:
                        os.fsync(af.fileno())
                except OSError:
                    pass

        sentinel = txn_dir / COMMITTED_SENTINEL
        sentinel.write_text(f"committed-at={time.time()}\npid={os.getpid()}\n")
        with open(sentinel, "rb") as f:
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
            try:
                fcntl.flock(rename_lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
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
    """POSIX liveness check. Permission errors count as 'alive' (conservative)."""
    import errno
    import os
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno != errno.ESRCH


def cleanup_orphan_tmp_dirs(target_parent: Path, profile_dir_name: str = "chameleon") -> int:
    """Sweep orphaned transaction dirs and recover interrupted commits.

    Called before every bootstrap/refresh. Returns the count of directories
    cleaned or recovered.

    Recovery: a hard crash between "move the live profile aside" and "move
    the new transaction into place" leaves the committed profile only in a
    ``.{name}.backup-<txn>`` dir. When the live profile is missing and a
    committed backup exists, the backup is restored; otherwise the stray
    backup (post-swap debris, or an uncommitted partial) is removed. Backup
    handling runs only while holding the same rename lock the commit uses,
    acquired non-blocking: if a live writer holds it, the backup is its
    in-flight swap (transient, not an orphan) and is left untouched.

    Orphan sweep: ``.{name}.tmp/<txn-id>/`` dirs that lack a COMMITTED
    sentinel are removed, unless the PID prefix is still alive (a concurrent
    writer mid-commit). Legacy dirs with no PID prefix are cleaned
    unconditionally. The legacy double-dot tmp/backup names emitted by
    chameleon <= 1.2.0 are swept too.
    """
    target_dir = target_parent / f".{profile_dir_name}"
    handled = 0

    lock_fd: int | None = None
    holds_lock = False
    try:
        lock_fd = _open_rename_lock_fd(target_parent)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        holds_lock = True
    except OSError:
        holds_lock = False

    if holds_lock:
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

        restored = False
        for backup_dir in sorted(backups, key=_mtime, reverse=True):
            if not backup_dir.is_dir():
                continue
            backup_committed = (backup_dir / COMMITTED_SENTINEL).is_file()
            target_committed = (target_dir / COMMITTED_SENTINEL).is_file()
            if (
                not restored
                and backup_committed
                and not target_committed
                and not target_dir.exists()
            ):
                try:
                    os.rename(backup_dir, target_dir)
                    restored = True
                    handled += 1
                    continue
                except OSError:
                    pass
            shutil.rmtree(backup_dir, ignore_errors=True)
            handled += 1

    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
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

    # The commit path flocks the parent-directory fd and creates no lock file,
    # so any *.rename.lock is legacy debris from chameleon <= 1.2.0; sweep it.
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
