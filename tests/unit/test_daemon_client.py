"""Unit tests for chameleon_mcp.daemon_client — the wire-protocol client.

This module is normally only ever mocked. These tests exercise it for real:
frame encode/decode round-trips against a fake UNIX-socket server, oversize and
truncated-frame handling, connect-failure graceful degrade (no daemon running),
the error-envelope contract, and the no-retry guarantee.

The client never raises: every failure mode must return None so the hook helper
falls through to the in-process path.

Isolation: there is no conftest.py in this suite. The client resolves its socket
via chameleon_mcp.daemon.socket_path(), which reads CHAMELEON_PLUGIN_DATA at call
time (through plugin_paths.plugin_data_dir). Each test points that env var at a
fresh dir so no shared daemon state leaks between tests.

AF_UNIX path length is capped (~104 bytes on macOS). pytest's tmp_path on CI can
exceed that, so tests that bind a real socket use a short /tmp dir via the
``sock_dir`` fixture instead of tmp_path.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time

import pytest

from chameleon_mcp import daemon as daemon_mod
from chameleon_mcp import daemon_client
from chameleon_mcp.daemon import MAX_FRAME_BYTES, _LEN_STRUCT, recv_frame, send_frame


@pytest.fixture
def sock_dir(monkeypatch):
    """A short /tmp dir wired into CHAMELEON_PLUGIN_DATA, cleaned up after.

    Short path keeps the AF_UNIX socket name under the kernel cap. Setting the
    env var here is the inline isolation the suite expects (no conftest): the
    client's socket_path() reads CHAMELEON_PLUGIN_DATA on every call.
    """
    d = tempfile.mkdtemp(prefix="cdc", dir="/tmp")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", d)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _FakeDaemon:
    """A one-shot fake daemon: accepts a single connection, reads the request
    frame, and replies with a caller-supplied raw byte string or framed object.

    Runs in a daemon thread so the client's call() can drive the round-trip.
    Records how many connections were accepted (for the no-retry assertion) and
    the request bytes it received.
    """

    def __init__(self, sock_path: str):
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(sock_path)
        self.srv.listen(8)
        self.srv.settimeout(5.0)
        self.accepts = 0
        self.requests: list[bytes | None] = []
        self._thread: threading.Thread | None = None

    def serve_framed(self, response_obj) -> None:
        """Reply with a single length-prefixed JSON frame of ``response_obj``."""

        def _run():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                # Failure-path tests close the server before the client ever
                # connects (or never connect at all). A timed-out/closed accept
                # is expected here — exit the thread quietly instead of leaking
                # an unhandled TimeoutError into a later test.
                return
            self.accepts += 1
            try:
                self.requests.append(recv_frame(conn))
                send_frame(conn, json.dumps(response_obj).encode("utf-8"))
            finally:
                conn.close()

        self._start(_run)

    def serve_raw(self, raw: bytes | None, *, read_request: bool = True) -> None:
        """Read the request (optionally), then send ``raw`` bytes verbatim.

        ``raw=None`` means: read the request, then close without replying (EOF).
        """

        def _run():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                # Failure-path tests close the server before the client ever
                # connects (or never connect at all). A timed-out/closed accept
                # is expected here — exit the thread quietly instead of leaking
                # an unhandled TimeoutError into a later test.
                return
            self.accepts += 1
            try:
                if read_request:
                    self.requests.append(recv_frame(conn))
                if raw is not None:
                    conn.sendall(raw)
            finally:
                conn.close()

        self._start(_run)

    def serve_stall(self, seconds: float) -> None:
        """Read the request, then sleep without replying (drives client timeout)."""

        def _run():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                # Failure-path tests close the server before the client ever
                # connects (or never connect at all). A timed-out/closed accept
                # is expected here — exit the thread quietly instead of leaking
                # an unhandled TimeoutError into a later test.
                return
            self.accepts += 1
            try:
                self.requests.append(recv_frame(conn))
                time.sleep(seconds)
            finally:
                conn.close()

        self._start(_run)

    def _start(self, target) -> None:
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def close(self) -> None:
        try:
            self.srv.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Input validation: bad method / payload return None before any socket touch.
# ---------------------------------------------------------------------------


def test_empty_method_returns_none(sock_dir):
    assert daemon_client.call("") is None


def test_non_string_method_returns_none(sock_dir):
    assert daemon_client.call(123) is None  # type: ignore[arg-type]
    assert daemon_client.call(None) is None  # type: ignore[arg-type]


def test_non_dict_payload_returns_none(sock_dir):
    assert daemon_client.call("ping", payload=[1, 2, 3]) is None  # type: ignore[arg-type]
    assert daemon_client.call("ping", payload="oops") is None  # type: ignore[arg-type]


def test_unserializable_payload_returns_none(sock_dir):
    # Stand up a real socket so the missing-socket short-circuit can't mask the
    # json-encode failure path. A set is not JSON-serializable.
    sp = daemon_mod.socket_path()
    fake = _FakeDaemon(str(sp))
    try:
        fake.serve_framed({"ok": True})  # should never be reached
        result = daemon_client.call("ping", {"bad": {1, 2, 3}})
        assert result is None
        # The encode failed before connecting, so the daemon got no request.
        assert fake.accepts == 0
    finally:
        fake.close()


# ---------------------------------------------------------------------------
# Connect failure: no daemon listening -> graceful degrade to None.
# ---------------------------------------------------------------------------


def test_no_socket_file_returns_none(sock_dir):
    # Fresh data dir, nothing bound: socket_path() does not exist.
    assert not daemon_mod.socket_path().exists()
    assert daemon_client.call("ping") is None


def test_stale_socket_no_listener_returns_none(sock_dir):
    # A socket *file* exists but nothing is accepting on it (e.g. a crashed
    # daemon left the inode). connect() must fail and call() must degrade.
    sp = daemon_mod.socket_path()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sp))
    # Do NOT listen() — connect() gets ECONNREFUSED.
    try:
        assert sp.exists()
        assert daemon_client.call("ping") is None
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Happy path: real round-trip through a fake daemon.
# ---------------------------------------------------------------------------


def test_roundtrip_returns_full_response_dict(sock_dir):
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed({"ok": True, "data": {"hello": "world"}})
        result = daemon_client.call("get_pattern_context", {"file_path": "/foo/bar.ts"})
        fake.join()
        # call() returns the WHOLE response dict, not just the "data" payload.
        assert result == {"ok": True, "data": {"hello": "world"}}
    finally:
        fake.close()


def test_request_frame_shape_is_method_and_payload(sock_dir):
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed({"ok": 1})
        daemon_client.call("get_rules", {"archetype": "controller"})
        fake.join()
        assert fake.requests == [b'{"method": "get_rules", "payload": {"archetype": "controller"}}']
    finally:
        fake.close()


def test_none_payload_encodes_empty_object(sock_dir):
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed({"ok": 1})
        daemon_client.call("ping")  # payload omitted
        fake.join()
        assert fake.requests == [b'{"method": "ping", "payload": {}}']
    finally:
        fake.close()


def test_roundtrip_unicode_payload(sock_dir):
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed({"echo": "héllo-é中"})
        result = daemon_client.call("ping", {"name": "café"})
        fake.join()
        # The unicode value round-trips intact across the wire.
        assert result == {"echo": "héllo-é中"}
        # json.dumps defaults to ensure_ascii=True, so the request frame escapes
        # non-ASCII to \uXXXX rather than emitting raw UTF-8 bytes. Decoding the
        # frame back must reproduce the original string.
        assert fake.requests[0] is not None
        assert b"caf\\u00e9" in fake.requests[0]
        assert json.loads(fake.requests[0])["payload"]["name"] == "café"
    finally:
        fake.close()


# ---------------------------------------------------------------------------
# Error envelope: a response with an "error" key returns None.
# ---------------------------------------------------------------------------


def test_error_envelope_returns_none(sock_dir):
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed({"error": "method not found"})
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


def test_error_key_falsy_still_returns_none(sock_dir):
    # The contract keys on presence of "error", not its truthiness.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed({"error": None, "data": {"x": 1}})
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


# ---------------------------------------------------------------------------
# Malformed responses: non-dict JSON, invalid JSON, empty frame.
# ---------------------------------------------------------------------------


def test_non_dict_json_response_returns_none(sock_dir):
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed([1, 2, 3])  # valid JSON, but a list
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


def test_invalid_json_response_returns_none(sock_dir):
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        bad = b"not-json{"
        fake.serve_raw(_LEN_STRUCT.pack(len(bad)) + bad)
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


def test_empty_response_frame_returns_none(sock_dir):
    # An explicit zero-length frame: recv_frame yields b"", json.loads("")
    # raises, call() catches it and returns None.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_raw(_LEN_STRUCT.pack(0))
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


def test_non_utf8_response_returns_none(sock_dir):
    # Bytes that are not valid UTF-8: decode raises UnicodeDecodeError -> None.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        bad = b"\xff\xfe\xfd"
        fake.serve_raw(_LEN_STRUCT.pack(len(bad)) + bad)
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


# ---------------------------------------------------------------------------
# Truncated / EOF / oversize response framing.
# ---------------------------------------------------------------------------


def test_server_closes_without_responding_returns_none(sock_dir):
    # Server reads the request then closes the connection: recv_frame hits EOF
    # reading the header and returns None.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_raw(None)
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


def test_truncated_response_payload_returns_none(sock_dir):
    # Header claims 100 bytes, server sends 10 then closes: recv_frame can't
    # fill the payload and returns None.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_raw(_LEN_STRUCT.pack(100) + b"x" * 10)
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


def test_partial_response_header_returns_none(sock_dir):
    # Only 2 of the 4 header bytes, then EOF: recv_frame returns None.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_raw(b"\x00\x00")
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


def test_oversize_response_header_returns_none(sock_dir):
    # Header declares a length above MAX_FRAME_BYTES: recv_frame rejects it
    # without reading the body, and call() returns None.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_raw(_LEN_STRUCT.pack(MAX_FRAME_BYTES + 1))
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
    finally:
        fake.close()


# ---------------------------------------------------------------------------
# Oversize request: rejected client-side before connecting.
# ---------------------------------------------------------------------------


def test_oversize_request_returns_none_without_connecting(sock_dir):
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed({"ok": True})  # never reached
        huge = {"blob": "x" * (MAX_FRAME_BYTES + 1024)}
        result = daemon_client.call("ping", huge)
        assert result is None
        # The frame exceeds MAX_FRAME_BYTES once JSON-wrapped, so the client
        # bails before opening a connection.
        time.sleep(0.05)
        assert fake.accepts == 0
    finally:
        fake.close()


def test_large_but_inbounds_request_roundtrips(sock_dir):
    # A payload near, but under, the cap must still go through. The JSON wrapper
    # adds overhead, so leave headroom under MAX_FRAME_BYTES.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed({"ok": True})
        payload = {"blob": "y" * (MAX_FRAME_BYTES - 1024)}
        result = daemon_client.call("ping", payload)
        fake.join()
        assert result == {"ok": True}
        assert fake.requests[0] is not None
        assert json.loads(fake.requests[0])["payload"] == payload
    finally:
        fake.close()


# ---------------------------------------------------------------------------
# Timeout and no-retry behavior.
# ---------------------------------------------------------------------------


def test_timeout_returns_none_within_budget(sock_dir):
    # Server accepts and reads the request but never replies. The client must
    # give up at its deadline and return None, not block on the server's longer
    # stall.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_stall(3.0)
        start = time.monotonic()
        result = daemon_client.call("ping", timeout=0.2)
        elapsed = time.monotonic() - start
        assert result is None
        # Floor on per-recv timeout is 0.05s; allow scheduling slack but it must
        # be far below the server's 3s stall.
        assert elapsed < 1.0, f"call blocked {elapsed:.2f}s past its 0.2s timeout"
        fake.join()
    finally:
        fake.close()


def test_no_retry_on_error_envelope(sock_dir):
    # On a daemon error the client returns None and does NOT reconnect/retry —
    # exactly one connection should be accepted.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_framed({"error": "boom"})
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
        assert fake.accepts == 1
    finally:
        fake.close()


def test_no_retry_on_eof(sock_dir):
    # On a truncated/closed response the client returns None with a single
    # accept — no reconnect.
    fake = _FakeDaemon(str(daemon_mod.socket_path()))
    try:
        fake.serve_raw(None)
        result = daemon_client.call("ping")
        fake.join()
        assert result is None
        assert fake.accepts == 1
    finally:
        fake.close()
