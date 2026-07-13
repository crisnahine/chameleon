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
    _AcceptBackoff,
    _af_unix_available,
    _code_fingerprint,
    _DaemonState,
    _ensure_private_socket_dir,
    _flock_reliable,
    _idle_timeout_from_env,
    _socket_tmp_base,
    _sweep_orphan_version_files,
    _version_tag,
    daemon_info,
    pid_path,
    recv_frame,
    run_daemon,
    send_frame,
    serve_forever,
    socket_path,
    socket_path_for,
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
    # None until the first real request is served (the ping/daemon_status contract).
    assert state.last_request_at is None


def test_daemon_state_mark_request():
    state = _DaemonState(idle_timeout_s=10.0)
    assert state.last_request_at is None
    assert state.request_count == 0

    time.sleep(0.01)
    state.mark_request()

    assert state.request_count == 1
    assert state.last_request_at is not None
    assert state.last_request_at >= state.started_at


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
    # The pidfile stays in the data dir, version-scoped by name. The socket
    # name carries the same identity folded into a short hash (sun_path cap).
    assert pp.name == f".daemon-{tag}.pid"
    assert sp == socket_path_for(fake, tag, _socket_tmp_base())


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


# ---------------------------------------------------------------------------
# Socket relocation: AF_UNIX sun_path is capped (~104 bytes on macOS, ~108 on
# Linux). The socket must resolve to a short per-user tmp dir so a deep
# CHAMELEON_PLUGIN_DATA cannot push bind() past the cap and silently kill the
# daemon fast path forever. Pidfile and log stay in the data dir.
# ---------------------------------------------------------------------------


def test_socket_path_for_deep_data_dir_stays_under_sun_path_limit():
    # A 200-char data dir used to produce an unbindable socket path.
    deep = Path("/" + "x" * 100 + "/" + "y" * 99)
    assert len(str(deep)) >= 200
    tmp_base = Path("/tmp/chameleon-501")
    sp = socket_path_for(deep, "2.69.0-abcd1234", tmp_base)
    assert sp.parent == tmp_base
    assert len(os.fsencode(str(sp))) <= 100


def test_socket_path_for_different_data_dirs_differ():
    # Two CHAMELEON_PLUGIN_DATA universes must never share a socket: the name
    # hash has to include the data dir, not just the version.
    tmp_base = Path("/tmp/chameleon-501")
    a = socket_path_for(Path("/universe/a"), "1.2.3", tmp_base)
    b = socket_path_for(Path("/universe/b"), "1.2.3", tmp_base)
    assert a != b


def test_socket_path_for_different_versions_differ():
    tmp_base = Path("/tmp/chameleon-501")
    a = socket_path_for(Path("/data"), "1.2.3-aaaa1111", tmp_base)
    b = socket_path_for(Path("/data"), "1.2.4-bbbb2222", tmp_base)
    assert a != b


def test_socket_path_for_no_tmp_base_uses_data_dir():
    # Windows (no getuid) resolves tmp_base=None: keep the legacy in-data-dir
    # path; the AF_UNIX guards make the daemon a no-op there anyway.
    sp = socket_path_for(Path("/data"), "1.2.3", None)
    assert sp == Path("/data/.daemon-1.2.3.sock")


def test_socket_path_for_pathological_tmp_base_falls_back_to_data_dir():
    # A TMPDIR so deep that even the relocated path overruns sun_path: fall
    # back to the legacy data-dir path and let run_daemon's existing
    # fail-open bind diagnostics fire. Never crash.
    long_tmp = Path("/" + "t" * 120)
    sp = socket_path_for(Path("/data"), "1.2.3", long_tmp / "chameleon-501")
    assert sp == Path("/data/.daemon-1.2.3.sock")


def test_socket_path_creates_private_socket_dir(tmp_path: Path):
    import tempfile as _tempfile

    fake_data = tmp_path / "data"
    fake_data.mkdir()
    base = Path(_tempfile.mkdtemp(prefix="cdt", dir="/tmp")) / "sockdir"
    try:
        with (
            patch("chameleon_mcp.daemon._plugin_data", return_value=fake_data),
            patch("chameleon_mcp.daemon._socket_tmp_bases", return_value=[base]),
        ):
            sp = socket_path()
        assert sp.parent == base
        assert base.is_dir()
        assert (base.stat().st_mode & 0o777) == 0o700
        assert base.stat().st_uid == os.getuid()
    finally:
        import shutil

        shutil.rmtree(base.parent, ignore_errors=True)


def test_socket_path_falls_back_when_socket_dir_not_private(tmp_path: Path):
    # A squatted /tmp/chameleon-<uid> (another uid owns it, sticky bit blocks
    # removal) must not be used for the socket: fall back to the data dir.
    fake_data = tmp_path / "data"
    fake_data.mkdir()
    with (
        patch("chameleon_mcp.daemon._plugin_data", return_value=fake_data),
        patch("chameleon_mcp.daemon._ensure_private_socket_dir", return_value=False),
    ):
        sp = socket_path()
    assert sp == fake_data / f".daemon-{_version_tag()}.sock"


def test_ensure_private_socket_dir_refuses_symlink(tmp_path: Path):
    # A symlink planted at the socket-dir path redirects the bind elsewhere;
    # lstat must see the link itself and refuse.
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert _ensure_private_socket_dir(link) is False
    assert _ensure_private_socket_dir(real) is True


def test_ensure_private_socket_dir_tightens_loose_mode(tmp_path: Path):
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    assert _ensure_private_socket_dir(d) is True
    assert (d.stat().st_mode & 0o777) == 0o700


def test_client_and_daemon_resolve_the_same_socket_path(tmp_path: Path):
    # One source of truth: daemon_client imports daemon.socket_path, so both
    # sides of the wire must agree byte-for-byte.
    from chameleon_mcp import daemon_client as dc

    fake_data = tmp_path / "data"
    fake_data.mkdir()
    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake_data):
        assert dc.socket_path() == socket_path()


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
# Bare module invocation (`python -m chameleon_mcp.daemon`, no "start"
# subcommand) must leave the same discoverable pidfile behind as the `start`
# subcommand path, since main()'s empty-argv branch calls run_daemon()
# directly, bypassing start_daemon()'s fork/exec dance entirely.
# ---------------------------------------------------------------------------


def test_run_daemon_writes_pidfile_without_going_through_start_daemon(tmp_path: Path):
    import threading

    from chameleon_mcp.daemon import _read_pidfile, is_daemon_alive

    fake = tmp_path / "d"
    fake.mkdir()
    with (
        patch("chameleon_mcp.daemon._plugin_data", return_value=fake),
        patch.dict(os.environ, {"CHAMELEON_DAEMON_IDLE_TIMEOUT": "0.2"}),
    ):
        t = threading.Thread(target=run_daemon, daemon=True)
        t.start()
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not is_daemon_alive():
                time.sleep(0.02)
            assert is_daemon_alive() is True, "bare run_daemon() never became discoverable"
            pid, sock = _read_pidfile()
            assert pid == os.getpid()
            assert sock
        finally:
            t.join(timeout=5.0)
        # The idle timeout (0.2s) self-exits the thread; the pidfile cleanup in
        # run_daemon()'s finally block must follow, so the daemon reads as gone.
        assert is_daemon_alive() is False


def test_run_daemon_second_bare_invocation_does_not_steal_the_lock(tmp_path: Path):
    """Two bare invocations racing for the same pidfile: only the first gets
    to serve; the second must see the lock held and bail out cleanly rather
    than silently overwriting the running daemon's pidfile."""
    fake = tmp_path / "d"
    fake.mkdir()
    from chameleon_mcp.daemon import _acquire_daemon_pidfile

    with patch("chameleon_mcp.daemon._plugin_data", return_value=fake):
        first = _acquire_daemon_pidfile(fake / "d.sock")
        try:
            assert first is not None
            second = _acquire_daemon_pidfile(fake / "d.sock")
            assert second is None
        finally:
            os.close(first)


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


def test_code_fingerprint_differs_on_content_not_mtime(tmp_path: Path):
    # Two source trees with byte-for-byte different content but identical file
    # mtimes must produce different fingerprints. Hashing mtimes alone collides
    # in frozen-timestamp environments (Docker layers, git checkouts), which
    # would let a new-code daemon reuse a stale old-code socket.
    frozen = 1_700_000_000

    tree_a = tmp_path / "a"
    tree_a.mkdir()
    (tree_a / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    os.utime(tree_a / "mod.py", (frozen, frozen))

    tree_b = tmp_path / "b"
    tree_b.mkdir()
    (tree_b / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(tree_b / "mod.py", (frozen, frozen))

    fp_a = _code_fingerprint(tree_a)
    fp_b = _code_fingerprint(tree_b)

    assert fp_a != "0"
    assert fp_b != "0"
    assert fp_a != fp_b


def test_code_fingerprint_stable_when_mtime_changes(tmp_path: Path):
    # Touching a file (changing its mtime) without changing content must NOT
    # rotate the fingerprint: content is the identity, not the timestamp.
    tree = tmp_path / "t"
    tree.mkdir()
    f = tree / "mod.py"
    f.write_text("VALUE = 1\n", encoding="utf-8")
    os.utime(f, (1_700_000_000, 1_700_000_000))
    fp_before = _code_fingerprint(tree)
    os.utime(f, (1_800_000_000, 1_800_000_000))
    fp_after = _code_fingerprint(tree)
    assert fp_before == fp_after


def test_code_fingerprint_empty_tree_returns_zero(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _code_fingerprint(empty) == "0"


# ---------------------------------------------------------------------------
# accept-loop backoff: a persistent accept() error (e.g. EMFILE fd pressure)
# must not hot-spin the loop or flood the daemon log. Sleep grows with an
# exponential cap; stderr logging is rate limited per error type.
# ---------------------------------------------------------------------------


def test_accept_backoff_sleep_grows_then_caps():
    bo = _AcceptBackoff()
    sleeps = [bo.observe("err") for _ in range(12)]
    # Strictly non-decreasing up to the cap.
    for a, b in zip(sleeps, sleeps[1:], strict=False):
        assert b >= a
    # First wait is small; the cap bounds the longest wait.
    assert sleeps[0] <= 0.2
    assert max(sleeps) <= _AcceptBackoff.MAX_SLEEP_S + 1e-9
    assert sleeps[-1] == _AcceptBackoff.MAX_SLEEP_S


def test_accept_backoff_resets_after_success():
    bo = _AcceptBackoff()
    for _ in range(5):
        bo.observe("err")
    grown = bo.observe("err")
    bo.reset()
    first_again = bo.observe("err")
    assert first_again < grown
    assert first_again <= 0.2


def test_serve_forever_backs_off_and_throttles_persistent_accept_error(monkeypatch):
    # A socket whose accept() always raises EMFILE must not hot-spin or flood
    # stderr. The loop should sleep (bounded) between tries and log far fewer
    # times than it retries.
    import errno as _errno

    sleeps: list[float] = []
    monkeypatch.setattr("chameleon_mcp.daemon.time.sleep", lambda s: sleeps.append(s))

    log_lines: list[str] = []
    monkeypatch.setattr(
        "chameleon_mcp.daemon.sys.stderr",
        type("W", (), {"write": lambda _self, s: log_lines.append(s)})(),
    )

    class _EmfileSock:
        def settimeout(self, _t):  # noqa: D401 - test stub
            return None

        def accept(self):
            # Stop the loop after enough iterations to observe backoff.
            if len(sleeps) >= 8:
                state.shutdown_requested = True
                raise TimeoutError
            raise OSError(_errno.EMFILE, "Too many open files")

    state = _DaemonState(idle_timeout_s=10_000.0)
    serve_forever(_EmfileSock(), state, lambda m, p: {"ok": True})

    assert len(sleeps) >= 8, "loop should have backed off on each accept error"
    # Bounded: no sleep exceeds the cap.
    assert max(sleeps) <= _AcceptBackoff.MAX_SLEEP_S + 1e-9
    # Throttled: one EMFILE log line, not one per retry.
    emfile_logs = [line for line in log_lines if "accept error" in line]
    assert len(emfile_logs) == 1, f"expected a single throttled log line, got {len(emfile_logs)}"


def test_accept_backoff_logs_first_then_suppresses(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("chameleon_mcp.daemon.time.monotonic", lambda: now[0])
    bo = _AcceptBackoff()
    # First occurrence of an error type always logs.
    assert bo.should_log("emfile") is True
    # Immediate repeats of the SAME type are suppressed.
    assert bo.should_log("emfile") is False
    assert bo.should_log("emfile") is False
    # A different error type logs immediately even within the window.
    assert bo.should_log("econnaborted") is True
    # After the rate-limit window elapses, the same type logs again.
    now[0] += _AcceptBackoff.LOG_INTERVAL_S + 0.01
    assert bo.should_log("emfile") is True


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


class TestDispatchLintFileTruncation:
    """The PostToolUse hook caps an oversized file to its 100KB prefix, then hands
    that prefix to the daemon's lint_file. The daemon must forward the caller's
    content_truncated flag so the removed-export check is skipped on the prefix;
    dropping it made every export past the cap read as spuriously removed."""

    def _capture(self, monkeypatch):
        seen: dict = {}

        def fake_lint_file(repo, archetype, content, file_path=None, content_truncated=None):
            seen["content_truncated"] = content_truncated
            return {"api_version": "1", "data": {"violations": []}}

        monkeypatch.setattr("chameleon_mcp.tools.lint_file", fake_lint_file)
        return seen

    def _dispatch_lint(self, extra: dict):
        from chameleon_mcp.daemon import _dispatch

        payload = {"repo": "/x", "archetype": "a", "content": "y", "file_path": "f.ts"}
        payload.update(extra)
        return _dispatch("lint_file", payload)

    def test_true_is_forwarded(self, monkeypatch):
        seen = self._capture(monkeypatch)
        self._dispatch_lint({"content_truncated": True})
        assert seen["content_truncated"] is True

    def test_false_is_forwarded(self, monkeypatch):
        seen = self._capture(monkeypatch)
        self._dispatch_lint({"content_truncated": False})
        assert seen["content_truncated"] is False

    def test_absent_defaults_to_none(self, monkeypatch):
        seen = self._capture(monkeypatch)
        self._dispatch_lint({})
        assert seen["content_truncated"] is None

    def test_non_bool_coerced_to_none(self, monkeypatch):
        seen = self._capture(monkeypatch)
        self._dispatch_lint({"content_truncated": "yes"})
        assert seen["content_truncated"] is None


class TestDaemonStatusHonesty:
    """daemon_status/ping/daemon_info must report the true fast-path state:
    no phantom last-request before any work, and not-alive when the socket
    (the reachability the fast path needs) is gone even if the PID lives."""

    def test_last_request_at_is_none_until_a_real_request(self):
        from chameleon_mcp.daemon import _DaemonState

        state = _DaemonState(30.0)
        # ping and daemon_status surface this; the contract is "None until served".
        assert state.last_request_at is None
        assert state.request_count == 0
        state.mark_request()
        assert state.last_request_at is not None
        assert state.request_count == 1

    def test_daemon_info_not_alive_when_socket_gone_though_pid_lives(self, monkeypatch):
        import os

        from chameleon_mcp import daemon as d

        monkeypatch.setattr(d, "_read_pidfile", lambda: (os.getpid(), "/no/such/socket.sock"))
        monkeypatch.setattr(d, "_pid_alive", lambda pid: True)
        # PID alive but the socket is gone -> unreachable -> fast path not engaged.
        assert d.daemon_info()["alive"] is False

    def test_daemon_info_alive_when_socket_connectable(self, monkeypatch):
        import os
        import socket
        import tempfile

        from chameleon_mcp import daemon as d

        # A short socket path: AF_UNIX caps the path at ~104 bytes on macOS, and a
        # pytest tmp_path under a deep scratch dir overruns it.
        sock_dir = tempfile.mkdtemp(prefix="cd")
        sock_path = os.path.join(sock_dir, "d.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(1)
        try:
            monkeypatch.setattr(d, "_read_pidfile", lambda: (os.getpid(), sock_path))
            monkeypatch.setattr(d, "_pid_alive", lambda pid: True)
            assert d.daemon_info()["alive"] is True
        finally:
            srv.close()
            os.unlink(sock_path)
            os.rmdir(sock_dir)
