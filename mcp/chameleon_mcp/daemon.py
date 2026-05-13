"""Long-lived chameleon-mcp daemon (Phase 4.5).

Replaces the subprocess-per-call hook (200-500ms warm latency) with a
UNIX-socket-based daemon (target: sub-100ms after first warm-up). The daemon
holds the Python interpreter + module cache alive between PreToolUse hook
invocations so the dominant cost — `import chameleon_mcp.tools` and friends —
amortizes across the whole session.

Architecture (POSIX-only):

  hook subprocess (bash + tiny python launcher)
      │
      ▼  UNIX domain socket at ${PLUGIN_DATA}/.daemon.sock
  [chameleon-mcp daemon process]
      └─ in-process dispatch to chameleon_mcp.tools.<method>

Protocol framing:
  - 4-byte big-endian uint32 length prefix.
  - Followed by a UTF-8 JSON payload of exactly that many bytes.
  - Both request and response use the same framing.
  - Hard cap: 1 MB per direction. Oversize frames are rejected with an
    "oversize" error and the connection is closed.

Lifecycle:
  - `start_daemon()` forks (double-fork) a background daemon process,
    waits for the socket to become connectable (≤ 3s), and returns the
    pid + socket path. If the recorded PID in the pidfile is dead, the
    stale socket + pidfile are cleaned up before respawn.
  - `stop_daemon()` sends SIGTERM, waits up to 5s, removes the socket
    + pidfile. Sends SIGKILL if the process is still alive at the deadline.
  - `is_daemon_alive()` reads the pidfile and probes the PID with
    `os.kill(pid, 0)`.
  - The daemon's main loop tracks `last_request_at` and shuts itself
    down after `IDLE_TIMEOUT_S` seconds of no activity. Configurable
    via the `CHAMELEON_DAEMON_IDLE_TIMEOUT` env var (tests use a low
    value to keep the loop snappy).

Fail-open contract:
  - The daemon is a performance optimization, not a correctness layer.
  - Every error path in the daemon returns a JSON error envelope rather
    than crashing the loop, and the client (`daemon_client`) returns
    `None` on any framing / socket / parse failure so the hook can fall
    back to the existing in-process path.

Single-threaded for v0.5: each connection is handled to completion
before the next is accepted. A thread pool can be retrofitted later;
the protocol is stateless per connection so the change is local.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Hard cap on a single frame. Keeps the daemon honest about its memory
# bounds — a misbehaving caller can't make us allocate gigabytes.
MAX_FRAME_BYTES = 1024 * 1024  # 1 MB

# Length-prefix struct (network byte order, unsigned 32-bit). 4 bytes.
_LEN_STRUCT = struct.Struct("!I")
_LEN_BYTES = _LEN_STRUCT.size

# Default idle-shutdown window. The daemon exits cleanly when no request
# arrives for this long. Tests override via env var to ~1s.
DEFAULT_IDLE_TIMEOUT_S = 600.0  # 10 minutes

# How long start_daemon() waits for the socket to become connectable.
_SPAWN_WAIT_SECONDS = 3.0

# Backlog for socket.listen(). Hooks are sequential per Claude Code session
# so we don't need a huge backlog; 16 is generous.
_LISTEN_BACKLOG = 16

# Logging: stderr only (no file journal in this MVP). Daemons launched via
# start_daemon() redirect stderr to a per-run log under PLUGIN_DATA so the
# user can `tail -f` if they need to debug.


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _plugin_data() -> Path:
    """Resolve the plugin data dir. Importing locally avoids a circular
    import from chameleon_mcp.profile.trust at module load time."""
    from chameleon_mcp.profile.trust import plugin_data_dir

    return plugin_data_dir()


def socket_path() -> Path:
    """UNIX socket path. Lives at the plugin data root so it's shared
    across all repos this user works on (the daemon is per-user, not
    per-repo). Created on demand by `start_daemon()`."""
    d = _plugin_data()
    d.mkdir(parents=True, exist_ok=True)
    return d / ".daemon.sock"


def pid_path() -> Path:
    """PID file location. Contains `<pid>\\n<socket_path>\\n`."""
    d = _plugin_data()
    d.mkdir(parents=True, exist_ok=True)
    return d / ".daemon.pid"


def log_path() -> Path:
    """Per-run daemon log. Truncated on each successful start."""
    d = _plugin_data()
    d.mkdir(parents=True, exist_ok=True)
    return d / ".daemon.log"


# ---------------------------------------------------------------------------
# Liveness helpers (mirrors bootstrap/transaction.py:_pid_alive)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """POSIX liveness check. Permission errors count as 'alive' (conservative)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno != errno.ESRCH


def _read_pidfile() -> tuple[int | None, str | None]:
    """Returns (pid, socket_path_str) or (None, None) on any parse failure."""
    pf = pid_path()
    try:
        raw = pf.read_text(encoding="utf-8").strip().splitlines()
    except (OSError, UnicodeDecodeError):
        return None, None
    if not raw:
        return None, None
    try:
        pid = int(raw[0])
    except (TypeError, ValueError):
        return None, None
    sock = raw[1] if len(raw) > 1 else None
    return pid, sock


def is_daemon_alive() -> bool:
    """True iff the pidfile points at a running process AND its socket exists."""
    pid, sock = _read_pidfile()
    if pid is None:
        return False
    if not _pid_alive(pid):
        return False
    if sock and not Path(sock).exists():
        return False
    return True


def _cleanup_stale() -> None:
    """Remove a stale pidfile + socket if the recorded PID is dead.

    Idempotent and best-effort: callers run this before bind() so a crashed
    previous daemon doesn't leave us unable to spawn.
    """
    pid, sock = _read_pidfile()
    if pid is not None and _pid_alive(pid):
        return  # not stale; leave it alone
    for p in (pid_path(), socket_path()):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    if sock:
        try:
            Path(sock).unlink()
        except (FileNotFoundError, OSError):
            pass


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    """Read exactly `n` bytes from the socket. Returns None on EOF / error."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(conn: socket.socket) -> bytes | None:
    """Read a single length-prefixed frame.

    Returns the raw payload bytes, or None on EOF / oversize / read error.
    """
    hdr = _recv_exact(conn, _LEN_BYTES)
    if hdr is None:
        return None
    (length,) = _LEN_STRUCT.unpack(hdr)
    if length == 0:
        return b""
    if length > MAX_FRAME_BYTES:
        # Drain and reject. We can't trust the rest of the stream.
        return None
    return _recv_exact(conn, length)


def send_frame(conn: socket.socket, payload: bytes) -> bool:
    """Write a single length-prefixed frame.

    Returns True on success, False on socket error or oversize.
    """
    if len(payload) > MAX_FRAME_BYTES:
        return False
    try:
        conn.sendall(_LEN_STRUCT.pack(len(payload)) + payload)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Request dispatch
# ---------------------------------------------------------------------------


def _dispatch(method: str, payload: dict) -> dict:
    """Map a method name to a chameleon_mcp.tools call.

    The handler set is intentionally small: only the hooks' hot path
    (`get_pattern_context`) plus a few useful query tools and the daemon's
    own status probe. Tools that mutate persistent state (bootstrap_repo,
    refresh_repo, teach_profile, trust_profile) are NOT exposed over the
    socket — they go through the MCP stdio interface where the caller
    has a stronger trust relationship with the server.
    """
    from chameleon_mcp import tools as _tools

    if method == "get_pattern_context":
        file_path = payload.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return {"error": "file_path required"}
        return _tools.get_pattern_context(file_path)

    if method == "detect_repo":
        file_path = payload.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return {"error": "file_path required"}
        return _tools.detect_repo(file_path)

    if method == "get_archetype":
        repo = payload.get("repo")
        file_path = payload.get("file_path")
        if not isinstance(repo, str) or not isinstance(file_path, str):
            return {"error": "repo + file_path required"}
        return _tools.get_archetype(repo, file_path)

    if method == "lint_file":
        repo = payload.get("repo")
        archetype = payload.get("archetype")
        content = payload.get("content", "")
        if not isinstance(repo, str) or not isinstance(archetype, str):
            return {"error": "repo + archetype required"}
        if not isinstance(content, str):
            return {"error": "content must be a string"}
        return _tools.lint_file(repo, archetype, content)

    if method == "ping":
        # Lightweight health probe used by tests + daemon_status().
        return {"ok": True, "ts": time.time()}

    return {"error": f"unknown method {method!r}"}


# ---------------------------------------------------------------------------
# Daemon state (process-local)
# ---------------------------------------------------------------------------


class _DaemonState:
    """Mutable state held by the main loop.

    Kept as an object (not module globals) so tests can spin up an
    isolated `serve_forever()` in a thread without leaking timestamps
    into the next test.
    """

    def __init__(self, idle_timeout_s: float) -> None:
        self.started_at = time.time()
        self.last_request_at = time.time()
        self.idle_timeout_s = idle_timeout_s
        self.request_count = 0
        self.shutdown_requested = False

    def mark_request(self) -> None:
        self.last_request_at = time.time()
        self.request_count += 1


# ---------------------------------------------------------------------------
# Daemon main loop
# ---------------------------------------------------------------------------


def _handle_connection(
    conn: socket.socket, state: _DaemonState, dispatcher: Callable[[str, dict], dict]
) -> None:
    """Read one request, dispatch, write one response, close.

    Single-request-per-connection keeps framing trivial and lets us treat
    every connection as a stateless RPC — no pipelining, no half-closed
    states to reason about.
    """
    try:
        frame = recv_frame(conn)
        if frame is None:
            # Client closed early, oversize frame, or socket error.
            # Try to surface an "oversize" envelope when possible so the
            # client can distinguish a transient hangup from a protocol
            # violation; on socket error, send_frame will just fail.
            send_frame(conn, json.dumps({"error": "oversize_or_disconnect"}).encode("utf-8"))
            return
        try:
            request = json.loads(frame.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            send_frame(conn, json.dumps({"error": f"invalid_json: {exc.__class__.__name__}"}).encode("utf-8"))
            return
        method = request.get("method") if isinstance(request, dict) else None
        payload = request.get("payload") if isinstance(request, dict) else None
        if not isinstance(method, str) or not isinstance(payload, dict):
            send_frame(conn, json.dumps({"error": "missing method or payload"}).encode("utf-8"))
            return

        state.mark_request()
        try:
            result = dispatcher(method, payload)
        except Exception as exc:  # noqa: BLE001 — daemon must not die on tool errors
            # Surface the error to the client without crashing the loop.
            sys.stderr.write(
                f"[chameleon-daemon] dispatch error in {method!r}: "
                f"{exc.__class__.__name__}: {exc}\n"
            )
            traceback.print_exc(file=sys.stderr)
            result = {"error": f"dispatch_failed: {exc.__class__.__name__}"}
        try:
            payload_bytes = json.dumps(result, default=str).encode("utf-8")
        except (TypeError, ValueError):
            payload_bytes = json.dumps({"error": "result_not_serializable"}).encode("utf-8")
        send_frame(conn, payload_bytes)
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


def serve_forever(
    sock: socket.socket,
    state: _DaemonState,
    dispatcher: Callable[[str, dict], dict] = _dispatch,
) -> None:
    """Daemon's accept loop. Returns when shutdown is requested or idle.

    The socket must already be bound + listening. Accept timeout is 1s
    so the idle-shutdown check runs even when no clients connect.
    """
    sock.settimeout(1.0)
    while not state.shutdown_requested:
        # Idle-shutdown gate. We compare against the LAST request time so
        # a busy daemon stays up even when the loop iterates without an
        # accept(). request_count == 0 + idle window elapsed still triggers
        # shutdown — a daemon that nobody ever connected to is a leak.
        if (time.time() - state.last_request_at) > state.idle_timeout_s:
            sys.stderr.write(
                f"[chameleon-daemon] idle for {state.idle_timeout_s}s, shutting down\n"
            )
            return
        try:
            conn, _addr = sock.accept()
        except TimeoutError:
            continue
        except OSError as e:
            if e.errno in (errno.EBADF, errno.EINVAL):
                # Socket was closed underneath us (signal handler did the
                # right thing). Exit cleanly.
                return
            sys.stderr.write(f"[chameleon-daemon] accept error: {e}\n")
            continue
        _handle_connection(conn, state, dispatcher)


# ---------------------------------------------------------------------------
# In-process entry point (run inside the forked child)
# ---------------------------------------------------------------------------


def _install_signal_handlers(state: _DaemonState) -> None:
    """Wire SIGTERM/SIGINT to flip the shutdown flag.

    The accept loop has a 1s timeout so the flag is observed within one
    second of the signal arriving — no need for socket.shutdown() on the
    listening socket.
    """

    def _handler(signum: int, _frame) -> None:
        sys.stderr.write(f"[chameleon-daemon] received signal {signum}, shutting down\n")
        state.shutdown_requested = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (OSError, ValueError):
            # Threads can't install signal handlers — that's fine; the
            # test harness drives shutdown via `state.shutdown_requested`
            # directly.
            pass


def _idle_timeout_from_env() -> float:
    raw = os.environ.get("CHAMELEON_DAEMON_IDLE_TIMEOUT")
    if not raw:
        return DEFAULT_IDLE_TIMEOUT_S
    try:
        v = float(raw)
        return v if v > 0 else DEFAULT_IDLE_TIMEOUT_S
    except (TypeError, ValueError):
        return DEFAULT_IDLE_TIMEOUT_S


def run_daemon() -> int:
    """In-process daemon entry point.

    Called from inside the forked child by `start_daemon()`. The parent's
    role is to write the pidfile + verify the socket comes up.
    """
    sock_path = socket_path()
    # Defensive: a previous unclean exit may have left the socket file behind.
    try:
        sock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        sys.stderr.write(f"[chameleon-daemon] cannot remove stale socket: {e}\n")
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_path))
    # Owner-only access — the daemon is per-user, and the socket lives
    # under ~/.local/share which is already 0700 on most systems, but
    # belt-and-suspenders never hurt.
    try:
        os.chmod(sock_path, 0o600)
    except OSError:
        pass
    sock.listen(_LISTEN_BACKLOG)

    state = _DaemonState(idle_timeout_s=_idle_timeout_from_env())
    _install_signal_handlers(state)

    sys.stderr.write(
        f"[chameleon-daemon] listening on {sock_path} "
        f"(pid={os.getpid()}, idle_timeout={state.idle_timeout_s}s)\n"
    )

    try:
        serve_forever(sock, state)
    finally:
        try:
            sock.close()
        except OSError:
            pass
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            pid_path().unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return 0


# ---------------------------------------------------------------------------
# Out-of-process daemon spawn
# ---------------------------------------------------------------------------


def _write_pidfile(pid: int, sock_path: Path) -> None:
    pf = pid_path()
    tmp = pf.with_suffix(".pid.tmp")
    tmp.write_text(f"{pid}\n{sock_path}\n", encoding="utf-8")
    os.replace(tmp, pf)


def _wait_for_socket(sock_path: Path, deadline: float) -> bool:
    """Poll until the socket accepts a connection or the deadline passes."""
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.5)
                probe.connect(str(sock_path))
                probe.close()
                return True
            except OSError:
                pass
        time.sleep(0.05)
    return False


def start_daemon(*, force: bool = False) -> dict:
    """Spawn the daemon if it isn't already running.

    Returns a status dict:
      { "status": "already_running" | "started" | "failed",
        "pid": int | None,
        "socket": str,
        "error": str (only when status=failed) }

    Strategy: double-fork to detach from the calling process group, exec
    `python -m chameleon_mcp.daemon` so the child has a clean process
    image (no inherited file descriptors or signal masks from the hook).

    `force=True` skips the "already running" early return — used by
    `start_daemon()` callers that just received a SIGTERM error from the
    existing daemon and want to respawn.
    """
    sock_path = socket_path()

    if not force and is_daemon_alive():
        pid, _ = _read_pidfile()
        return {"status": "already_running", "pid": pid, "socket": str(sock_path)}

    _cleanup_stale()

    # Locate a Python interpreter. Prefer the same one the caller is using
    # so we don't accidentally exec into a sibling venv that doesn't have
    # chameleon_mcp installed.
    python = sys.executable or "python3"

    # Open the log file BEFORE forking so the child can inherit the fd.
    try:
        log_fd = os.open(str(log_path()), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError:
        log_fd = None

    # First fork: detaches from the parent's process group.
    try:
        first_pid = os.fork()
    except OSError as e:
        if log_fd is not None:
            os.close(log_fd)
        return {"status": "failed", "pid": None, "socket": str(sock_path), "error": str(e)}

    if first_pid > 0:
        # Original caller. Reap the intermediate child immediately and
        # wait for the grandchild's socket.
        try:
            os.waitpid(first_pid, 0)
        except OSError:
            pass
        if log_fd is not None:
            os.close(log_fd)
        deadline = time.monotonic() + _SPAWN_WAIT_SECONDS
        if not _wait_for_socket(sock_path, deadline):
            return {
                "status": "failed",
                "pid": None,
                "socket": str(sock_path),
                "error": "socket did not appear within spawn window",
            }
        pid, _ = _read_pidfile()
        return {"status": "started", "pid": pid, "socket": str(sock_path)}

    # ---- intermediate child ----
    try:
        os.setsid()
    except OSError:
        pass
    try:
        second_pid = os.fork()
    except OSError:
        os._exit(1)

    if second_pid > 0:
        # Intermediate child: exit immediately so the grandchild is
        # reparented to init / launchd.
        os._exit(0)

    # ---- grandchild (the daemon) ----
    # Redirect stdio so we don't keep the calling terminal busy.
    try:
        null_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null_fd, 0)
        os.close(null_fd)
    except OSError:
        pass
    if log_fd is not None:
        try:
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            os.close(log_fd)
        except OSError:
            pass

    # Write the pidfile from inside the grandchild — that's the PID the
    # client will SIGTERM later.
    try:
        _write_pidfile(os.getpid(), sock_path)
    except OSError as e:
        sys.stderr.write(f"[chameleon-daemon] cannot write pidfile: {e}\n")
        os._exit(1)

    # Re-exec into a clean python process. This drops any state the parent
    # may have leaked (open file handles to the user's repo, loaded MCP
    # modules tied to the parent's working directory, etc.) and gives us
    # a deterministic startup that's easy to test.
    try:
        os.execlp(python, python, "-m", "chameleon_mcp.daemon")
    except OSError as e:
        sys.stderr.write(f"[chameleon-daemon] exec failed: {e}\n")
        os._exit(1)
    os._exit(0)  # unreachable


def stop_daemon(*, timeout: float = 5.0) -> dict:
    """Send SIGTERM to the running daemon and wait for it to exit.

    Returns:
      { "status": "stopped" | "not_running" | "timeout" | "failed",
        "pid": int | null }

    If the daemon is still alive after `timeout` seconds, SIGKILL it and
    return "timeout" (the user gets to see that the graceful path didn't
    work, which is useful diagnostic signal).
    """
    pid, sock = _read_pidfile()
    if pid is None or not _pid_alive(pid):
        # Best-effort cleanup of leftover artifacts.
        for p in (pid_path(), socket_path()):
            try:
                p.unlink()
            except (FileNotFoundError, OSError):
                pass
        return {"status": "not_running", "pid": None}

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return {"status": "failed", "pid": pid, "error": str(e)}

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.05)
    else:
        # Graceful path didn't work; escalate.
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        # Brief grace period for the kernel to reap.
        time.sleep(0.1)
        # Clean up artifacts ourselves since the daemon couldn't.
        for p in (pid_path(), socket_path()):
            try:
                p.unlink()
            except (FileNotFoundError, OSError):
                pass
        return {"status": "timeout", "pid": pid}

    # SIGTERM path successful. The daemon's finally block should have
    # removed the pidfile + socket, but defend against an exit before
    # those cleanups.
    for p in (pid_path(), socket_path()):
        try:
            p.unlink()
        except (FileNotFoundError, OSError):
            pass
    if sock and Path(sock).exists():
        try:
            Path(sock).unlink()
        except (FileNotFoundError, OSError):
            pass
    return {"status": "stopped", "pid": pid}


def daemon_info() -> dict:
    """Read-only status snapshot — no side effects. Used by daemon_status()."""
    pid, sock = _read_pidfile()
    alive = pid is not None and _pid_alive(pid)
    if not alive:
        return {
            "alive": False,
            "pid": None,
            "socket": str(socket_path()),
            "uptime_s": None,
        }
    # Process start time → uptime. /proc isn't available on macOS, so we
    # fall back to mtime of the pidfile (close enough; pidfile is written
    # once at daemon birth).
    pf = pid_path()
    try:
        started_at = pf.stat().st_mtime
        uptime_s = max(0.0, time.time() - started_at)
    except OSError:
        uptime_s = None
    return {
        "alive": True,
        "pid": pid,
        "socket": sock or str(socket_path()),
        "uptime_s": uptime_s,
    }


# ---------------------------------------------------------------------------
# Hook-side helper for spawning the daemon out-of-process
# ---------------------------------------------------------------------------


def ensure_daemon_async() -> None:
    """Fire-and-forget: spawn the daemon if it isn't running.

    Called from preflight-and-advise's first invocation. The hook must
    not block on daemon spawn (that would defeat the whole point of the
    optimization).

    Implementation note: an earlier version delegated to a background
    thread that called ``start_daemon()`` directly. ``start_daemon`` does
    a double-fork; on macOS, ``os.fork()`` from inside a Python thread
    can hang the parent for seconds (libc/Cocoa locks held across the
    fork boundary are not released cleanly when the child briefly held
    them). That manifested as ~3 to 10 percent of hook calls hitting
    the bash ``timeout 2`` ceiling and fail-opening.

    The fix is to use ``subprocess.Popen`` with ``start_new_session=True``
    so the OS performs ``fork()`` + ``execve()`` atomically, sidestepping
    the threaded-fork landmine entirely. The freshly-exec'd Python
    interpreter then calls ``start_daemon`` from a clean single-threaded
    process where the double-fork is safe.

    Subsequent hook calls in the same session will find the daemon ready
    and route through the socket.
    """
    if is_daemon_alive():
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "chameleon_mcp.daemon", "start"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001 — never raise from the spawn helper
        pass


# ---------------------------------------------------------------------------
# Module entry point: `python -m chameleon_mcp.daemon`
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return run_daemon()
    cmd = args[0]
    if cmd == "start":
        result = start_daemon()
        print(json.dumps(result))
        return 0 if result["status"] in ("started", "already_running") else 1
    if cmd == "stop":
        result = stop_daemon()
        print(json.dumps(result))
        return 0
    if cmd == "status":
        print(json.dumps(daemon_info()))
        return 0
    sys.stderr.write(f"daemon.py: unknown command {cmd!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
