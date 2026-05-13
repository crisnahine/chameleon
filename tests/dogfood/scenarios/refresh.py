"""Phase 7.x: refresh and lock contention scenarios.

Finding (7.2): refresh_repo acquires .chameleon/.refresh.lock (non-blocking
flock) at the top of the function before delegating to bootstrap_repo. A
concurrent /chameleon-refresh call gets a fast "failed" envelope instead of
serializing on the 30s rename flock inside atomic_profile_commit.

Finding (7.4): teach_profile uses a non-blocking advisory flock at
.chameleon/.idioms.lock. If that lock is held, teach_profile returns a
"failed" envelope immediately. This is the contention path tested in 7.4.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import sys
import time
from pathlib import Path

from tests.dogfood.scenario import Result, Scenario

_FIXTURE_REL = "tests/fixtures/eval_repos/ts_minimal"


def _ensure_mcp_on_path(ctx) -> None:
    d = str(ctx.plugin_root / "mcp")
    if d not in sys.path:
        sys.path.insert(0, d)


def _make_fresh_copy(ctx) -> Path:
    src = ctx.plugin_root / _FIXTURE_REL
    dest = ctx.plugin_data_dir / "ts_minimal"
    shutil.copytree(src, dest)
    return dest


def _set_env(ctx) -> dict:
    old = {
        "CHAMELEON_PLUGIN_DATA": os.environ.get("CHAMELEON_PLUGIN_DATA"),
        "CHAMELEON_ALLOW_TMP_REPO": os.environ.get("CHAMELEON_ALLOW_TMP_REPO"),
    }
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    return old


def _restore_env(old: dict) -> None:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# 7.1  Normal refresh succeeds
# ---------------------------------------------------------------------------

def _run_normal_refresh(ctx) -> Result:
    """refresh_repo on a fresh fixture returns noop or success (no change since bootstrap)."""
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import refresh_repo, trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        trust_profile(str(repo), repo.name)
        response = refresh_repo(str(repo), force=False)
    finally:
        _restore_env(old)

    data = response.get("data", {})
    status = data.get("status")

    if status == "noop":
        return Result(
            status="PASS",
            notes=f"refresh_repo returned noop (no files changed since bootstrap)",
        )

    if status == "success":
        archetypes = data.get("archetypes_detected", 0)
        return Result(
            status="PASS",
            notes=f"refresh_repo returned success (archetypes_detected={archetypes})",
        )

    return Result(
        status="FAIL",
        notes=f"expected status=noop or success, got {status!r}; data={data}",
    )


# ---------------------------------------------------------------------------
# 7.2  Lock contention rejects 2nd refresh
# ---------------------------------------------------------------------------

def _run_lock_contention_refresh(ctx) -> Result:
    """Hold .chameleon/.refresh.lock and verify refresh_repo returns failed quickly.

    refresh_repo acquires .chameleon/.refresh.lock (non-blocking flock) at the
    top of the function. If we hold that lock in the test process, refresh_repo
    must return a 'failed' envelope without blocking on the 30s rename flock.
    """
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import refresh_repo, trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        trust_profile(str(repo), repo.name)

        lock_path = repo / ".chameleon" / ".refresh.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Acquire the refresh lock exclusively (non-blocking; should succeed
        # since no other process holds it yet).
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        lock_held = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_held = True
                # Write PID + current time so acquire_advisory_lock sees a
                # live holder (prevents the stale-lock bypass).
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
            except OSError:
                pass

            if not lock_held:
                return Result(status="SKIP", notes="could not pre-acquire refresh lock for test")

            # While holding the lock, call refresh_repo - must fail fast.
            t0 = time.monotonic()
            response = refresh_repo(str(repo), force=True)
            elapsed = time.monotonic() - t0
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
    finally:
        _restore_env(old)

    data = response.get("data", {})
    status = data.get("status")
    error = data.get("error", "")

    if status != "failed":
        return Result(
            status="FAIL",
            notes=f"expected status=failed on lock contention, got {status!r} (elapsed={elapsed:.2f}s)",
        )

    # Must return quickly (non-blocking flock, not the 30s rename flock).
    if elapsed > 1.0:
        return Result(
            status="FAIL",
            notes=f"refresh_repo blocked for {elapsed:.2f}s instead of returning fast on lock contention",
        )

    # Error message should mention in-progress / lock / retry.
    if "in progress" not in error and "lock" not in error.lower() and "retry" not in error.lower():
        return Result(
            status="FAIL",
            notes=f"failed but error doesn't mention lock: {error!r}",
        )

    return Result(
        status="PASS",
        notes=f"refresh_repo rejected fast on held lock (elapsed={elapsed:.2f}s, error={error[:80]!r})",
    )


# ---------------------------------------------------------------------------
# 7.3  Stale-lock recovery
# ---------------------------------------------------------------------------

def _run_stale_lock_recovery(ctx) -> Result:
    """Manually create the idioms lock file (unflocked) and verify teach succeeds.

    A file without an active flock is not a live lock. POSIX flock semantics:
    lock state is per open file-descriptor, not per inode. A file at the lock
    path with no live fd holding LOCK_EX is immediately acquirable. This
    verifies chameleon's lock path self-heals when an old lock file exists from
    a previous process that exited cleanly.
    """
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import teach_profile, trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        trust_profile(str(repo), repo.name)

        # Plant a stale lock file (touch it, but don't hold an flock)
        lock_path = repo / ".chameleon" / ".idioms.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999 0.0\n", encoding="utf-8")  # fake dead PID + old epoch

        # teach_profile must acquire the lock and succeed
        response = teach_profile(str(repo), "Stale-lock recovery test idiom.")
    finally:
        _restore_env(old)

    data = response.get("data", {})
    status = data.get("status")

    if status != "success":
        return Result(
            status="FAIL",
            notes=f"expected success after stale lock file, got status={status!r}, error={data.get('error')!r}",
        )

    idioms_path = repo / ".chameleon" / "idioms.md"
    if not idioms_path.is_file() or "recovery test idiom" not in idioms_path.read_text(encoding="utf-8"):
        return Result(status="FAIL", notes="idiom not written despite status=success")

    return Result(status="PASS", notes="teach_profile acquired lock over stale lock file and succeeded")


# ---------------------------------------------------------------------------
# 7.4  Concurrent teach contention
# ---------------------------------------------------------------------------

def _run_concurrent_teach_contention(ctx) -> Result:
    """Hold the idioms lock and verify teach_profile returns failed quickly.

    teach_profile uses a non-blocking acquire_advisory_lock on
    .chameleon/.idioms.lock. If we hold that lock in the test process,
    teach_profile must return a 'failed' envelope without blocking.
    """
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import teach_profile, trust_profile  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        trust_profile(str(repo), repo.name)

        lock_path = repo / ".chameleon" / ".idioms.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Acquire the idioms lock exclusively (non-blocking, should succeed since
        # no other process holds it yet).
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        lock_held = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_held = True
                # Write our PID + current time so acquire_advisory_lock sees a
                # live holder (prevents the stale-lock bypass).
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
            except OSError:
                pass

            if not lock_held:
                return Result(status="SKIP", notes="could not pre-acquire idioms lock for test")

            # While holding the lock, call teach_profile - must fail fast
            t0 = time.monotonic()
            response = teach_profile(str(repo), "This should be rejected due to lock contention.")
            elapsed = time.monotonic() - t0
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
    finally:
        _restore_env(old)

    data = response.get("data", {})
    status = data.get("status")
    error = data.get("error", "")

    if status != "failed":
        return Result(
            status="FAIL",
            notes=f"expected status=failed on lock contention, got {status!r} (elapsed={elapsed:.2f}s)",
        )

    # Must return quickly (non-blocking acquire_advisory_lock)
    if elapsed > 5.0:
        return Result(
            status="FAIL",
            notes=f"teach_profile blocked for {elapsed:.2f}s instead of returning fast on lock contention",
        )

    # Error message should mention lock / in progress
    if "in progress" not in error and "lock" not in error.lower() and "retry" not in error.lower():
        return Result(
            status="FAIL",
            notes=f"failed but error doesn't mention lock: {error!r}",
        )

    return Result(
        status="PASS",
        notes=f"teach_profile rejected fast on held lock (elapsed={elapsed:.2f}s, error={error[:80]!r})",
    )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="7.1",
        name="normal refresh succeeds",
        family="refresh",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_normal_refresh,
    ),
    Scenario(
        id="7.2",
        name="lock contention rejects 2nd refresh",
        family="refresh",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_lock_contention_refresh,
    ),
    Scenario(
        id="7.3",
        name="stale-lock recovery",
        family="refresh",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_stale_lock_recovery,
    ),
    Scenario(
        id="7.4",
        name="concurrent teach contention",
        family="refresh",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_concurrent_teach_contention,
    ),
]
