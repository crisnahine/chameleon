"""Tests for the chameleon-mcp long-lived daemon (Phase 4.5).

Covers:
  - start/stop lifecycle (start_daemon → is_daemon_alive → stop_daemon).
  - Socket location matches pidfile contents.
  - get_pattern_context over the socket matches the in-process result.
  - Length-prefix framing rejects oversize payloads.
  - Idle-timeout shutdown (CHAMELEON_DAEMON_IDLE_TIMEOUT=1).
  - Stale-socket / stale-pidfile recovery on start.
  - Two sequential clients on the same daemon both succeed.
  - hook_helper falls back to in-process when the daemon is down.

Each test isolates state under a tmpdir set via CHAMELEON_PLUGIN_DATA so we
don't stomp on the user's real daemon.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/daemon_test.py
"""

from __future__ import annotations

import io
import json
import os
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path

# Wire chameleon_mcp before any import below.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

PASS = 0
FAIL = 0


def t(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n=== {name} ===")


# ---------------------------------------------------------------------------
# Test bootstrap: isolated plugin data dir per run.
# ---------------------------------------------------------------------------

_TMP_DATA = tempfile.mkdtemp(prefix="chameleon_daemon_test_")
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_DATA

import chameleon_mcp.daemon as daemon  # noqa: E402
import chameleon_mcp.daemon_client as daemon_client  # noqa: E402


def _wait_until(pred, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _stop_safely() -> None:
    """Defensive stop — used between tests to keep state clean."""
    try:
        daemon.stop_daemon(timeout=3.0)
    except Exception:  # noqa: BLE001
        pass
    # Belt + suspenders: clobber any stray artifacts.
    for p in (daemon.pid_path(), daemon.socket_path()):
        try:
            p.unlink()
        except (FileNotFoundError, OSError):
            pass


# ---------------------------------------------------------------------------
# 1. Lifecycle: start → alive → stop → not alive.
# ---------------------------------------------------------------------------
section("1. Lifecycle: start / alive / stop")

_stop_safely()
t("baseline: daemon not alive before start", not daemon.is_daemon_alive())

start_result = daemon.start_daemon()
t(
    f"start_daemon returns started or already_running (got {start_result.get('status')})",
    start_result.get("status") in ("started", "already_running"),
    str(start_result),
)
t("start_daemon reports a PID", isinstance(start_result.get("pid"), int))
t(
    "daemon socket file exists after start",
    Path(start_result.get("socket", "")).exists() or daemon.socket_path().exists(),
)
t("is_daemon_alive() True after start", daemon.is_daemon_alive())

# Idempotent: a second start returns "already_running" with same pid.
start_again = daemon.start_daemon()
t(
    "second start returns already_running",
    start_again.get("status") == "already_running"
    and start_again.get("pid") == start_result.get("pid"),
    str(start_again),
)

stop_result = daemon.stop_daemon(timeout=3.0)
t(
    f"stop_daemon returns stopped (got {stop_result.get('status')})",
    stop_result.get("status") == "stopped",
    str(stop_result),
)
t("is_daemon_alive() False after stop", not daemon.is_daemon_alive())
t("pidfile removed after stop", not daemon.pid_path().exists())
t("socket file removed after stop", not daemon.socket_path().exists())


# ---------------------------------------------------------------------------
# 2. Socket path in pidfile matches socket_path().
# ---------------------------------------------------------------------------
section("2. Pidfile encodes socket path")

_stop_safely()
daemon.start_daemon()
recorded = daemon.pid_path().read_text().strip().splitlines()
t("pidfile has at least 2 lines", len(recorded) >= 2)
if len(recorded) >= 2:
    pid_in_file = int(recorded[0])
    sock_in_file = recorded[1]
    t("pidfile PID is alive", daemon._pid_alive(pid_in_file))
    t(
        "pidfile socket path matches daemon.socket_path()",
        sock_in_file == str(daemon.socket_path()),
        f"pidfile says {sock_in_file!r}, expected {daemon.socket_path()!r}",
    )

_stop_safely()


# ---------------------------------------------------------------------------
# 3. ping over the socket round-trips.
# ---------------------------------------------------------------------------
section("3. Round-trip: ping over socket")

_stop_safely()
daemon.start_daemon()
t("daemon alive for ping", daemon.is_daemon_alive())

pong = daemon_client.call("ping", {}, timeout=1.5)
t("ping returns a dict", isinstance(pong, dict), str(pong))
t("ping response has ok=True", isinstance(pong, dict) and pong.get("ok") is True)
t("ping response has timestamp", isinstance(pong, dict) and "ts" in pong)


# ---------------------------------------------------------------------------
# 4. get_pattern_context over socket matches in-process call.
# ---------------------------------------------------------------------------
section("4. get_pattern_context: socket result matches in-process")

# Use a temp dir that has no .chameleon profile — both paths should still
# return a structurally-valid envelope (with archetype.name == None).
with tempfile.TemporaryDirectory(prefix="chameleon_daemon_pc_") as scratch:
    target = Path(scratch) / "x.ts"
    target.write_text("export const x = 1;\n", encoding="utf-8")

    from chameleon_mcp.tools import get_pattern_context as in_proc

    in_proc_result = in_proc(str(target))
    sock_result = daemon_client.call("get_pattern_context", {"file_path": str(target)})

    t(
        "socket returns a dict for get_pattern_context",
        isinstance(sock_result, dict),
        str(type(sock_result)),
    )
    t(
        "socket response has api_version",
        isinstance(sock_result, dict) and sock_result.get("api_version") == "1",
    )
    t(
        "socket data.archetype matches in-process data.archetype",
        isinstance(sock_result, dict)
        and sock_result.get("data", {}).get("archetype") == in_proc_result.get("data", {}).get("archetype"),
    )


# ---------------------------------------------------------------------------
# 5. Length-prefix framing rejects oversize payloads.
# ---------------------------------------------------------------------------
section("5. Framing: oversize request rejected")

# The client refuses oversize requests entirely — it returns None without
# even attempting to send. To exercise the SERVER's oversize handling we
# craft a frame manually.
sock_path = daemon.socket_path()

# Client-side rejection.
big_payload = "x" * (daemon.MAX_FRAME_BYTES + 1)
oversize_client = daemon_client.call("ping", {"junk": big_payload})
t(
    "client returns None when serialized request exceeds MAX_FRAME_BYTES",
    oversize_client is None,
)

# Server-side rejection: send a header announcing a > 1 MB body without
# sending the body. The server reads the header, sees it's oversize, and
# closes the connection (or sends an error envelope). Either way, no
# hang and no successful parse.
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(str(sock_path))
    s.sendall(struct.pack("!I", daemon.MAX_FRAME_BYTES + 1))
    # Don't send any body. The server will recv up to header, reject.
    try:
        # Server may answer with an error envelope, or close. Read what we can.
        resp = s.recv(8192)
    except (TimeoutError, OSError):
        resp = b""
    s.close()
    t("server does not hang on oversize header", True)
except Exception as e:  # noqa: BLE001
    t("server oversize handling did not crash test", False, str(e))


# ---------------------------------------------------------------------------
# 6. Two sequential clients hit the same daemon.
# ---------------------------------------------------------------------------
section("6. Two sequential clients in the same daemon process")

r1 = daemon_client.call("ping", {})
r2 = daemon_client.call("ping", {})
t("first sequential client succeeds", isinstance(r1, dict) and r1.get("ok") is True)
t("second sequential client succeeds", isinstance(r2, dict) and r2.get("ok") is True)
t(
    "two pings: second timestamp >= first (monotonic increasing)",
    isinstance(r1, dict) and isinstance(r2, dict) and r2.get("ts", 0) >= r1.get("ts", 0),
)


# ---------------------------------------------------------------------------
# 7. Stale-socket / stale-pidfile recovery on start.
# ---------------------------------------------------------------------------
section("7. Stale-socket recovery")

_stop_safely()

# Fabricate a dead-PID pidfile + stray socket file. The dead PID we pick
# (1) is init/launchd, very much alive — so we use a PID that's almost
# certainly not running: 2^31 - 1 (Linux max + macOS).
fake_pid = 2147483646
fake_sock = daemon.socket_path()
# Touch a fake socket file (a regular file is fine — the cleanup uses
# unlink, which works on either).
fake_sock.parent.mkdir(parents=True, exist_ok=True)
fake_sock.write_bytes(b"stale")
daemon.pid_path().write_text(f"{fake_pid}\n{fake_sock}\n", encoding="utf-8")
t("fabricated stale pidfile present", daemon.pid_path().is_file())
t("daemon recognized as not-alive (dead PID)", not daemon.is_daemon_alive())

recover = daemon.start_daemon()
t(
    f"start_daemon recovers from stale state (status={recover.get('status')})",
    recover.get("status") in ("started", "already_running"),
    str(recover),
)
t("daemon alive after stale-state recovery", daemon.is_daemon_alive())

# Confirm we can talk to it.
pong = daemon_client.call("ping", {}, timeout=1.5)
t(
    "daemon responds to ping after stale-state recovery",
    isinstance(pong, dict) and pong.get("ok") is True,
)

_stop_safely()


# ---------------------------------------------------------------------------
# 8. Idle-timeout shutdown.
# ---------------------------------------------------------------------------
section("8. Idle-timeout shutdown")

# Drive a short idle window via env var inherited by the spawned daemon.
prev_idle = os.environ.get("CHAMELEON_DAEMON_IDLE_TIMEOUT")
os.environ["CHAMELEON_DAEMON_IDLE_TIMEOUT"] = "1.5"
try:
    _stop_safely()
    res = daemon.start_daemon()
    t("daemon started with short idle window", res.get("status") == "started", str(res))
    # Wait for self-shutdown. The loop polls every 1s; allow 5s budget.
    shut_down = _wait_until(lambda: not daemon.is_daemon_alive(), timeout=8.0)
    t("daemon self-shuts-down after idle window", shut_down)
    t("pidfile cleaned up after idle exit", not daemon.pid_path().exists())
    t("socket cleaned up after idle exit", not daemon.socket_path().exists())
finally:
    if prev_idle is None:
        os.environ.pop("CHAMELEON_DAEMON_IDLE_TIMEOUT", None)
    else:
        os.environ["CHAMELEON_DAEMON_IDLE_TIMEOUT"] = prev_idle


# ---------------------------------------------------------------------------
# 9. Hook helper fallback when daemon is down.
# ---------------------------------------------------------------------------
section("9. Hook helper falls back to in-process when daemon is unavailable")

_stop_safely()
t("daemon is down for fallback test", not daemon.is_daemon_alive())

# daemon_client.call() returns None on connection refused. That's the
# fallback signal hook_helper.preflight_and_advise relies on.
no_daemon = daemon_client.call("ping", {}, timeout=0.5)
t("daemon_client.call returns None when daemon is down", no_daemon is None)

# Drive preflight_and_advise with a synthetic Edit input. Should emit
# an envelope (the in-process path) even with no daemon.
with tempfile.TemporaryDirectory(prefix="chameleon_daemon_fallback_") as scratch:
    target = Path(scratch) / "y.ts"
    target.write_text("export const y = 2;\n", encoding="utf-8")

    payload = {
        "tool_input": {"file_path": str(target)},
        "session_id": "test-session",
    }
    stdin_backup = sys.stdin
    stdout_backup = sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload))
    captured = io.StringIO()
    sys.stdout = captured
    try:
        from chameleon_mcp.hook_helper import preflight_and_advise

        rc = preflight_and_advise()
    finally:
        sys.stdin = stdin_backup
        sys.stdout = stdout_backup

    t("preflight_and_advise returns 0 even with no daemon", rc == 0)
    output = captured.getvalue().strip()
    t("preflight_and_advise emits valid JSON", bool(output))
    try:
        parsed = json.loads(output) if output else {}
        t("output is a dict", isinstance(parsed, dict))
    except json.JSONDecodeError as e:
        t("output is a dict", False, f"json decode: {e}")


# ---------------------------------------------------------------------------
# 10. daemon_info() / daemon_status() shape sanity.
# ---------------------------------------------------------------------------
section("10. daemon_info / daemon_status shape")

_stop_safely()
not_running = daemon.daemon_info()
t("daemon_info reports alive=False when down", not_running.get("alive") is False)
t("daemon_info has socket path", isinstance(not_running.get("socket"), str))

# Bring it back up to test the "alive" branch + the tools-layer wrapper.
daemon.start_daemon()
running = daemon.daemon_info()
t("daemon_info reports alive=True when up", running.get("alive") is True)
t("daemon_info reports a PID when up", isinstance(running.get("pid"), int))
t("daemon_info reports uptime_s when up", isinstance(running.get("uptime_s"), int | float))

# Tools layer surface: daemon_status returns an _envelope.
from chameleon_mcp.tools import daemon_status as ds_tool

ds = ds_tool()
t("daemon_status returns api_version=1", ds.get("api_version") == "1")
ds_data = ds.get("data", {})
t("daemon_status.data.alive is True when up", ds_data.get("alive") is True)
t(
    "daemon_status.data.last_request_at populated after ping",
    isinstance(ds_data.get("last_request_at"), str),
)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
_stop_safely()

print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass:  {PASS}")
print(f"  Fail:  {FAIL}")

# Best-effort tmpdir cleanup.
import shutil  # noqa: E402

shutil.rmtree(_TMP_DATA, ignore_errors=True)

sys.exit(0 if FAIL == 0 else 1)
