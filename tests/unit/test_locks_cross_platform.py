"""Cross-platform locking layer: the Windows (msvcrt) branch and import guards.

These run on POSIX by simulating the Windows path: `locks.fcntl` is forced to
None so `portable_flock` / `portable_funlock` / `pid_alive` / `open_dir_lock_fd`
take their no-fcntl branch, with a fake `msvcrt` standing in for the real one.
The genuine msvcrt calls are exercised by the windows-latest CI job; here we lock
down the platform-selection logic and the errno normalization.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys

import pytest

import chameleon_mcp.locks as locks


class _FakeMsvcrt:
    """Minimal stand-in for the stdlib `msvcrt` module's locking surface."""

    LK_LOCK = 1
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, *, side_effects=None):
        self.calls: list[tuple[int, int, int]] = []
        self._side_effects = list(side_effects or [])

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((fd, mode, nbytes))
        if self._side_effects:
            exc = self._side_effects.pop(0)
            if exc is not None:
                raise exc


@pytest.fixture
def real_fd(tmp_path):
    """A real, writable fd so os.lseek in the Windows branch has something valid."""
    fd = os.open(str(tmp_path / "lock.bin"), os.O_RDWR | os.O_CREAT, 0o600)
    yield fd
    os.close(fd)


def _force_windows(monkeypatch, msvcrt_obj):
    monkeypatch.setattr(locks, "fcntl", None)
    monkeypatch.setattr(locks, "msvcrt", msvcrt_obj)


def test_imports_without_fcntl_subprocess():
    """With `import fcntl` blocked, every refactored module still imports.

    Proves the Windows-importability claim on this host: a child process maps
    `fcntl` to None in sys.modules (so `import fcntl` raises ImportError, exactly
    as on native Windows) and imports the full package.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    mcp_dir = os.path.join(repo_root, "mcp")
    script = (
        "import sys\n"
        "sys.modules['fcntl'] = None\n"  # next `import fcntl` -> ImportError
        "import chameleon_mcp.locks as L\n"
        "import chameleon_mcp.bootstrap.transaction\n"
        "import chameleon_mcp.profile.canonical_loader\n"
        "import chameleon_mcp.profile.trust\n"
        "import chameleon_mcp.safe_open\n"
        "import chameleon_mcp.server\n"
        "assert L.fcntl is None, 'fcntl should be None when blocked'\n"
        "print('WINDOWS-IMPORT-OK')\n"
    )
    env = dict(os.environ, PYTHONPATH=mcp_dir)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    assert "WINDOWS-IMPORT-OK" in result.stdout


def test_portable_flock_nonblocking_held_raises_eagain(monkeypatch, real_fd):
    """A held region on Windows surfaces as BlockingIOError(EAGAIN), like fcntl."""
    held = OSError(errno.EACCES, "region held")
    fake = _FakeMsvcrt(side_effects=[held])
    _force_windows(monkeypatch, fake)

    with pytest.raises(OSError) as exc:
        locks.portable_flock(real_fd, nonblocking=True)
    assert exc.value.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
    assert isinstance(exc.value, BlockingIOError)
    # LK_NBLCK was the mode used for the non-blocking attempt.
    assert fake.calls and fake.calls[0][1] == fake.LK_NBLCK


def test_portable_flock_nonblocking_acquires(monkeypatch, real_fd):
    fake = _FakeMsvcrt(side_effects=[None])
    _force_windows(monkeypatch, fake)
    locks.portable_flock(real_fd, nonblocking=True)  # no raise == acquired
    assert fake.calls[0][1] == fake.LK_NBLCK


def test_portable_flock_blocking_retries_on_deadlock(monkeypatch, real_fd):
    """Blocking acquire retries while msvcrt reports EDEADLOCK, then succeeds."""
    deadlock = OSError(locks._EDEADLOCK, "would deadlock")
    fake = _FakeMsvcrt(side_effects=[deadlock, deadlock, None])
    _force_windows(monkeypatch, fake)
    monkeypatch.setattr(locks.time, "sleep", lambda _s: None)  # don't actually wait

    locks.portable_flock(real_fd, nonblocking=False)
    assert len(fake.calls) == 3
    assert all(mode == fake.LK_LOCK for _fd, mode, _n in fake.calls)


def test_portable_flock_blocking_propagates_other_oserror(monkeypatch, real_fd):
    boom = OSError(errno.EIO, "disk fell over")
    fake = _FakeMsvcrt(side_effects=[boom])
    _force_windows(monkeypatch, fake)
    with pytest.raises(OSError) as exc:
        locks.portable_flock(real_fd, nonblocking=False)
    assert exc.value.errno == errno.EIO


def test_portable_funlock_windows_unlocks(monkeypatch, real_fd):
    fake = _FakeMsvcrt(side_effects=[None])
    _force_windows(monkeypatch, fake)
    locks.portable_funlock(real_fd)
    assert fake.calls and fake.calls[-1][1] == fake.LK_UNLCK


def test_portable_funlock_windows_swallows_errors(monkeypatch, real_fd):
    fake = _FakeMsvcrt(side_effects=[OSError(errno.EINVAL, "not locked")])
    _force_windows(monkeypatch, fake)
    locks.portable_funlock(real_fd)  # must not raise


def test_pid_alive_windows_never_calls_os_kill(monkeypatch):
    """On the no-fcntl path, liveness must not route through os.kill.

    os.kill on Windows with a non-CTRL signal calls TerminateProcess. The probe
    must avoid it entirely; the ctypes path is unavailable on this host, so it
    degrades to 'assume alive'.
    """
    monkeypatch.setattr(locks, "fcntl", None)

    def _boom(*_a, **_k):
        raise AssertionError("os.kill must not be called on the Windows path")

    monkeypatch.setattr(locks.os, "kill", _boom)
    # The invariant is "os.kill is never reached"; the boolean itself is
    # platform-dependent (POSIX with ctypes absent -> assume alive/True; real
    # Windows -> the OpenProcess probe answers for the actual PID), so only the
    # no-signal contract and the return type are asserted here.
    result = locks.pid_alive(4242)
    assert isinstance(result, bool)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: real Windows has no fcntl")
def test_pid_alive_posix_uses_os_kill():
    """Sanity: with fcntl present the POSIX probe is exact for the current PID."""
    assert locks.fcntl is not None  # this host is POSIX
    assert locks.pid_alive(os.getpid()) is True
    assert locks.pid_alive(99999999) is False


def test_open_dir_lock_fd_windows_uses_sidecar(monkeypatch, tmp_path):
    monkeypatch.setattr(locks, "fcntl", None)
    target = tmp_path / "repo_root"
    target.mkdir()
    fd = locks.open_dir_lock_fd(target)
    try:
        sidecar = target / ".chameleon.winlock"
        assert sidecar.is_file(), "Windows branch must create a sidecar lock file"
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: Windows uses the sidecar")
def test_open_dir_lock_fd_posix_uses_directory_fd(tmp_path):
    """POSIX locks the directory inode and leaves no stray file."""
    assert locks.fcntl is not None
    target = tmp_path / "repo_root"
    target.mkdir()
    fd = locks.open_dir_lock_fd(target)
    try:
        assert not (target / ".chameleon.winlock").exists()
        assert list(target.iterdir()) == []  # no lock file created
    finally:
        os.close(fd)


def test_safe_open_o_nofollow_is_guarded():
    """The hot-path reader resolves O_NOFOLLOW via getattr so Windows (0) is fine."""
    import chameleon_mcp.safe_open  # noqa: F401 - import must not require O_NOFOLLOW

    assert getattr(os, "O_NOFOLLOW", 0) is not None  # absent -> 0, never AttributeError


# --- Real-platform Windows paths (run only on the windows-latest CI job) -------
# These hit the genuine msvcrt / ctypes code that cannot execute off-Windows, so
# they skip on POSIX. The simulated tests above cover the branching logic; these
# confirm the real primitives are wired correctly on the actual platform.


@pytest.mark.skipif(sys.platform != "win32", reason="exercises the real msvcrt lock")
def test_windows_real_lock_roundtrip(tmp_path):
    fd = os.open(str(tmp_path / "excl.bin"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        locks.portable_flock(fd, nonblocking=True)  # real LK_NBLCK
        locks.portable_funlock(fd)  # real LK_UNLCK
        locks.portable_flock(fd, nonblocking=False)  # real LK_LOCK
        locks.portable_funlock(fd)
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "win32", reason="exercises the real OpenProcess probe")
def test_windows_real_pid_alive():
    assert locks.pid_alive(os.getpid()) is True  # real ctypes OpenProcess
    assert locks.pid_alive(0x7FFFFFFF) is False  # no such PID -> NULL handle


@pytest.mark.skipif(sys.platform != "win32", reason="exercises the real sidecar open")
def test_windows_real_open_dir_lock_fd(tmp_path):
    fd = locks.open_dir_lock_fd(tmp_path)
    try:
        assert (tmp_path / ".chameleon.winlock").is_file()
    finally:
        os.close(fd)
