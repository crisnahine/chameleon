"""OS-level advisory locks for chameleon operations.

Used by `/chameleon-refresh` to prevent concurrent invocations corrupting
shared state. Per ARCHITECTURE.md "Atomicity & Crash Safety" → "OS-level locks".

POSIX `flock(2)` semantics:
- LOCK_EX | LOCK_NB: exclusive non-blocking; returns immediately if held
- Lock auto-releases when file descriptor is closed
- Stale lock detection: check PID in lock file is still alive

Pattern adopted from claude-measure-twice's flock usage.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockHeldError(Exception):
    """Raised when a non-blocking lock acquisition fails."""

    def __init__(self, lock_path: Path, holder_pid: int | None, holder_started_at: float | None) -> None:
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        self.holder_started_at = holder_started_at
        super().__init__(
            f"lock {lock_path} held by PID {holder_pid} (started {holder_started_at})"
        )


def _read_lock_metadata(lock_path: Path) -> tuple[int | None, float | None]:
    """Read PID + start timestamp from lock file. Returns (None, None) on parse failure."""
    try:
        content = lock_path.read_text(errors="ignore").strip()
        parts = content.split()
        if len(parts) >= 2:
            return int(parts[0]), float(parts[1])
    except (OSError, ValueError):
        pass
    return None, None


def _is_pid_alive(pid: int) -> bool:
    """Check if a PID is still running (POSIX). Returns False on permission error too (conservative)."""
    try:
        os.kill(pid, 0)  # signal 0 = check existence without sending
        return True
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        return False  # conservative: treat permission errors as dead


@contextmanager
def acquire_advisory_lock(lock_path: Path, *, stale_after_seconds: int = 3600):
    """Context manager: acquire exclusive non-blocking flock on lock_path.

    Args:
        lock_path: path to the lock file (will be created if missing)
        stale_after_seconds: how old a lock can be before we forcibly break it
                             (default 1 hour; matches refresh_repo expected ceiling)

    Yields:
        None — caller has exclusive access while the context manager is active.

    Raises:
        LockHeldError: if another live process holds the lock.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open lock file (create if missing). Keep fd alive for duration of context.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            # Lock held by someone else. Check if stale.
            pid, started_at = _read_lock_metadata(lock_path)
            now = time.time()
            stale = (
                pid is not None
                and started_at is not None
                and (not _is_pid_alive(pid) or (now - started_at) > stale_after_seconds)
            )
            if stale:
                # Break the lock: acquire it now (the previous holder is gone)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise LockHeldError(lock_path, pid, started_at) from e

        # We have the lock. Write our PID + start time.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
        os.fsync(fd)

        try:
            yield
        finally:
            # Lock auto-releases on close, but write empty content first to
            # signal "clean shutdown" (helps stale-lock detection).
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
