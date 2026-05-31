"""A stalled client must not wedge the single-threaded daemon accept loop.

Accepted sockets do not inherit the listening socket's timeout, so before the
fix a client that connected but never sent its frame blocked recv() forever and
starved every later hook for the rest of the session. serve_forever now sets
CONN_RECV_TIMEOUT_S on each accepted connection.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time

from chameleon_mcp import daemon as daemon_mod
from chameleon_mcp.daemon import _DaemonState, recv_frame, send_frame, serve_forever


def _ping_dispatcher(method: str, payload: dict) -> dict:
    return {"ok": True, "method": method}


def test_stalled_client_does_not_wedge_daemon(monkeypatch):
    # Short per-connection read timeout keeps the test fast.
    monkeypatch.setattr(daemon_mod, "CONN_RECV_TIMEOUT_S", 0.3)

    # AF_UNIX paths are capped (~104 bytes on macOS); pytest's tmp_path on CI
    # runners can exceed that, so bind under a short /tmp dir instead.
    sock_dir = tempfile.mkdtemp(prefix="cs", dir="/tmp")
    sock_path = os.path.join(sock_dir, "d.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(8)

    state = _DaemonState(idle_timeout_s=30.0)
    server_thread = threading.Thread(
        target=serve_forever, args=(srv, state, _ping_dispatcher), daemon=True
    )
    server_thread.start()

    stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    good = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # Client 1 connects and sends NOTHING — it stalls mid-frame.
        stalled.connect(str(sock_path))

        # Client 2 is well-behaved and must still be served promptly.
        good.settimeout(3.0)
        good.connect(str(sock_path))
        send_frame(good, b'{"method": "ping", "payload": {}}')

        start = time.time()
        resp = recv_frame(good)
        elapsed = time.time() - start

        assert resp is not None, "well-behaved client got no response — daemon wedged"
        assert elapsed < 2.0, f"response took {elapsed:.2f}s; the stalled client wedged the loop"
    finally:
        state.shutdown_requested = True
        for s in (stalled, good, srv):
            try:
                s.close()
            except OSError:
                pass
        server_thread.join(timeout=3.0)
        shutil.rmtree(sock_dir, ignore_errors=True)
