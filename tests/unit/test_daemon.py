"""Unit tests for chameleon_mcp.daemon wire protocol and state."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.daemon import (
    _LEN_STRUCT,
    DEFAULT_IDLE_TIMEOUT_S,
    MAX_FRAME_BYTES,
    _af_unix_available,
    _DaemonState,
    _flock_reliable,
    _idle_timeout_from_env,
    _sweep_orphan_version_files,
    _version_tag,
    daemon_info,
    pid_path,
    recv_frame,
    run_daemon,
    send_frame,
    socket_path,
    start_daemon,
)


def _socketpair() -> tuple[socket.socket, socket.socket]:
    """Create a connected pair of sockets for testing."""
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


def _send_raw(sock: socket.socket, data: bytes) -> None:
    """Send raw bytes (bypassing send_frame) for low-level tests."""
    sock.sendall(data)


def test_frame_roundtrip_small():
    a, b = _socketpair()
    try:
        payload = b'{"method": "ping", "payload": {}}'
        assert send_frame(a, payload) is True
        got = recv_frame(b)
        assert got == payload
    finally:
        a.close()
        b.close()


def test_frame_roundtrip_empty():
    a, b = _socketpair()
    try:
        assert send_frame(a, b"") is True
        got = recv_frame(b)
        assert got == b""
    finally:
        a.close()
        b.close()


def test_frame_roundtrip_json():
    a, b = _socketpair()
    try:
        obj = {"method": "get_pattern_context", "payload": {"file_path": "/foo/bar.ts"}}
        payload = json.dumps(obj).encode("utf-8")
        assert send_frame(a, payload) is True
        got = recv_frame(b)
        assert json.loads(got) == obj
    finally:
        a.close()
        b.close()


def test_recv_frame_oversize_returns_none():
    a, b = _socketpair()
    try:
        fake_len = MAX_FRAME_BYTES + 1
        _send_raw(a, _LEN_STRUCT.pack(fake_len))
        a.close()
        result = recv_frame(b)
        assert result is None
    finally:
        b.close()


def test_recv_frame_eof_returns_none():
    a, b = _socketpair()
    try:
        a.close()
        result = recv_frame(b)
        assert result is None
    finally:
        b.close()


def test_recv_frame_partial_header_returns_none():
    a, b = _socketpair()
    try:
        _send_raw(a, b"\x00\x00")
        a.close()
        result = recv_frame(b)
        assert result is None
    finally:
        b.close()


def test_recv_frame_truncated_payload_returns_none():
    a, b = _socketpair()
    try:
        _send_raw(a, _LEN_STRUCT.pack(100) + b"x" * 10)
        a.close()
        result = recv_frame(b)
        assert result is None
    finally:
        b.close()


def test_send_frame_oversize_returns_false():
    a, b = _socketpair()
    try:
        big = b"x" * (MAX_FRAME_BYTES + 1)
        assert send_frame(a, big) is False
    finally:
        a.close()
        b.close()


def test_send_frame_moderate_roundtrip():
    """Round-trip a payload larger than the length prefix (4 KB)."""
    a, b = _socketpair()
    try:
        payload = b"y" * 4096
        assert send_frame(a, payload) is True
        got = recv_frame(b)
        assert got == payload
    finally:
        a.close()
        b.close()


def test_send_frame_exactly_max_roundtrips():
    """Boundary check: MAX_FRAME_BYTES round-trips. Uses a thread so the
    sender doesn't block waiting for the receiver to drain."""
    import threading

    a, b = _socketpair()
    result = [None]

    def _reader():
        result[0] = recv_frame(b)

    t = threading.Thread(target=_reader)
    t.start()
    try:
        big = b"z" * MAX_FRAME_BYTES
        ok = send_frame(a, big)
        assert ok is True
        t.join(timeout=10)
        assert result[0] == big
    finally:
        a.close()
        b.close()


def test_send_frame_closed_socket_returns_false():
    a, b = _socketpair()
    b.close()
    a.close()
    assert send_frame(a, b"hello") is False


def test_daemon_state_initial_values():
    before = time.time()
    state = _DaemonState(idle_timeout_s=42.0)
    after = time.time()

    assert state.idle_timeout_s == 42.0
    assert state.request_count == 0
    assert state.shutdown_requested is False
    assert before <= state.started_at <= after
    assert before <= state.last_request_at <= after


def test_daemon_state_mark_request():
    state = _DaemonState(idle_timeout_s=10.0)
    initial_time = state.last_request_at
    assert state.request_count == 0

    time.sleep(0.01)
    state.mark_request()

    assert state.request_count == 1
    assert state.last_request_at >= initial_time


def test_daemon_state_mark_request_increments():
    state = _DaemonState(idle_timeout_s=10.0)
    for i in range(5):
        state.mark_request()
    assert state.request_count == 5


def test_daemon_info_no_pidfile(tmp_path: Path):
    """When no pidfile exists, daemon_info() returns alive=False."""

    fake_data = tmp_path / "chameleon-test"
    fake_data.mkdir()

    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake_data):
        info = daemon_info()

    assert info["alive"] is False
    assert info["pid"] is None
    assert info["uptime_s"] is None


def test_daemon_info_dead_pid(tmp_path: Path):
    """When pidfile points to a dead PID, daemon_info() returns alive=False."""

    fake_data = tmp_path / "chameleon-test"
    fake_data.mkdir()

    pf = fake_data / ".daemon.pid"
    pf.write_text("99999999\n/tmp/fake.sock\n")

    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake_data):
        info = daemon_info()

    assert info["alive"] is False
    assert info["pid"] is None


def test_idle_timeout_default():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CHAMELEON_DAEMON_IDLE_TIMEOUT", None)
        assert _idle_timeout_from_env() == DEFAULT_IDLE_TIMEOUT_S


def test_idle_timeout_from_env_valid():
    with patch.dict(os.environ, {"CHAMELEON_DAEMON_IDLE_TIMEOUT": "30.5"}):
        assert _idle_timeout_from_env() == 30.5


def test_idle_timeout_from_env_zero_uses_default():
    with patch.dict(os.environ, {"CHAMELEON_DAEMON_IDLE_TIMEOUT": "0"}):
        assert _idle_timeout_from_env() == DEFAULT_IDLE_TIMEOUT_S


def test_idle_timeout_from_env_negative_uses_default():
    with patch.dict(os.environ, {"CHAMELEON_DAEMON_IDLE_TIMEOUT": "-5"}):
        assert _idle_timeout_from_env() == DEFAULT_IDLE_TIMEOUT_S


def test_idle_timeout_from_env_garbage_uses_default():
    with patch.dict(os.environ, {"CHAMELEON_DAEMON_IDLE_TIMEOUT": "notanumber"}):
        assert _idle_timeout_from_env() == DEFAULT_IDLE_TIMEOUT_S


def test_idle_timeout_from_env_empty_uses_default():
    with patch.dict(os.environ, {"CHAMELEON_DAEMON_IDLE_TIMEOUT": ""}):
        assert _idle_timeout_from_env() == DEFAULT_IDLE_TIMEOUT_S


def test_socket_and_pid_paths_are_version_scoped(tmp_path: Path):
    fake = tmp_path / "d"
    fake.mkdir()
    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake):
        sp = socket_path()
        pp = pid_path()
    tag = _version_tag()
    assert tag and "/" not in tag
    assert sp.name == f".daemon-{tag}.sock"
    assert pp.name == f".daemon-{tag}.pid"


def test_socket_path_differs_across_versions(tmp_path: Path):
    # Regression: a newer plugin build must not reuse a daemon spawned by an
    # older build (which would serve stale in-memory advisory logic until it
    # idle-exited). Different versions -> different sockets.
    fake = tmp_path / "d"
    fake.mkdir()
    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake):
        with patch("chameleon_mcp.daemon._version_tag", return_value="1.2.3"):
            a = socket_path()
        with patch("chameleon_mcp.daemon._version_tag", return_value="1.2.4"):
            b = socket_path()
    assert a != b
    assert a.name == ".daemon-1.2.3.sock"
    assert b.name == ".daemon-1.2.4.sock"


def test_sweep_orphan_version_files_drops_dead_keeps_live(tmp_path: Path):
    fake = tmp_path / "d"
    fake.mkdir()
    dead_pid = fake / ".daemon-9.9.9.pid"
    dead_pid.write_text("99999999\nx\n")
    dead_sock = fake / ".daemon-9.9.9.sock"
    dead_sock.write_text("")
    live_pid = fake / ".daemon-8.8.8.pid"
    live_pid.write_text(f"{os.getpid()}\nx\n")  # our own pid -> alive
    live_sock = fake / ".daemon-8.8.8.sock"
    live_sock.write_text("")
    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake):
        _sweep_orphan_version_files()
    assert not dead_pid.exists()
    assert not dead_sock.exists()
    assert live_pid.exists()
    assert live_sock.exists()


def test_sweep_orphan_keeps_empty_pidfile_startup_window(tmp_path: Path):
    # A daemon mid-startup may have written an empty/half pidfile before its pid.
    # The sweep must not reap it (would delete a live daemon's socket).
    fake = tmp_path / "d"
    fake.mkdir()
    empty_pid = fake / ".daemon-7.7.7.pid"
    empty_pid.write_text("")  # not yet written
    empty_sock = fake / ".daemon-7.7.7.sock"
    empty_sock.write_text("")
    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake):
        _sweep_orphan_version_files()
    assert empty_pid.exists()
    assert empty_sock.exists()


# ---------------------------------------------------------------------------
# Windows compatibility: AF_UNIX is POSIX-only. The daemon is an optional
# performance layer, so its absence must degrade gracefully (no crash).
# ---------------------------------------------------------------------------


def test_af_unix_available_reflects_socket_module():
    assert _af_unix_available() is hasattr(socket, "AF_UNIX")


def test_run_daemon_degrades_when_af_unix_missing(tmp_path: Path):
    # Simulate Windows where socket.AF_UNIX does not exist. run_daemon() must
    # return a non-zero status and not raise AttributeError on socket.socket().
    fake = tmp_path / "d"
    fake.mkdir()
    with (
        patch("chameleon_mcp.daemon._plugin_data", return_value=fake),
        patch("chameleon_mcp.daemon._af_unix_available", return_value=False),
    ):
        rc = run_daemon()
    assert rc != 0


def test_start_daemon_degrades_when_af_unix_missing(tmp_path: Path):
    fake = tmp_path / "d"
    fake.mkdir()
    with (
        patch("chameleon_mcp.daemon._plugin_data", return_value=fake),
        patch("chameleon_mcp.daemon._af_unix_available", return_value=False),
    ):
        result = start_daemon()
    assert result["status"] == "failed"
    assert result["pid"] is None


# ---------------------------------------------------------------------------
# Code-only upgrade safety: a code change without a version bump must still
# change the daemon identity so a new-code hook never reuses a stale daemon.
# ---------------------------------------------------------------------------


def test_version_tag_changes_when_source_changes(monkeypatch):
    # Two different source fingerprints under the same declared version must
    # yield different tags, so the socket/pidfile names differ and the old
    # daemon is never reused after a code-only upgrade.
    monkeypatch.setattr("chameleon_mcp.daemon._code_fingerprint", lambda: "aaaa")
    tag_a = _version_tag()
    monkeypatch.setattr("chameleon_mcp.daemon._code_fingerprint", lambda: "bbbb")
    tag_b = _version_tag()
    assert tag_a != tag_b


def test_version_tag_is_filesystem_safe():
    tag = _version_tag()
    assert tag
    assert "/" not in tag
    assert all(c.isalnum() or c in "._-" for c in tag)


# ---------------------------------------------------------------------------
# stop_daemon flock guard: on platforms where flock is unreliable
# (Windows/NFS) the recycle-TOCTOU probe must be skipped so a spuriously
# acquired lock can't make stop_daemon delete a live daemon's pidfile.
# ---------------------------------------------------------------------------


def test_flock_reliable_false_without_fcntl(monkeypatch):
    monkeypatch.setattr("chameleon_mcp.daemon.fcntl", None)
    assert _flock_reliable() is False


def test_flock_reliable_false_on_nfs(tmp_path, monkeypatch):
    fake = tmp_path / "d"
    fake.mkdir()
    monkeypatch.setattr("chameleon_mcp.daemon._plugin_data", lambda: fake)
    monkeypatch.setattr("chameleon_mcp.daemon._plugin_data_fstype", lambda: "nfs")
    assert _flock_reliable() is False


def test_flock_reliable_true_on_local_fs(tmp_path, monkeypatch):
    fake = tmp_path / "d"
    fake.mkdir()
    monkeypatch.setattr("chameleon_mcp.daemon._plugin_data", lambda: fake)
    monkeypatch.setattr("chameleon_mcp.daemon._plugin_data_fstype", lambda: "apfs")
    assert _flock_reliable() is True


def test_stop_daemon_skips_recycle_probe_when_flock_unreliable(tmp_path, monkeypatch):
    # On an unreliable-flock platform, stop_daemon must NOT use the
    # acquire-means-stale shortcut. It must signal the live pid instead of
    # silently reporting not_running and deleting the pidfile.
    import signal as _signal

    fake = tmp_path / "d"
    fake.mkdir()
    sentinel_pid = 4242
    pf = fake / f".daemon-{_version_tag()}.pid"
    pf.write_text(f"{sentinel_pid}\n/tmp/x.sock\n")

    killed: list[tuple[int, int]] = []
    # alive=True until SIGTERM is delivered, then dead so the wait loop ends.
    alive_state = {"alive": True}

    def _fake_pid_alive(pid: int) -> bool:
        return alive_state["alive"]

    def _fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        if sig == _signal.SIGTERM:
            alive_state["alive"] = False

    from chameleon_mcp.daemon import stop_daemon

    with (
        patch("chameleon_mcp.daemon._plugin_data", return_value=fake),
        patch("chameleon_mcp.daemon._pid_alive", side_effect=_fake_pid_alive),
        patch("chameleon_mcp.daemon._flock_reliable", return_value=False),
        patch("chameleon_mcp.daemon.os.kill", side_effect=_fake_kill),
    ):
        result = stop_daemon(timeout=1.0)

    assert (sentinel_pid, _signal.SIGTERM) in killed
    assert result["status"] == "stopped"
    assert result["pid"] == sentinel_pid
