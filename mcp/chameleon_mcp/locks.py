"""OS-level advisory locks for chameleon operations.

Used by `/chameleon-refresh` to prevent concurrent invocations corrupting
shared state. Per docs/architecture.md "Atomicity & Crash Safety" → "OS-level locks".

POSIX `flock(2)` semantics:
- LOCK_EX | LOCK_NB: exclusive non-blocking; returns immediately if held
- Lock auto-releases when file descriptor is closed
- Stale lock detection: check PID in lock file is still alive

This module is the single cross-platform locking layer. On POSIX it uses
`fcntl.flock`. On Windows (no `fcntl`) it falls back to `msvcrt.locking` over a
fixed one-byte region, normalizing a held lock to `BlockingIOError(EAGAIN)` so
every caller's non-blocking handler behaves identically on both platforms. The
other modules that need a lock import the helpers here rather than touching
`fcntl` directly.
"""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX has no msvcrt
    msvcrt = None  # type: ignore[assignment]

# msvcrt.locking() locks a byte range from the current file position; one byte
# at offset 0 is enough for whole-file mutual exclusion as long as every process
# locks the same region.
_MSVCRT_REGION = 1

# msvcrt.locking() signals a blocked LK_LOCK with EDEADLOCK on Windows. That name
# only exists in `errno` on Windows, so resolve it portably (falling back to the
# POSIX EDEADLK alias / its numeric value) — the constant must be evaluatable on
# every platform even though the branch using it only runs under msvcrt.
_EDEADLOCK = getattr(errno, "EDEADLOCK", getattr(errno, "EDEADLK", 36))


def portable_flock(fd: int, *, nonblocking: bool) -> None:
    """Acquire an exclusive lock on ``fd``.

    POSIX: ``fcntl.flock``. Windows: ``msvcrt.locking`` over a fixed one-byte
    region. When ``nonblocking`` and the lock is already held, raises
    ``BlockingIOError`` with ``errno.EAGAIN`` on both platforms so existing
    ``EAGAIN``/``EWOULDBLOCK`` handlers fire uniformly. Other failures raise the
    underlying ``OSError``. If neither primitive exists the call is a best-effort
    no-op (single-process use stays safe).
    """
    if fcntl is not None:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(fd, flags)
        return
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        if nonblocking:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, _MSVCRT_REGION)
            except OSError as e:
                # msvcrt raises EACCES/EDEADLOCK when the region is held; present
                # it as EAGAIN so callers don't branch on platform-specific errno.
                raise BlockingIOError(errno.EAGAIN, "lock held") from e
            return
        # Blocking: LK_LOCK blocks ~10s per attempt then raises EDEADLOCK; retry
        # so the wait is honored without a hard cap that a long holder would hit.
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, _MSVCRT_REGION)
                return
            except OSError as e:
                if e.errno != _EDEADLOCK:
                    raise
                time.sleep(0.05)


def portable_funlock(fd: int) -> None:
    """Release a lock taken with :func:`portable_flock`. Failures are swallowed."""
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    if msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _MSVCRT_REGION)
        except OSError:
            pass


def open_dir_lock_fd(lock_dir: Path) -> int:
    """Open an fd to contend on for a directory-scoped lock.

    POSIX: the directory's own fd — a stable inode that is never created or
    unlinked, so contenders share it without leaving a lock file. Windows cannot
    open a directory as a lockable fd, so a sidecar ``<name>.winlock`` file is
    created inside the directory and contended on instead. The caller closes the
    returned fd to release.
    """
    if fcntl is not None:
        return os.open(str(lock_dir), os.O_RDONLY)
    sidecar = lock_dir / ".chameleon.winlock"
    return os.open(str(sidecar), os.O_RDWR | os.O_CREAT, 0o600)


def pid_alive(pid: int) -> bool:
    """True iff ``pid`` is a running process, without ever signalling it.

    POSIX: ``os.kill(pid, 0)`` (EPERM counts as alive — different user). Windows:
    ``os.kill`` with a non-CTRL signal calls ``TerminateProcess``, so it must
    never be used for a liveness probe; query via ``OpenProcess`` instead and, if
    that is unavailable, assume alive so a live holder's lock is never broken (a
    truly dead one still expires via the timestamp staleness ceiling).
    """
    if fcntl is not None:
        try:
            os.kill(pid, 0)
            return True
        except OSError as e:
            return e.errno != errno.ESRCH
    try:
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - any failure degrades to "assume alive"
        return True


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
    """Whether a PID is still running. Delegates to the cross-platform probe."""
    return pid_alive(pid)


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
            portable_flock(fd, nonblocking=True)
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
                    portable_flock(fd, nonblocking=True)
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
        portable_funlock(fd)
        os.close(fd)


def portable_flock_deadline(fd: int, timeout_seconds: float) -> bool:
    """Exclusive lock on ``fd`` with a deadline: poll LOCK_NB until acquired.

    Returns True once the lock is held, False when the deadline lapses. LOCK_NB
    polling rather than a plain blocking flock keeps the wait bounded on every
    platform, so a caller can fail open instead of wedging an entire session
    behind one long-lived holder (a daemon mid-extraction, a stuck sibling).
    Non-EAGAIN lock errors propagate.
    """
    deadline = time.time() + timeout_seconds
    while True:
        try:
            portable_flock(fd, nonblocking=True)
            return True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            if time.time() >= deadline:
                return False
            time.sleep(0.01)


def _acquire_blocking(fd: int, lock_path: Path, blocking_timeout: float | None) -> bool:
    """Block-and-retry a non-blocking flock until acquired or the timeout lapses.

    Returns True once the lock is held, False if blocking_timeout is None (caller
    wants fail-fast) or the deadline passed.
    """
    if blocking_timeout is None:
        return False
    return portable_flock_deadline(fd, blocking_timeout)
