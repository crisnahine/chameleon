"""OS-level advisory locks for chameleon operations.

Used by `/chameleon-refresh` to prevent concurrent invocations corrupting
shared state. Per docs/architecture.md "Atomicity & Crash Safety" → "OS-level locks".

POSIX `flock(2)` semantics:
- LOCK_EX | LOCK_NB: exclusive non-blocking; returns immediately if held
- Lock auto-releases when file descriptor is closed
- Stale lock detection: check PID in lock file is still alive
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

    def __init__(
        self, lock_path: Path, holder_pid: int | None, holder_started_at: float | None
    ) -> None:
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        self.holder_started_at = holder_started_at
        super().__init__(f"lock {lock_path} held by PID {holder_pid} (started {holder_started_at})")


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
    """Check if a PID is still running (POSIX). EPERM means alive but different user."""
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno != errno.ESRCH


@contextmanager
def acquire_advisory_lock(
    lock_path: Path,
    *,
    stale_after_seconds: int = 3600,
    blocking_timeout: float | None = None,
):
    """Context manager: acquire an exclusive flock on lock_path.

    Args:
        lock_path: path to the lock file (will be created if missing)
        stale_after_seconds: how old a lock can be before we forcibly break it
                             (default 1 hour; matches refresh_repo expected ceiling)
        blocking_timeout: when set, block-and-retry for up to this many seconds
                          to serialize against a live holder instead of failing
                          immediately. The non-blocking probe still runs first so
                          a stale lock is broken without waiting. None keeps the
                          original non-blocking behavior.

    Yields:
        None — caller has exclusive access while the context manager is active.

    Raises:
        LockHeldError: if another live process holds the lock and it could not be
                       acquired (immediately, or within blocking_timeout).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            pid, started_at = _read_lock_metadata(lock_path)
            now = time.time()
            stale = (
                pid is not None
                and started_at is not None
                and (not _is_pid_alive(pid) or (now - started_at) > stale_after_seconds)
            )
            if stale:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as e2:
                    if e2.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    # The stale holder was replaced between the staleness check
                    # and this retry. Re-read so the error names the current
                    # holder, not the cached (now-dead) PID, then either wait it
                    # out or report the live holder.
                    if not _acquire_blocking(fd, lock_path, blocking_timeout):
                        cur_pid, cur_started = _read_lock_metadata(lock_path)
                        raise LockHeldError(lock_path, cur_pid, cur_started) from e2
            else:
                if not _acquire_blocking(fd, lock_path, blocking_timeout):
                    raise LockHeldError(lock_path, pid, started_at) from e

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
        os.fsync(fd)

        try:
            yield
        finally:
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


def _acquire_blocking(fd: int, lock_path: Path, blocking_timeout: float | None) -> bool:
    """Block-and-retry a non-blocking flock until acquired or the timeout lapses.

    Returns True once the lock is held, False if blocking_timeout is None (caller
    wants fail-fast) or the deadline passed. Uses LOCK_NB polling rather than a
    plain blocking flock so a holder that dies mid-wait cannot wedge the caller
    indefinitely and so the deadline is honored on every platform.
    """
    if blocking_timeout is None:
        return False
    deadline = time.time() + blocking_timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            if time.time() >= deadline:
                return False
            time.sleep(0.01)
