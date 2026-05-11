"""Atomic multi-file commit pattern for chameleon profile writes.

Per ARCHITECTURE.md "Atomicity & Crash Safety" → "Multi-file transactional commit":

  1. Write all artifacts into .chameleon/.tmp/<txn-id>/
  2. Verify each artifact (fsync, schema-validate, secret-scan)
  3. Write COMMITTED sentinel file last
  4. Atomic rename: .chameleon/.tmp/<txn-id>/ → .chameleon/

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


def _acquire_rename_lock(lock_path: Path, *, timeout_seconds: float = 30.0) -> int:
    """Block-and-retry until exclusive lock on lock_path is held.

    Used to serialize the txn_dir → target_dir rename across concurrent
    bootstrap processes. Returns the open fd; caller is responsible for
    closing it (which releases the lock).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
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
                raise TimeoutError(f"could not acquire {lock_path} within {timeout_seconds}s")
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

    # Per-PID + uuid temp subdir to prevent collisions between concurrent
    # processes (refresh_repo + bootstrap_repo running simultaneously).
    txn_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}-{int(time.time())}"
    tmp_root = target_dir.parent / f".{target_dir.name}.tmp"
    tmp_root.mkdir(exist_ok=True)
    txn_dir = tmp_root / txn_id
    txn_dir.mkdir()

    try:
        yield txn_dir

        # All writes succeeded. Verify expected artifacts are present.
        # (Real validation in Phase 2; for now, just check for at least one file.)
        if not any(txn_dir.iterdir()):
            raise RuntimeError("atomic_profile_commit: no artifacts written")

        # Write COMMITTED sentinel LAST.
        sentinel = txn_dir / COMMITTED_SENTINEL
        sentinel.write_text(f"committed-at={time.time()}\npid={os.getpid()}\n")
        # fsync the sentinel to ensure it's on disk before the rename
        with open(sentinel, "rb") as f:
            os.fsync(f.fileno())

        # Atomic rename: txn_dir → target_dir, serialized across concurrent
        # processes via an advisory flock on a sibling file. POSIX rename(2)
        # over an existing directory requires the target to be empty on
        # macOS, so we move target_dir aside first and rename our txn into
        # place — the lock prevents two writers from racing the move/rename
        # pair (TOCTOU between target_dir.exists() and os.rename produces
        # ENOTEMPTY when both writers think the target is missing).
        backup_dir = target_dir.parent / f".{target_dir.name}.backup-{txn_id}"
        rename_lock_path = target_dir.parent / f".{target_dir.name}.rename.lock"
        rename_lock_fd = _acquire_rename_lock(rename_lock_path)
        try:
            if target_dir.exists():
                os.rename(target_dir, backup_dir)
            try:
                os.rename(txn_dir, target_dir)
            except OSError:
                if backup_dir.exists():
                    os.rename(backup_dir, target_dir)
                raise
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
        finally:
            try:
                fcntl.flock(rename_lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(rename_lock_fd)
    except Exception:
        # Clean up partial txn dir on any failure
        if txn_dir.exists():
            shutil.rmtree(txn_dir, ignore_errors=True)
        raise


def is_committed(target_dir: Path) -> bool:
    """Return True iff target_dir contains a valid COMMITTED sentinel.

    Loaders use this to refuse incomplete profiles per ARCHITECTURE.md.
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


def cleanup_orphan_tmp_dirs(target_parent: Path, profile_dir_name: str = ".chameleon") -> int:
    """Sweep orphaned .tmp/<txn-id>/ directories that lack COMMITTED sentinels.

    Called on MCP server startup. Returns count of cleaned-up directories.

    Skips dirs whose PID prefix is still alive — that's a concurrent
    chameleon-mcp process mid-bootstrap; clobbering it would race with
    the live transaction. Legacy dirs with no PID prefix are cleaned
    unconditionally (those predate the PID-stamped txn_id format).
    """
    tmp_root = target_parent / f".{profile_dir_name}.tmp"
    if not tmp_root.is_dir():
        return 0
    cleaned = 0
    for txn_dir in tmp_root.iterdir():
        if not txn_dir.is_dir():
            continue
        if (txn_dir / COMMITTED_SENTINEL).exists():
            continue
        pid = _txn_dir_pid(txn_dir)
        if pid is not None and _pid_alive(pid):
            continue  # concurrent writer; do not clobber
        shutil.rmtree(txn_dir, ignore_errors=True)
        cleaned += 1
    return cleaned
