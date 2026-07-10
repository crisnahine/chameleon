"""Long-lived chameleon-mcp daemon (Phase 4.5).

Replaces the subprocess-per-call hook (200-500ms warm latency) with a
UNIX-socket-based daemon (target: sub-100ms after first warm-up). The daemon
holds the Python interpreter + module cache alive between PreToolUse hook
invocations so the dominant cost — `import chameleon_mcp.tools` and friends —
amortizes across the whole session.

POSIX-only: the IPC layer uses AF_UNIX sockets, which do not exist on Windows.
Every entry point guards on `_af_unix_available()` and degrades gracefully when
the socket family is missing: the daemon refuses to start, callers fall back to
the in-process path, and nothing raises. The daemon is a performance layer, not
a correctness layer, so Windows simply runs without it.

Architecture (POSIX-only):

  hook subprocess (bash + tiny python launcher)
      │
      ▼  UNIX domain socket at <tmpdir>/chameleon-<uid>/d-<hash>.sock
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
  - `is_daemon_alive()` reads the pidfile, probes the PID with
    `os.kill(pid, 0)`, and requires the recorded socket to accept a
    connection (a live PID alone can be a recycled, unrelated process).
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

Single-threaded: each connection is handled to completion
before the next is accepted. A thread pool can be retrofitted later;
the protocol is stateless per connection so the change is local.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import signal
import socket
import stat as stat_mod
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path

try:
    import fcntl
except ImportError:
    # POSIX-only: Windows has no fcntl. The daemon is unavailable there anyway
    # (AF_UNIX is also absent), so leave fcntl=None and let the AF_UNIX guards
    # short-circuit before any flock call would run.
    fcntl = None  # type: ignore[assignment]

MAX_FRAME_BYTES = 1024 * 1024

_LEN_STRUCT = struct.Struct("!I")
_LEN_BYTES = _LEN_STRUCT.size

DEFAULT_IDLE_TIMEOUT_S = 600.0

# A single accepted connection that goes silent (buggy client, paused debugger,
# `nc -U`) must not wedge the single-threaded accept loop. Accepted sockets do
# NOT inherit the listening socket's timeout, so set one explicitly; recv/send
# then raise (a subclass of OSError) and the connection is dropped. The client's
# own deadline (~1.5s in daemon_client) is shorter, so this only bounds the
# server side.
CONN_RECV_TIMEOUT_S = 5.0

_SPAWN_WAIT_SECONDS = 3.0

_LISTEN_BACKLOG = 128


def _af_unix_available() -> bool:
    """True iff this platform supports AF_UNIX domain sockets.

    Windows lacks `socket.AF_UNIX`. Every socket entry point checks this so the
    daemon degrades to a no-op there instead of raising AttributeError. Wrapped
    in a function so tests can simulate the Windows path with a single patch.
    """
    return hasattr(socket, "AF_UNIX")


def _plugin_data() -> Path:
    """Resolve the plugin data dir. Importing locally avoids a circular
    import from chameleon_mcp.profile.trust at module load time."""
    from chameleon_mcp.profile.trust import plugin_data_dir

    return plugin_data_dir()


_CODE_FINGERPRINT_CACHE: str | None = None


def _code_fingerprint(pkg_dir: Path | None = None) -> str:
    """Short content hash of the chameleon_mcp source tree.

    Folded into the version tag so a code-only upgrade (git pull, cherry-pick,
    /plugin update) that forgot to bump the version string still produces a
    distinct daemon identity. Without this, two builds with the same declared
    version share one socket name and a new-code hook would reuse the stale
    old-code daemon for up to one idle window.

    Identity is the bytes of each .py file, not its mtime. Hashing mtimes
    collides in frozen-timestamp environments (Docker image layers, cached
    filesystems, `git checkout` and `--recurse-submodules` which do not
    preserve commit times): two genuinely different code versions can carry
    identical mtimes and would then share a socket. Content hashing rotates the
    tag whenever a byte changes and stays stable across a pure `touch`.
    Returns "0" on any error so the tag computation never fails.

    Memoized per process for the real package dir: a running interpreter's own
    source can't change under it, so the read+hash runs once. A NEW process
    started after an upgrade computes a fresh fingerprint from the new bytes,
    which is exactly the rotation we want. Kept off the hot path's repeat cost
    (socket_path is called several times per hook). An explicit pkg_dir (tests)
    bypasses the cache so it neither reads nor pollutes the memoized value.
    """
    global _CODE_FINGERPRINT_CACHE
    use_cache = pkg_dir is None
    if use_cache and _CODE_FINGERPRINT_CACHE is not None:
        return _CODE_FINGERPRINT_CACHE
    try:
        root = pkg_dir if pkg_dir is not None else Path(__file__).resolve().parent
        hasher = hashlib.sha256()
        count = 0
        for p in sorted(root.rglob("*.py")):
            try:
                data = p.read_bytes()
            except OSError:
                continue
            # Name + size + content so a rename or split is also a rotation,
            # and the per-file boundary can't be forged by concatenation.
            hasher.update(p.name.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(str(len(data)).encode("ascii"))
            hasher.update(b"\0")
            hasher.update(data)
            count += 1
        result = "0" if count == 0 else hasher.hexdigest()[:8]
    except Exception:  # noqa: BLE001 - fingerprint must never break daemon paths
        result = "0"
    if use_cache:
        _CODE_FINGERPRINT_CACHE = result
    return result


def _version_tag() -> str:
    """Filesystem-safe identifier for the running plugin build.

    The socket/pidfile names are scoped by this so a newer plugin build never
    connects to a daemon spawned by an older build: the daemon holds the
    advisory code (lint engine, conventions) in memory, so a reused old daemon
    would serve stale logic for up to one idle window after an upgrade. Each
    version gets its own socket; the prior version's daemon idle-exits on its
    own and removes its files.

    The declared version is combined with a short source fingerprint so a
    code-only change that did not bump the version still rotates the socket
    name. This closes the gap where a forgotten version bump would let a
    new-code hook reuse the stale old-code daemon.
    """
    try:
        from chameleon_mcp import __version__ as v
    except Exception:  # noqa: BLE001 - version lookup must never break daemon paths
        v = "0"
    base = re.sub(r"[^0-9A-Za-z._-]", "_", str(v)) or "0"
    fp = _code_fingerprint()
    return f"{base}-{fp}" if fp and fp != "0" else base


# bind() rejects an AF_UNIX path longer than sun_path: ~104 bytes on macOS,
# ~108 on Linux (including the trailing NUL). Paths at or under this budget
# are safe on both.
_SUN_PATH_SAFE_BYTES = 100


def _socket_tmp_base() -> Path | None:
    """Short per-user base dir for the daemon socket, or None where unusable.

    `<tmpdir>/chameleon-<uid>` is short on every mainstream platform, so the
    socket path stays under the sun_path cap even when the plugin data dir
    resolves deep. Returns None when the platform has no uid (Windows — the
    daemon is AF_UNIX-only and disabled there anyway)."""
    bases = _socket_tmp_bases()
    return bases[0] if bases else None


def _socket_tmp_bases() -> list[Path]:
    """Candidate socket base dirs, preferred first.

    `tempfile.gettempdir()` honors TMPDIR, which can itself be deep (test
    harnesses point it inside an isolated run dir) — deep enough that even the
    relocated socket blows the sun_path cap. Literal `/tmp` is the classic
    POSIX fallback for exactly this (ssh-agent, postgres); our per-user
    subdir is 0700-hardened against the shared-tmp risks. Deduplicated, so on
    a default macOS/Linux setup this is usually a single entry."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return []
    bases: list[Path] = []
    try:
        uid = getuid()
        bases.append(Path(tempfile.gettempdir()) / f"chameleon-{uid}")
        fallback = Path("/tmp") / f"chameleon-{uid}"
        if fallback not in bases and Path("/tmp").is_dir():
            bases.append(fallback)
    except Exception:  # noqa: BLE001 - path resolution must never break daemon paths
        pass
    return bases


def _ensure_private_socket_dir(d: Path) -> bool:
    """Create/verify the socket dir as user-private. False means: do not use.

    The base lives under a shared, world-writable tmp dir, so another local
    user can pre-create (squat) the path or plant a symlink there; the sticky
    bit stops us from removing it. Require a real directory (lstat, so a
    symlink is seen as itself), owned by this uid, with no group/other bits
    (tightened to 0700 if the owner check passes). Anything else is refused
    and the caller falls back to the data-dir path. Never raises."""
    try:
        d.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        return False
    except Exception:  # noqa: BLE001 - never break socket resolution
        return False
    try:
        st = os.lstat(str(d))
    except OSError:
        return False
    if not stat_mod.S_ISDIR(st.st_mode):
        return False
    if st.st_uid != os.getuid():
        return False
    if st.st_mode & 0o077:
        from chameleon_mcp.plugin_paths import secure_chmod

        # mkdir's mode is masked by umask; tighten explicitly.
        if not secure_chmod(d, 0o700):
            return False
        try:
            st = os.lstat(str(d))
        except OSError:
            return False
        if st.st_mode & 0o077:
            return False
    return True


def socket_path_for(data_dir: Path, version_tag: str, tmp_base: Path | None = None) -> Path:
    """Pure socket-path computation — the single source of truth for both the
    daemon and daemon_client, so the two sides of the wire always agree.

    The daemon is per-user (shared across all repos this user works on), so
    its identity is (data dir, version tag): the version tag keeps a newer
    build off an older build's daemon, and the data dir keeps two
    CHAMELEON_PLUGIN_DATA universes from cross-talking now that the socket no
    longer lives inside the data dir itself. Both are folded into a short
    hash because the full strings would blow the sun_path budget the
    relocation exists to respect.

    Falls back to the legacy `<data>/.daemon-<version_tag>.sock` when there is
    no usable tmp base or when even the tmp-based path would exceed the
    sun_path budget (pathologically deep TMPDIR); bind() then fails with the
    existing fail-open diagnostics instead of crashing here."""
    legacy = data_dir / f".daemon-{version_tag}.sock"
    if tmp_base is None:
        return legacy
    digest = hashlib.sha256(os.fsencode(str(data_dir)) + b"\0" + version_tag.encode("utf-8"))
    candidate = tmp_base / f"d-{digest.hexdigest()[:12]}.sock"
    if len(os.fsencode(str(candidate))) > _SUN_PATH_SAFE_BYTES:
        return legacy
    return candidate


def socket_path() -> Path:
    """UNIX socket path. The daemon is per-user, not per-repo, so one socket
    serves every repo; the name is keyed on plugin version + data dir. The
    socket lives under a short per-user tmp dir — not the plugin data dir —
    because AF_UNIX caps sun_path at ~104 bytes and a deep data dir would make
    every bind fail, silently disabling the fast path. Created on demand by
    `start_daemon()`; pidfile and log stay in the data dir (only the socket
    path is length-limited)."""
    d = _plugin_data()
    d.mkdir(parents=True, exist_ok=True)
    tag = _version_tag()
    # First candidate base whose socket path fits the sun_path budget AND
    # whose dir can be secured wins; the client runs the same deterministic
    # walk, so both sides of the wire agree.
    for base in _socket_tmp_bases():
        p = socket_path_for(d, tag, base)
        if p.parent == d:
            continue  # this base overflows the budget; try the next
        if _ensure_private_socket_dir(p.parent):
            return p
    return d / f".daemon-{tag}.sock"


def pid_path() -> Path:
    """PID file location. Contains `<pid>\\n<socket_path>\\n`. Version-scoped
    to match :func:`socket_path`."""
    d = _plugin_data()
    d.mkdir(parents=True, exist_ok=True)
    return d / f".daemon-{_version_tag()}.pid"


def log_path() -> Path:
    """Per-run daemon log. Truncated on each successful start."""
    d = _plugin_data()
    d.mkdir(parents=True, exist_ok=True)
    return d / ".daemon.log"


def _flock_reliable() -> bool:
    """True iff `fcntl.flock` can be trusted for the stop-daemon recycle guard.

    The guard concludes "no live daemon holds the pidfile" when it ACQUIRES the
    flock. That conclusion is only safe where flock is advisory-but-honest:
    POSIX local filesystems. On Windows there is no fcntl at all, and on some
    NFS mounts flock can spuriously succeed even while another host holds the
    lock. In those cases a successful acquire does NOT prove the daemon is dead,
    so we must not act on it (acting would delete a live daemon's pidfile).

    NFS detection is best-effort and intentionally conservative: when we cannot
    tell, we keep the trustworthy POSIX-local fast path. The home/plugin-data
    dir is local in the common case; operators on NFS can point
    CHAMELEON_PLUGIN_DATA at a local dir.
    """
    if fcntl is None:
        return False
    try:
        fstype = _plugin_data_fstype()
    except Exception:  # noqa: BLE001 - never let a stat failure break stop_daemon
        return True
    return fstype not in _UNRELIABLE_FLOCK_FSTYPES


_UNRELIABLE_FLOCK_FSTYPES = frozenset({"nfs", "nfs4", "smbfs", "cifs"})


def _plugin_data_fstype() -> str | None:
    """Best-effort filesystem type for the plugin data dir, lowercased.

    Returns None when it cannot be determined (no statvfs f_fstypename, or any
    error). Only used to decide whether flock is trustworthy, so an unknown type
    is treated as the trustworthy local case by the caller.
    """
    try:
        st = os.statvfs(str(_plugin_data()))
    except (OSError, AttributeError):
        return None
    fstype = getattr(st, "f_fstypename", None)
    if isinstance(fstype, bytes):
        fstype = fstype.decode("utf-8", "ignore")
    return fstype.lower() if isinstance(fstype, str) else None


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


def _socket_connectable(sock_path: str) -> bool:
    """True iff a short-timeout AF_UNIX connect to ``sock_path`` succeeds.

    A bound, listening daemon accepts the connect; a leftover socket file with
    no listener, a wrong-type path, or a missing AF_UNIX family all fail. Any
    error means not connectable, so callers can treat a False as "no daemon
    here". Fail-safe: never raises."""
    if not _af_unix_available():
        return False
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(sock_path)
            return True
        finally:
            probe.close()
    except OSError:
        return False


def is_daemon_alive() -> bool:
    """True iff the pidfile points at a running process AND its recorded socket
    accepts a connection.

    A live PID alone is not enough: PIDs are recycled, so an unrelated live
    process can inherit a dead daemon's PID. A real daemon always records its
    socket and keeps it bound, so we require an actual connect to that socket.
    A pidfile with no socket line therefore cannot be our daemon."""
    pid, sock = _read_pidfile()
    if pid is None:
        return False
    if not _pid_alive(pid):
        return False
    if not sock:
        return False
    return _socket_connectable(sock)


def _cleanup_stale() -> None:
    """Remove a stale pidfile + socket if the recorded PID is dead.

    Idempotent and best-effort: callers run this before bind() so a crashed
    previous daemon doesn't leave us unable to spawn.
    """
    pid, sock = _read_pidfile()
    if pid is not None and _pid_alive(pid):
        return
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
    _sweep_orphan_version_files()


def _sweep_orphan_version_files() -> None:
    """Remove `.daemon-<ver>.{pid,sock}` left by a dead daemon of any version.

    Version-scoped socket names mean each upgrade introduces a new filename; a
    daemon that crashed (rather than idle-exiting cleanly) leaves its files
    behind. This best-effort sweep drops the ones whose recorded PID is no
    longer alive, so PLUGIN_DATA doesn't accumulate dead sockets over many
    upgrades. A live daemon's files are always left untouched.
    """
    try:
        data_dir = _plugin_data()
    except Exception:  # noqa: BLE001
        return
    try:
        pidfiles = list(data_dir.glob(".daemon-*.pid"))
    except OSError:
        return
    for pf in pidfiles:
        raw: list[str] = []
        try:
            raw = pf.read_text(encoding="utf-8").strip().splitlines()
            pid = int(raw[0]) if raw else None
        except (OSError, UnicodeDecodeError, ValueError):
            pid = None
        # Only reap when the PID is parseable AND confirmed dead. An empty or
        # half-written pidfile (a daemon mid-startup, before it writes its pid)
        # must be left alone, or the sweep would delete a live daemon's socket
        # in that window.
        if pid is None or _pid_alive(pid):
            continue
        # The socket normally lives in the per-user tmp dir at the path the
        # pidfile records; the data-dir sibling covers daemons from builds
        # that kept the socket next to the pidfile.
        targets = [pf, pf.with_suffix(".sock")]
        if len(raw) > 1 and raw[1]:
            targets.append(Path(raw[1]))
        for p in targets:
            try:
                p.unlink()
            except (FileNotFoundError, OSError):
                pass


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
        file_path = payload.get("file_path")
        content_truncated = payload.get("content_truncated")
        if not isinstance(repo, str) or not isinstance(archetype, str):
            return {"error": "repo + archetype required"}
        if not isinstance(content, str):
            return {"error": "content must be a string"}
        return _tools.lint_file(
            repo,
            archetype,
            content,
            file_path=file_path if isinstance(file_path, str) else None,
            # A caller that already capped an oversized file to its prefix flags
            # it so the removed-export check is skipped; drop a non-bool.
            content_truncated=content_truncated if isinstance(content_truncated, bool) else None,
        )

    if method == "invalidate_cache":
        from chameleon_mcp.profile.loader import clear_profile_cache

        clear_profile_cache()
        return {"ok": True, "cleared": True}

    # ping never reaches the dispatcher: _handle_connection answers it directly
    # with the full status reply (ts + last_request_at + request_count) and
    # returns before dispatching, so it is intentionally absent here.

    return {"error": f"unknown method {method!r}"}


class _AcceptBackoff:
    """Bounded backoff and rate-limited logging for accept() errors.

    A persistent accept() failure (classically EMFILE from fd exhaustion, but
    also EINTR storms or a transient OS resource limit) would otherwise hot-spin
    the single accept loop: a fixed 0.1s sleep still burns ~10 stderr lines per
    second, which fills the daemon log and any /tmp budget within minutes.

    Two independent bounds:
    - Sleep grows geometrically from BASE_SLEEP_S and caps at MAX_SLEEP_S, so a
      stuck condition costs at most one wake per MAX_SLEEP_S while a one-off
      error still recovers within a few hundred ms. reset() is called after any
      successful accept so a later isolated error starts fresh.
    - should_log() emits at most one line per error type per LOG_INTERVAL_S, and
      always logs the first sighting of a new error type so a changing failure
      mode is never hidden.
    """

    BASE_SLEEP_S = 0.05
    MAX_SLEEP_S = 2.0
    LOG_INTERVAL_S = 5.0

    def __init__(self) -> None:
        self._consecutive = 0
        self._last_log_mono: dict[str, float] = {}

    def observe(self, _error_key: str) -> float:
        """Record an error and return how long to sleep before retrying."""
        sleep_s = self.BASE_SLEEP_S * (2**self._consecutive)
        if sleep_s >= self.MAX_SLEEP_S:
            sleep_s = self.MAX_SLEEP_S
        else:
            self._consecutive += 1
        return sleep_s

    def reset(self) -> None:
        """Clear the run of consecutive errors after a successful accept."""
        self._consecutive = 0

    def should_log(self, error_key: str) -> bool:
        """True iff this error type should be written to stderr now.

        Logs the first sighting of an error type immediately, then suppresses
        repeats of that type until LOG_INTERVAL_S has elapsed.
        """
        now = time.monotonic()
        last = self._last_log_mono.get(error_key)
        if last is not None and (now - last) < self.LOG_INTERVAL_S:
            return False
        self._last_log_mono[error_key] = now
        return True


class _DaemonState:
    """Mutable state held by the main loop.

    Kept as an object (not module globals) so tests can spin up an
    isolated `serve_forever()` in a thread without leaking timestamps
    into the next test.
    """

    def __init__(self, idle_timeout_s: float) -> None:
        self.started_at = time.time()
        # None until the first real request is served (mark_request). ping and
        # daemon_status surface this verbatim, and their contract is "None when
        # the daemon hasn't served any requests yet" -- seeding it with the start
        # time reported a phantom last-request before any work was done. The idle
        # decision uses last_request_mono below, not this, so it is unaffected.
        self.last_request_at: float | None = None
        # Monotonic clock drives the idle decision so a wall-clock jump
        # (NTP step, manual set) can't make the daemon hang or reap early.
        self.last_request_mono = time.monotonic()
        self.idle_timeout_s = idle_timeout_s
        self.request_count = 0
        self.shutdown_requested = False

    def mark_request(self) -> None:
        self.last_request_at = time.time()
        self.last_request_mono = time.monotonic()
        self.request_count += 1


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
            send_frame(conn, json.dumps({"error": "oversize_or_disconnect"}).encode("utf-8"))
            return
        try:
            request = json.loads(frame.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            send_frame(
                conn,
                json.dumps({"error": f"invalid_json: {exc.__class__.__name__}"}).encode("utf-8"),
            )
            return
        method = request.get("method") if isinstance(request, dict) else None
        payload = request.get("payload") if isinstance(request, dict) else None
        if not isinstance(method, str) or not isinstance(payload, dict):
            send_frame(conn, json.dumps({"error": "missing method or payload"}).encode("utf-8"))
            return

        # ping is a pure status query: report the real last-request time and
        # do NOT mark_request (so /chameleon-status doesn't reset the idle timer).
        if method == "ping":
            send_frame(
                conn,
                json.dumps(
                    {
                        "ok": True,
                        "ts": time.time(),
                        "last_request_at": state.last_request_at,
                        "request_count": state.request_count,
                    }
                ).encode("utf-8"),
            )
            return

        state.mark_request()
        try:
            result = dispatcher(method, payload)
        except Exception as exc:  # noqa: BLE001 — daemon must not die on tool errors
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
    backoff = _AcceptBackoff()
    while not state.shutdown_requested:
        if (time.monotonic() - state.last_request_mono) > state.idle_timeout_s:
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
                return
            # A persistent error (e.g. EMFILE fd pressure) must not hot-spin the
            # loop or flood the log. Sleep grows and caps; logging is throttled
            # per error type so a changing failure mode still surfaces.
            error_key = errno.errorcode.get(e.errno, str(e.errno))
            if backoff.should_log(error_key):
                sys.stderr.write(f"[chameleon-daemon] accept error: {e}\n")
            time.sleep(backoff.observe(error_key))
            continue
        backoff.reset()
        # Accepted sockets are blocking by default; bound recv/send so one
        # stalled client can't wedge the loop for the rest of the session.
        try:
            conn.settimeout(CONN_RECV_TIMEOUT_S)
        except OSError:
            try:
                conn.close()
            except OSError:
                pass
            continue
        _handle_connection(conn, state, dispatcher)


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
    if not _af_unix_available():
        sys.stderr.write(
            "[chameleon-daemon] AF_UNIX unavailable on this platform; "
            "daemon disabled (hooks use the in-process path)\n"
        )
        return 1
    sock_path = socket_path()
    try:
        from chameleon_mcp.plugin_paths import ensure_plugin_data_dir

        ensure_plugin_data_dir()
    except Exception:
        pass
    try:
        sock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        sys.stderr.write(f"[chameleon-daemon] cannot remove stale socket: {e}\n")
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(sock_path))
    except OSError as e:
        # AF_UNIX sun_path is length-capped (~104 bytes on macOS, ~108 on Linux).
        # A long CHAMELEON_PLUGIN_DATA pushes the socket path past the limit and
        # bind fails with ENAMETOOLONG. Not fatal: the parent reads a non-zero
        # exit as "daemon unavailable" and every caller falls back to the
        # in-process path. Emit an actionable one-liner instead of a raw
        # traceback so the lost speedup is diagnosable.
        try:
            sock.close()
        except OSError:
            pass
        sys.stderr.write(
            f"[chameleon-daemon] cannot bind socket ({e}); path is "
            f"{len(str(sock_path).encode())} bytes (AF_UNIX limit ~104). Set "
            "TMPDIR (or CHAMELEON_PLUGIN_DATA) to a shorter path to enable the "
            "daemon; hooks use the in-process path meanwhile\n"
        )
        return 1
    from chameleon_mcp.plugin_paths import secure_chmod

    secure_chmod(sock_path, 0o600)
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


def _wait_for_socket(sock_path: Path, deadline: float) -> bool:
    """Poll until the socket accepts a connection or the deadline passes."""
    while time.monotonic() < deadline:
        if sock_path.exists() and _socket_connectable(str(sock_path)):
            return True
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
    if not _af_unix_available():
        return {
            "status": "failed",
            "pid": None,
            "socket": "",
            "error": "AF_UNIX unavailable on this platform; daemon disabled",
        }

    sock_path = socket_path()

    if not force and is_daemon_alive():
        pid, _ = _read_pidfile()
        return {"status": "already_running", "pid": pid, "socket": str(sock_path)}

    _cleanup_stale()

    python = sys.executable or "python3"

    try:
        log_fd = os.open(str(log_path()), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError:
        log_fd = None

    try:
        first_pid = os.fork()
    except OSError as e:
        if log_fd is not None:
            os.close(log_fd)
        return {"status": "failed", "pid": None, "socket": str(sock_path), "error": str(e)}

    if first_pid > 0:
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

    try:
        os.setsid()
    except OSError:
        pass
    try:
        second_pid = os.fork()
    except OSError:
        os._exit(1)

    if second_pid > 0:
        os._exit(0)

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

    pf = pid_path()
    try:
        lock_fd = os.open(str(pf), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        sys.stderr.write(f"[chameleon-daemon] cannot open pidfile for lock: {e}\n")
        os._exit(1)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        os._exit(0)

    try:
        os.ftruncate(lock_fd, 0)
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.write(lock_fd, f"{os.getpid()}\n{sock_path}\n".encode())
        os.fsync(lock_fd)
    except OSError as e:
        sys.stderr.write(f"[chameleon-daemon] cannot write pidfile: {e}\n")
        os.close(lock_fd)
        os._exit(1)
    try:
        flags = fcntl.fcntl(lock_fd, fcntl.F_GETFD)
        fcntl.fcntl(lock_fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
    except OSError:
        pass

    try:
        os.execlp(python, python, "-m", "chameleon_mcp.daemon")
    except OSError as e:
        sys.stderr.write(f"[chameleon-daemon] exec failed: {e}\n")
        os._exit(1)
    os._exit(0)


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
        for p in (pid_path(), socket_path()):
            try:
                p.unlink()
            except (FileNotFoundError, OSError):
                pass
        return {"status": "not_running", "pid": None}

    # Recycle-TOCTOU guard: a LIVE daemon holds an exclusive flock on the
    # pidfile (see serve_forever's lock_fd). Probe it — if we can ACQUIRE the
    # lock, no live daemon holds the file, so `pid` is a stale/recycled value
    # (an unrelated process that inherited it). Don't SIGTERM that process.
    # (Re-reading the pidfile is useless here: its bytes don't change on a pid
    # recycle, so a content comparison can't detect it.)
    #
    # Only run this probe where flock is trustworthy. On Windows (no fcntl) and
    # on NFS/SMB mounts a successful acquire does NOT prove the daemon is dead,
    # so trusting it would delete a live daemon's pidfile. There we skip the
    # probe and signal the pid we already confirmed alive at the top.
    _probe_fd = None
    if _flock_reliable():
        try:
            _probe_fd = os.open(str(pid_path()), os.O_RDWR)
        except OSError:
            _probe_fd = None
    if _probe_fd is not None:
        try:
            fcntl.flock(_probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Held by the live daemon — expected; fall through and signal it.
            os.close(_probe_fd)
        else:
            # Acquired => no live daemon. Release, clean stale files, bail.
            try:
                fcntl.flock(_probe_fd, fcntl.LOCK_UN)
            finally:
                os.close(_probe_fd)
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
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.1)
        for p in (pid_path(), socket_path()):
            try:
                p.unlink()
            except (FileNotFoundError, OSError):
                pass
        return {"status": "timeout", "pid": pid}

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
    # Match is_daemon_alive (the actual fast-path gate): a live PID whose socket
    # is gone or unconnectable is unreachable, so the fast path is NOT engaged.
    # Reporting alive off the PID alone (a /tmp reaper can unlink an idle socket
    # while the process lives) contradicts what daemon_status exists to answer.
    alive = pid is not None and _pid_alive(pid) and bool(sock) and _socket_connectable(sock)
    if not alive:
        return {
            "alive": False,
            "pid": None,
            "socket": str(socket_path()),
            "uptime_s": None,
        }
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
    if not _af_unix_available():
        return
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
