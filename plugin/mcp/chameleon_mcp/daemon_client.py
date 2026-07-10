"""Tiny client for the chameleon-mcp daemon.

Connects to the UNIX socket at ${PLUGIN_DATA}/.daemon-<version_tag>.sock, sends a
length-prefixed JSON request, reads a length-prefixed JSON response.
One request per connection — same model as the daemon side.

POSIX-only: AF_UNIX does not exist on Windows. `call()` returns None there
before touching a socket, so the hook helper falls back to the in-process path.

Contract:
- `call()` returns the full decoded response envelope dict on success (the
  caller unwraps `data` itself), or `None` on ANY failure (refused
  connection, oversize, timeout, parse error). The hook helper takes `None`
  as the signal to fall back to the in-process path. We deliberately never
  raise from this module — the daemon is a performance optimization, not a
  correctness layer.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any

from chameleon_mcp.daemon import (
    MAX_FRAME_BYTES,
    _af_unix_available,
    recv_frame,
    send_frame,
    socket_path,
)

DEFAULT_TIMEOUT_S = 1.5


def call(
    method: str, payload: dict | None = None, *, timeout: float = DEFAULT_TIMEOUT_S
) -> dict | None:
    """Send a single request to the daemon. Returns the response dict or None.

    Failure modes that return None:
    - Daemon not running (ECONNREFUSED, FileNotFoundError on the socket path).
    - Per-call timeout exceeded (configurable via `timeout`).
    - Oversize request or response.
    - Daemon returned an error envelope (response has "error" key).
    - Any unexpected exception (defensive: every error returns None).

    The caller is expected to treat None as "fall through to the
    subprocess-per-call path" and proceed without retrying.
    """
    if not isinstance(method, str) or not method:
        return None
    if payload is not None and not isinstance(payload, dict):
        return None

    # POSIX-only: no AF_UNIX on Windows. Degrade to the in-process path.
    if not _af_unix_available():
        return None

    sock_path = socket_path()
    if not sock_path.exists():
        return None

    try:
        request_bytes = json.dumps({"method": method, "payload": payload or {}}).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if len(request_bytes) > MAX_FRAME_BYTES:
        return None

    deadline = time.monotonic() + max(0.05, float(timeout))

    conn = None
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            conn.connect(str(sock_path))
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            return None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        conn.settimeout(max(0.05, remaining))

        if not send_frame(conn, request_bytes):
            return None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        conn.settimeout(max(0.05, remaining))

        response_bytes = recv_frame(conn)
        if response_bytes is None:
            return None
        try:
            response: Any = json.loads(response_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(response, dict):
            return None
        if "error" in response:
            return None
        return response
    except (TimeoutError, OSError):
        return None
    except Exception:  # noqa: BLE001 — never raise to callers
        return None
    finally:
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
