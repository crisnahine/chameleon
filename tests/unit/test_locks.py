"""Unit tests for OS-level advisory locks (chameleon_mcp.locks)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chameleon_mcp import locks
from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

_HOLDER_SRC = """
import fcntl, os, sys, time
lock_path = sys.argv[1]
hold_seconds = float(sys.argv[2])
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, f"{os.getpid()} {time.time()}\\n".encode())
os.fsync(fd)
sys.stdout.write("locked\\n")
sys.stdout.flush()
time.sleep(hold_seconds)
"""


def _spawn_holder(lock_path: Path, hold_seconds: float) -> subprocess.Popen:
    """Start a subprocess that takes the flock, prints 'locked', then sleeps."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SRC, str(lock_path), str(hold_seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    assert line.strip() == "locked", f"holder failed to lock: {line!r}"
    return proc


def test_lock_held_by_live_process_raises(tmp_path: Path):
    lock_path = tmp_path / "x.lock"
    holder = _spawn_holder(lock_path, 5.0)
    try:
        with pytest.raises(LockHeldError) as exc:
            with acquire_advisory_lock(lock_path):
                pass
        assert exc.value.holder_pid == holder.pid
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_lock_acquires_when_free(tmp_path: Path):
    lock_path = tmp_path / "free.lock"
    with acquire_advisory_lock(lock_path):
        pid, started = locks._read_lock_metadata(lock_path)
        assert pid == os.getpid()


def test_blocking_timeout_waits_for_holder_then_acquires(tmp_path: Path):
    """A blocking acquire should wait out a short-lived holder and then succeed."""
    lock_path = tmp_path / "block.lock"
    holder = _spawn_holder(lock_path, 0.4)
    try:
        start = time.time()
        with acquire_advisory_lock(lock_path, blocking_timeout=5.0):
            waited = time.time() - start
        assert waited >= 0.3
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_blocking_timeout_gives_up_and_reports_live_holder(tmp_path: Path):
    """If the holder outlives the blocking window, raise naming the live holder."""
    lock_path = tmp_path / "block-fail.lock"
    holder = _spawn_holder(lock_path, 5.0)
    try:
        with pytest.raises(LockHeldError) as exc:
            with acquire_advisory_lock(lock_path, blocking_timeout=0.3):
                pass
        assert exc.value.holder_pid == holder.pid
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_stale_pid_in_metadata_does_not_leak_into_error(tmp_path: Path, monkeypatch):
    """Stale-holder detection must not name a dead PID once the holder swaps.

    Reproduces the TOCTOU race: the first metadata read names a dead PID, so the
    staleness check fires and the code retries the flock. But between the check
    and the retry a different live process took the lock, so the retry fails and
    the lock metadata now names that live holder. The raised LockHeldError must
    name the current live holder, not the cached dead PID from the first read.
    """
    lock_path = tmp_path / "stale.lock"

    # A live process holds the lock for real; its PID is the honest holder.
    holder = _spawn_holder(lock_path, 5.0)
    live_pid = holder.pid
    dead_pid = 2_000_000_000  # far outside any real PID range

    try:
        real_read = locks._read_lock_metadata
        reads: list[int] = []

        def staged_read(path):
            reads.append(1)
            if len(reads) == 1:
                # First read: the now-dead original holder.
                return dead_pid, time.time() - 1.0
            # Subsequent reads: the live process that grabbed the lock.
            return real_read(path)

        monkeypatch.setattr(locks, "_read_lock_metadata", staged_read)

        real_alive = locks._is_pid_alive

        def fake_alive(pid: int) -> bool:
            if pid == dead_pid:
                return False
            return real_alive(pid)

        monkeypatch.setattr(locks, "_is_pid_alive", fake_alive)

        with pytest.raises(LockHeldError) as exc:
            with acquire_advisory_lock(lock_path):
                pass

        assert exc.value.holder_pid != dead_pid
        assert exc.value.holder_pid == live_pid
        assert len(reads) >= 2, "expected a re-read of metadata after the retry"
    finally:
        holder.terminate()
        holder.wait(timeout=5)
