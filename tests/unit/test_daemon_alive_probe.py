"""Regression tests for is_daemon_alive() socket-connect probe.

A live PID alone does not prove a chameleon daemon is running: PIDs are
recycled, and an unrelated process can inherit the old daemon's PID. The
recorded socket must actually accept a connection, and a pidfile with no
socket line cannot belong to a real daemon (a real daemon always records its
socket). These cover the recycled-PID misreport and the missing-socket case.

AF_UNIX path length is capped (~104 bytes on macOS) and pytest's tmp_path on CI
can exceed that, so socket paths live in a short /tmp dir via the ``sock_dir``
fixture; pidfiles stay under tmp_path.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from chameleon_mcp.daemon import _version_tag, is_daemon_alive


@pytest.fixture
def sock_dir():
    """Short /tmp dir for socket paths, cleaned up after.

    AF_UNIX sun_path is capped (~104 bytes on macOS); pytest's tmp_path on CI
    can exceed it, so any path handed to bind() or the connect probe lives
    here instead.
    """
    d = tempfile.mkdtemp(prefix="cap", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _write_pidfile(data_dir: Path, pid: int, sock: str | None) -> None:
    pf = data_dir / f".daemon-{_version_tag()}.pid"
    if sock is None:
        pf.write_text(f"{pid}\n")
    else:
        pf.write_text(f"{pid}\n{sock}\n")


def test_live_pid_missing_socket_line_is_not_alive(tmp_path: Path):
    # Recycled PID with a socket-less pidfile: a real daemon always records its
    # socket, so the absence of a socket line means this is not our daemon.
    fake = tmp_path / "d"
    fake.mkdir()
    _write_pidfile(fake, os.getpid(), None)
    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake):
        assert is_daemon_alive() is False


def test_live_pid_nonconnectable_socket_is_not_alive(tmp_path: Path, sock_dir: Path):
    # The socket path is recorded and even exists as a file, but nothing is
    # listening on it. A live but unrelated PID must not be misreported as a
    # running daemon just because os.kill(pid, 0) succeeds.
    fake = tmp_path / "d"
    fake.mkdir()
    sock_file = sock_dir / "stale.sock"
    sock_file.write_text("")  # exists on disk but not a live listener
    _write_pidfile(fake, os.getpid(), str(sock_file))
    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake):
        assert is_daemon_alive() is False


def test_live_pid_connectable_socket_is_alive(tmp_path: Path, sock_dir: Path):
    # Positive case: a live PID plus a socket that actually accepts a
    # connection is a running daemon. Bind a real AF_UNIX listener so the
    # connect probe succeeds without spinning the full daemon.
    fake = tmp_path / "d"
    fake.mkdir()
    sock_file = sock_dir / "live.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(sock_file))
        listener.listen(1)
        _write_pidfile(fake, os.getpid(), str(sock_file))
        with patch("chameleon_mcp.daemon._plugin_data", return_value=fake):
            assert is_daemon_alive() is True
    finally:
        listener.close()
