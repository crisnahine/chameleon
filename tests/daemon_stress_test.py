"""Daemon socket stress test — v0.5.0 long-lived daemon under load.

Covers four scenarios against a fresh per-test daemon spawned inside an
isolated CHAMELEON_PLUGIN_DATA tempdir:

  1. Sequential burst — 100 sequential get_pattern_context calls,
     warm-up discarded; record p50/p95/p99 latency. p50 < 100 ms.
  2. Concurrent burst — 200 requests via ThreadPoolExecutor(20). The
     daemon is single-threaded (one connection at a time); the kernel
     listen() backlog absorbs the short queue. End-to-end wall-clock
     must complete inside 30 s.
  3. Oversize frame rejection — a request body > MAX_FRAME_BYTES is
     refused (client returns None; daemon does NOT crash). A subsequent
     normal call still succeeds, proving the loop stayed up.
  4. SIGTERM mid-flight — the daemon shuts down on SIGTERM; the next
     client call returns None (fail-open). A fresh start_daemon()
     respawns successfully.

Measured numbers are echoed to stdout so future regressions are
catchable by `grep '[DAEMON-STRESS-RESULT]' …`.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/daemon_stress_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import statistics
import struct
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
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
# Isolated plugin data dir — keeps the user's real daemon untouched.
# ---------------------------------------------------------------------------
_TMP_DATA = tempfile.mkdtemp(prefix="chameleon_daemon_stress_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_DATA

import chameleon_mcp.daemon as daemon  # noqa: E402
import chameleon_mcp.daemon_client as daemon_client  # noqa: E402


def _stop_safely() -> None:
    """Defensive stop between scenarios so each test starts clean."""
    try:
        daemon.stop_daemon(timeout=3.0)
    except Exception:  # noqa: BLE001
        pass
    for p in (daemon.pid_path(), daemon.socket_path()):
        try:
            p.unlink()
        except (FileNotFoundError, OSError):
            pass


def _wait_until(pred, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Tiny TS repo (same shape as v0_2_regression_test._make_tiny_ts_repo) so
# get_pattern_context has a real archetype to resolve. The trust handshake
# happens once at module load so all four scenarios share state and we don't
# pay the bootstrap cost four times.
# ---------------------------------------------------------------------------
def _make_tiny_ts_repo() -> Path:
    """Create a tiny TS repo with two distinguishable archetypes."""
    root = Path(tempfile.mkdtemp(prefix="chameleon_daemon_stress_repo_"))
    (root / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5.0.0"}}'
    )
    (root / "tsconfig.json").write_text("{}")

    app_dir = root / "app" / "controllers" / "api" / "v1"
    app_dir.mkdir(parents=True)
    for i in range(6):
        (app_dir / f"r{i}.ts").write_text(
            f"export class Resource{i} {{ get() {{ return {i}; }} }}\n"
        )

    spec_dir = root / "spec" / "controllers" / "api" / "v1"
    spec_dir.mkdir(parents=True)
    for i in range(6):
        (spec_dir / f"r{i}.test.ts").write_text(
            f"import {{ Resource{i} }} from '../../app/controllers/api/v1/r{i}';\n"
            f"test('r{i}', () => {{ expect(new Resource{i}().get()).toBe({i}); }});\n"
        )
    return root


# Build + trust the test repo BEFORE any daemon starts so the daemon's
# in-process state (which it'll consult for get_pattern_context) sees a
# trusted profile.
TEST_REPO = _make_tiny_ts_repo()
from chameleon_mcp.tools import bootstrap_repo, trust_profile  # noqa: E402

bootstrap_repo(str(TEST_REPO))
trust_profile(str(TEST_REPO), TEST_REPO.name)
TARGET_FILE = TEST_REPO / "app" / "controllers" / "api" / "v1" / "r0.ts"
assert TARGET_FILE.is_file(), "test fixture is missing"


# ---------------------------------------------------------------------------
# 1. Sequential 100-call latency profile
# ---------------------------------------------------------------------------
section("1. Sequential 100 get_pattern_context calls — p50 / p95 / p99")

_stop_safely()
start_result = daemon.start_daemon()
t(
    f"daemon started for sequential burst (status={start_result.get('status')})",
    start_result.get("status") in ("started", "already_running"),
    str(start_result),
)

# Warm-up: the first call inside the daemon process pays the
# import-chameleon-tools tax (~100-300ms). Drop the warm-up sample so the
# percentiles reflect steady-state behavior.
warmup = daemon_client.call(
    "get_pattern_context", {"file_path": str(TARGET_FILE)}, timeout=5.0
)
t(
    "warm-up call returns a dict (validates daemon is live)",
    isinstance(warmup, dict),
    f"got {type(warmup).__name__}",
)

latencies_ms: list[float] = []
for _ in range(100):
    t0 = time.monotonic()
    resp = daemon_client.call(
        "get_pattern_context", {"file_path": str(TARGET_FILE)}, timeout=5.0
    )
    t1 = time.monotonic()
    if isinstance(resp, dict):
        latencies_ms.append((t1 - t0) * 1000.0)

t(
    f"all 100 sequential calls returned a dict (got {len(latencies_ms)})",
    len(latencies_ms) == 100,
    f"only {len(latencies_ms)} of 100 succeeded — daemon dropping requests?",
)

if latencies_ms:
    p50 = statistics.median(latencies_ms)
    sorted_l = sorted(latencies_ms)
    p95 = sorted_l[int(0.95 * len(sorted_l)) - 1]
    p99 = sorted_l[int(0.99 * len(sorted_l)) - 1]
    avg = statistics.mean(latencies_ms)

    print(
        f"\n    [MEASURED] sequential latency: "
        f"avg={avg:.1f}ms p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms "
        f"min={min(latencies_ms):.1f}ms max={max(latencies_ms):.1f}ms"
    )
    t(
        f"p50 latency < 100 ms (got {p50:.1f} ms)",
        p50 < 100.0,
        f"slow median: {p50:.1f}ms",
    )
    # Diagnostic-only (not a hard assertion) — surface the tail so we can
    # see if a small fraction of calls spike. The hook treats anything
    # above DEFAULT_TIMEOUT_S=1500ms as a fallback-to-in-process signal,
    # so p99 above that would indicate a real regression.
    t(
        f"p99 latency under client default timeout (got {p99:.1f} ms)",
        p99 < 1500.0,
        f"tail too slow: {p99:.1f}ms vs 1500ms client deadline",
    )


# ---------------------------------------------------------------------------
# 2. Concurrent 200 requests via ThreadPoolExecutor(max_workers=20)
# ---------------------------------------------------------------------------
section("2. 200 concurrent get_pattern_context (20-thread pool)")

# Daemon is single-threaded: each connection serializes through
# _handle_connection. ThreadPoolExecutor pushes 20 in flight at a time;
# the rest queue at the kernel level inside listen() backlog (16). On
# macOS the kernel returns ECONNREFUSED immediately once backlog is
# saturated (no SYN-queue softening) — `daemon_client.call` surfaces
# that as None, exactly matching the hook's fail-open contract.
#
# Real production callers retry: the hook falls back to the in-process
# path when a daemon call returns None. We model the same behavior here
# with a bounded retry so the test measures "200 calls eventually all
# round-trip" rather than "200 calls all win the listen() backlog race".
# This validates the FAIL-OPEN contract under flood, not best-case
# kernel queueing.

# Retry budget: with kernel backlog=16, listen() refusals are spiky.
# Empirically a single unlucky thread can hit ~25 refuses in a row on
# macOS, so we give each call 60 attempts (~5 s worst-case per call,
# well inside the 30 s end-to-end budget).
_MAX_RETRIES = 60
_RETRY_BASE_DELAY_S = 0.005


def _one_call(idx: int) -> tuple[int, float, bool, int]:
    """Returns (idx, total_latency_ms, ok, retry_count).

    Retries on connection refused / None response to simulate the hook's
    fail-open retry behavior. Tracks total retries so the test output
    surfaces just how much queueing the kernel actually forced.
    """
    t0 = time.monotonic()
    retries = 0
    while retries < _MAX_RETRIES:
        resp = daemon_client.call(
            "get_pattern_context",
            {"file_path": str(TARGET_FILE)},
            timeout=10.0,
        )
        if isinstance(resp, dict):
            t1 = time.monotonic()
            return idx, (t1 - t0) * 1000.0, True, retries
        retries += 1
        # Jittered exponential backoff — caps at ~80 ms so a long queue
        # still drains inside the 30 s burst budget.
        delay = min(0.08, _RETRY_BASE_DELAY_S * (2 ** min(retries, 4)))
        time.sleep(delay)
    t1 = time.monotonic()
    return idx, (t1 - t0) * 1000.0, False, retries


burst_start = time.monotonic()
results: list[tuple[int, float, bool, int]] = []
with ThreadPoolExecutor(max_workers=20) as ex:
    futures = [ex.submit(_one_call, i) for i in range(200)]
    for fut in as_completed(futures):
        results.append(fut.result())
burst_elapsed = time.monotonic() - burst_start

oks = [r for r in results if r[2]]
burst_latencies = [r[1] for r in oks]
total_retries = sum(r[3] for r in results)
max_retries_seen = max((r[3] for r in results), default=0)
print(
    f"\n    [MEASURED] concurrent burst: "
    f"{len(oks)}/{len(results)} succeeded in {burst_elapsed:.2f}s, "
    f"avg_latency={statistics.mean(burst_latencies) if burst_latencies else 0:.0f}ms, "
    f"max_latency={max(burst_latencies) if burst_latencies else 0:.0f}ms, "
    f"total_retries={total_retries} (max_per_call={max_retries_seen})"
)
print(
    "    NOTE: daemon is single-threaded with listen() backlog=16; "
    "retries above zero reflect the documented fail-open contract — "
    "`daemon_client.call` returns None on ECONNREFUSED and the hook "
    "falls back to in-process. This test models that retry loop."
)

t(
    "daemon still alive after 200 concurrent requests",
    daemon.is_daemon_alive(),
    "daemon crashed under load",
)
t(
    f"all 200 concurrent requests eventually succeeded (got {len(oks)})",
    len(oks) == 200,
    f"only {len(oks)}/200 round-tripped after up to {_MAX_RETRIES} retries each",
)
t(
    f"200 concurrent requests complete inside 30 s (took {burst_elapsed:.2f}s)",
    burst_elapsed < 30.0,
    f"burst too slow: {burst_elapsed:.2f}s",
)


# ---------------------------------------------------------------------------
# 3. Oversize frame → daemon rejects cleanly + stays up
# ---------------------------------------------------------------------------
section("3. Oversize request (>1 MB) — clean rejection, no crash")

# Client refuses to send oversize entirely (it returns None before connecting),
# so we craft the frame manually to exercise the SERVER's oversize handling.
# We announce a length > MAX_FRAME_BYTES via the 4-byte header, then close
# the connection without sending a body — the daemon must:
#  (a) read the header,
#  (b) decide it's oversize,
#  (c) either close the connection or send the documented oversize envelope,
#  (d) NOT crash the accept loop.

# 3a. Client-side refusal.
big_payload = "x" * (daemon.MAX_FRAME_BYTES + 1)
oversize_client = daemon_client.call(
    "get_pattern_context", {"file_path": str(TARGET_FILE), "junk": big_payload}
)
t(
    "client returns None on locally-oversize request",
    oversize_client is None,
    f"client tried to send {len(big_payload)} bytes",
)

# 3b. Server-side: hand-craft a malicious header announcing an oversize body.
sock_path = daemon.socket_path()
oversize_envelope: dict | None = None
oversize_raw = b""
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(str(sock_path))
    # Header announces a body 1 byte over the cap. Body is never sent.
    s.sendall(struct.pack("!I", daemon.MAX_FRAME_BYTES + 1))
    try:
        # Read whatever the server emits in response. The daemon's
        # _handle_connection writes an "oversize_or_disconnect" envelope
        # then closes. We can read either the framed response or just EOF.
        oversize_raw = s.recv(8192)
    except (TimeoutError, OSError):
        oversize_raw = b""
    s.close()
except Exception as e:  # noqa: BLE001
    t("server oversize handling did not crash test harness", False, str(e))
else:
    # Try to parse what we got. Best case: 4-byte len prefix + JSON envelope.
    if len(oversize_raw) >= 4:
        (length,) = struct.unpack("!I", oversize_raw[:4])
        body = oversize_raw[4 : 4 + length]
        try:
            oversize_envelope = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            oversize_envelope = None

    t(
        "server sends an error envelope (not a crash) on oversize header",
        isinstance(oversize_envelope, dict)
        and "error" in oversize_envelope,
        f"got raw={oversize_raw[:64]!r}, parsed={oversize_envelope!r}",
    )

# Loop survived? Issue a normal ping to prove the daemon is still up.
t("daemon still alive after oversize attempt", daemon.is_daemon_alive())
post_oversize = daemon_client.call(
    "get_pattern_context", {"file_path": str(TARGET_FILE)}, timeout=5.0
)
t(
    "normal call after oversize attempt succeeds",
    isinstance(post_oversize, dict),
    f"got {type(post_oversize).__name__}",
)


# ---------------------------------------------------------------------------
# 4. SIGTERM mid-flight + fresh start_daemon respawn
# ---------------------------------------------------------------------------
section("4. SIGTERM-the-daemon → client returns None → respawn works")

assert daemon.is_daemon_alive(), "daemon must be up before SIGTERM scenario"
pid_pre, _ = daemon._read_pidfile()
t("recorded PID before SIGTERM", isinstance(pid_pre, int))

# Send SIGTERM. The daemon's signal handler flips shutdown_requested; the
# accept loop notices within 1s (timeout=1.0 on accept()). We wait up to
# 5s for the process to exit and the socket to disappear.
if isinstance(pid_pre, int):
    try:
        os.kill(pid_pre, signal.SIGTERM)
        sigterm_ok = True
    except OSError as e:
        sigterm_ok = False
        t("SIGTERM delivered", False, str(e))
    else:
        t("SIGTERM delivered", sigterm_ok)

    if sigterm_ok:
        shut_down = _wait_until(
            lambda: not daemon.is_daemon_alive(), timeout=5.0
        )
        t("daemon exits within 5s of SIGTERM", shut_down)

# After shutdown, the client returns None (fail-open) — exactly the
# contract preflight-and-advise relies on.
post_sigterm = daemon_client.call(
    "get_pattern_context", {"file_path": str(TARGET_FILE)}, timeout=0.5
)
t(
    "daemon_client.call returns None after SIGTERM (fail-open)",
    post_sigterm is None,
    f"got {type(post_sigterm).__name__}: {post_sigterm!r}",
)

# Respawn must succeed. start_daemon() wraps double-fork + waits for the
# socket; we expect status='started' (NOT 'already_running' since the
# pidfile was just cleaned by the dying daemon's finally block).
respawn = daemon.start_daemon()
t(
    f"start_daemon respawns after SIGTERM (status={respawn.get('status')})",
    respawn.get("status") in ("started", "already_running"),
    str(respawn),
)
t(
    "respawned daemon has a fresh PID different from old",
    isinstance(respawn.get("pid"), int)
    and respawn.get("pid") != pid_pre,
    f"old={pid_pre}, new={respawn.get('pid')}",
)
t("respawned daemon is alive", daemon.is_daemon_alive())

# Final sanity: a real call against the respawned daemon round-trips.
respawn_call = daemon_client.call(
    "get_pattern_context", {"file_path": str(TARGET_FILE)}, timeout=5.0
)
t(
    "first call against respawned daemon succeeds",
    isinstance(respawn_call, dict),
    f"got {type(respawn_call).__name__}",
)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
_stop_safely()
shutil.rmtree(TEST_REPO, ignore_errors=True)
shutil.rmtree(_TMP_DATA, ignore_errors=True)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {PASS + FAIL}")
print(f"  Pass:  {PASS}")
print(f"  Fail:  {FAIL}")

# Headline marker for grep-able CI logs.
if latencies_ms:
    sorted_l = sorted(latencies_ms)
    p50 = statistics.median(latencies_ms)
    p95 = sorted_l[int(0.95 * len(sorted_l)) - 1]
    p99 = sorted_l[int(0.99 * len(sorted_l)) - 1]
    print(
        f"\n[DAEMON-STRESS-RESULT] p50={p50:.1f}ms p95={p95:.1f}ms "
        f"p99={p99:.1f}ms burst200_s={burst_elapsed:.2f}"
    )

sys.exit(0 if FAIL == 0 else 1)
