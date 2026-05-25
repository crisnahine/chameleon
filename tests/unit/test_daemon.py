"""Unit tests for chameleon_mcp.daemon wire protocol and state."""
from __future__ import annotations

import json
import os
import socket
import struct
import time
from unittest.mock import patch

import pytest

from chameleon_mcp.daemon import (
    DEFAULT_IDLE_TIMEOUT_S,
    MAX_FRAME_BYTES,
    _DaemonState,
    _idle_timeout_from_env,
    _LEN_STRUCT,
    daemon_info,
    recv_frame,
    send_frame,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _socketpair() -> tuple[socket.socket, socket.socket]:
    """Create a connected pair of sockets for testing."""
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


def _send_raw(sock: socket.socket, data: bytes) -> None:
    """Send raw bytes (bypassing send_frame) for low-level tests."""
    sock.sendall(data)


# ---------------------------------------------------------------------------
# recv_frame / send_frame — valid round-trip
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# recv_frame — oversize
# ---------------------------------------------------------------------------


def test_recv_frame_oversize_returns_none():
    a, b = _socketpair()
    try:
        # Send a header claiming a frame larger than MAX_FRAME_BYTES
        fake_len = MAX_FRAME_BYTES + 1
        _send_raw(a, _LEN_STRUCT.pack(fake_len))
        a.close()
        result = recv_frame(b)
        assert result is None
    finally:
        b.close()


# ---------------------------------------------------------------------------
# recv_frame — EOF
# ---------------------------------------------------------------------------


def test_recv_frame_eof_returns_none():
    a, b = _socketpair()
    try:
        a.close()  # EOF immediately
        result = recv_frame(b)
        assert result is None
    finally:
        b.close()


def test_recv_frame_partial_header_returns_none():
    a, b = _socketpair()
    try:
        # Send only 2 of the 4 header bytes, then EOF
        _send_raw(a, b"\x00\x00")
        a.close()
        result = recv_frame(b)
        assert result is None
    finally:
        b.close()


def test_recv_frame_truncated_payload_returns_none():
    a, b = _socketpair()
    try:
        # Header says 100 bytes but only send 10
        _send_raw(a, _LEN_STRUCT.pack(100) + b"x" * 10)
        a.close()
        result = recv_frame(b)
        assert result is None
    finally:
        b.close()


# ---------------------------------------------------------------------------
# send_frame — oversize
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# send_frame — closed socket
# ---------------------------------------------------------------------------


def test_send_frame_closed_socket_returns_false():
    a, b = _socketpair()
    b.close()
    a.close()
    assert send_frame(a, b"hello") is False


# ---------------------------------------------------------------------------
# _DaemonState
# ---------------------------------------------------------------------------


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

    # Small sleep to ensure time advances
    time.sleep(0.01)
    state.mark_request()

    assert state.request_count == 1
    assert state.last_request_at >= initial_time


def test_daemon_state_mark_request_increments():
    state = _DaemonState(idle_timeout_s=10.0)
    for i in range(5):
        state.mark_request()
    assert state.request_count == 5


# ---------------------------------------------------------------------------
# daemon_info — no pidfile
# ---------------------------------------------------------------------------


def test_daemon_info_no_pidfile(tmp_path: Path):
    """When no pidfile exists, daemon_info() returns alive=False."""
    from pathlib import Path

    fake_data = tmp_path / "chameleon-test"
    fake_data.mkdir()

    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake_data):
        info = daemon_info()

    assert info["alive"] is False
    assert info["pid"] is None
    assert info["uptime_s"] is None


def test_daemon_info_dead_pid(tmp_path: Path):
    """When pidfile points to a dead PID, daemon_info() returns alive=False."""
    from pathlib import Path

    fake_data = tmp_path / "chameleon-test"
    fake_data.mkdir()

    # Write a pidfile pointing to a dead PID
    pf = fake_data / ".daemon.pid"
    sock = fake_data / ".daemon.sock"
    pf.write_text("99999999\n/tmp/fake.sock\n")

    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake_data):
        info = daemon_info()

    assert info["alive"] is False
    assert info["pid"] is None


# ---------------------------------------------------------------------------
# _idle_timeout_from_env
# ---------------------------------------------------------------------------


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
