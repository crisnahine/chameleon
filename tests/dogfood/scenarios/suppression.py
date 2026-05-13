"""Phase 8.x: suppression scenarios.

Exercises is_chameleon_suppressed and the four opt-out mechanisms:
  8.1 /chameleon-disable toggle (session_disable)
  8.2 /chameleon-pause-15m + .pause_until (with expiry backdate)
  8.3 CHAMELEON_DISABLE=1 env var
  8.4 .chameleon/.skip file
  8.5 Layered suppression: .skip wins over CHAMELEON_DISABLE

All scenarios are cheap / no-claude. Env mutations are always
saved and restored in a finally block.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
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


def _set_env(ctx, *, allow_tmp: bool = True) -> dict:
    """Apply CHAMELEON_PLUGIN_DATA + CHAMELEON_ALLOW_TMP_REPO; return old values."""
    old: dict = {
        "CHAMELEON_PLUGIN_DATA": os.environ.get("CHAMELEON_PLUGIN_DATA"),
        "CHAMELEON_ALLOW_TMP_REPO": os.environ.get("CHAMELEON_ALLOW_TMP_REPO"),
        "CHAMELEON_DISABLE": os.environ.get("CHAMELEON_DISABLE"),
    }
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
    if allow_tmp:
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    # Start clean — callers that need CHAMELEON_DISABLE can set it explicitly.
    os.environ.pop("CHAMELEON_DISABLE", None)
    return old


def _restore_env(old: dict) -> None:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# 8.1  /chameleon-disable toggle
# ---------------------------------------------------------------------------

def _run_chameleon_disable_toggle(ctx) -> Result:
    """Call disable_session; verify session_disable; then clear and verify None."""
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.optouts import (  # type: ignore[import]
        clear_session_disable,
        is_chameleon_suppressed,
        write_session_disable,
    )

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        # Create a minimal .chameleon dir so repo_root resolves properly.
        chameleon_dir = repo / ".chameleon"
        chameleon_dir.mkdir(exist_ok=True)

        repo_id = "test-disable-toggle-8-1"
        session_id = "session-disable-test"

        # Write the session-disable marker directly (simulates /chameleon-disable).
        write_session_disable(repo_id, session_id)

        # Verify: suppressed with reason "session_disable".
        reason = is_chameleon_suppressed(repo, repo_id, session_id)
        if reason != "session_disable":
            return Result(
                status="FAIL",
                notes=f"expected reason=session_disable after write, got {reason!r}",
            )

        # Clear the marker (simulates end-of-session cleanup or re-enable).
        cleared = clear_session_disable(repo_id, session_id)
        if not cleared:
            return Result(status="FAIL", notes="clear_session_disable returned False (marker missing)")

        # Verify: no suppression after clear.
        reason_after = is_chameleon_suppressed(repo, repo_id, session_id)
        if reason_after is not None:
            return Result(
                status="FAIL",
                notes=f"expected None after clear, got {reason_after!r}",
            )
    finally:
        _restore_env(old)

    return Result(status="PASS", notes="session_disable write + clear both transition correctly")


# ---------------------------------------------------------------------------
# 8.2  /chameleon-pause-15m + .pause_until (with expiry backdate)
# ---------------------------------------------------------------------------

def _run_pause_and_expiry(ctx) -> Result:
    """pause_session writes .pause_until; backdate it; verify auto-expiry."""
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.optouts import (  # type: ignore[import]
        is_chameleon_suppressed,
        write_pause,
    )
    from chameleon_mcp.profile.trust import repo_data_dir  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        chameleon_dir = repo / ".chameleon"
        chameleon_dir.mkdir(exist_ok=True)

        repo_id = "test-pause-expiry-8-2"

        # Write a 15-minute pause.
        expiry_iso = write_pause(repo_id, minutes=15)

        # Verify the .pause_until file exists.
        pause_path = repo_data_dir(repo_id) / ".pause_until"
        if not pause_path.is_file():
            return Result(status="FAIL", notes=".pause_until file not created by write_pause")

        # Verify: suppressed with reason "pause".
        reason = is_chameleon_suppressed(repo, repo_id)
        if reason != "pause":
            return Result(
                status="FAIL",
                notes=f"expected reason=pause while active, got {reason!r}",
            )

        # Backdate the .pause_until timestamp to 1 hour in the past.
        from datetime import UTC, datetime
        past_expiry = datetime.fromtimestamp(time.time() - 3600, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        pause_path.write_text(past_expiry, encoding="utf-8")

        # Verify: no suppression after expiry (auto-clean path).
        reason_after = is_chameleon_suppressed(repo, repo_id)
        if reason_after is not None:
            return Result(
                status="FAIL",
                notes=f"expected None after backdate, got {reason_after!r}",
            )

        # The expired .pause_until should have been cleaned up.
        if pause_path.is_file():
            return Result(
                status="FAIL",
                notes=".pause_until not cleaned up after expiry",
            )
    finally:
        _restore_env(old)

    return Result(
        status="PASS",
        notes=f"pause active (expiry={expiry_iso}), then expired and auto-cleaned",
    )


# ---------------------------------------------------------------------------
# 8.3  CHAMELEON_DISABLE=1 env var
# ---------------------------------------------------------------------------

def _run_env_var_disable(ctx) -> Result:
    """Set CHAMELEON_DISABLE=1; verify user_disable; unset; verify None."""
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.optouts import is_chameleon_suppressed  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        chameleon_dir = repo / ".chameleon"
        chameleon_dir.mkdir(exist_ok=True)

        repo_id = "test-env-disable-8-3"

        # Set the env var.
        os.environ["CHAMELEON_DISABLE"] = "1"
        reason = is_chameleon_suppressed(repo, repo_id)
        if reason != "user_disable":
            return Result(
                status="FAIL",
                notes=f"expected reason=user_disable with CHAMELEON_DISABLE=1, got {reason!r}",
            )

        # Unset and verify no suppression.
        os.environ.pop("CHAMELEON_DISABLE", None)
        reason_after = is_chameleon_suppressed(repo, repo_id)
        if reason_after is not None:
            return Result(
                status="FAIL",
                notes=f"expected None after unsetting CHAMELEON_DISABLE, got {reason_after!r}",
            )
    finally:
        _restore_env(old)

    return Result(status="PASS", notes="CHAMELEON_DISABLE=1 -> user_disable; unset -> None")


# ---------------------------------------------------------------------------
# 8.4  .chameleon/.skip file
# ---------------------------------------------------------------------------

def _run_skip_file(ctx) -> Result:
    """Touch .chameleon/.skip; verify repo_skip; remove; verify None."""
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.optouts import is_chameleon_suppressed  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        chameleon_dir = repo / ".chameleon"
        chameleon_dir.mkdir(exist_ok=True)

        repo_id = "test-skip-file-8-4"

        # Create the .skip sentinel.
        skip_path = chameleon_dir / ".skip"
        skip_path.touch()

        reason = is_chameleon_suppressed(repo, repo_id)
        if reason != "repo_skip":
            return Result(
                status="FAIL",
                notes=f"expected reason=repo_skip with .skip present, got {reason!r}",
            )

        # Remove .skip and verify no suppression.
        skip_path.unlink()
        reason_after = is_chameleon_suppressed(repo, repo_id)
        if reason_after is not None:
            return Result(
                status="FAIL",
                notes=f"expected None after removing .skip, got {reason_after!r}",
            )
    finally:
        _restore_env(old)

    return Result(status="PASS", notes=".skip present -> repo_skip; removed -> None")


# ---------------------------------------------------------------------------
# 8.5  Layered suppression: .skip wins over CHAMELEON_DISABLE
# ---------------------------------------------------------------------------

def _run_layered_suppression(ctx) -> Result:
    """Set CHAMELEON_DISABLE=1 AND create .skip; verify .skip wins (repo_skip)."""
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.optouts import is_chameleon_suppressed  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = _make_fresh_copy(ctx)
    old = _set_env(ctx)
    try:
        chameleon_dir = repo / ".chameleon"
        chameleon_dir.mkdir(exist_ok=True)

        repo_id = "test-layered-suppression-8-5"

        # Both .skip and CHAMELEON_DISABLE active simultaneously.
        (chameleon_dir / ".skip").touch()
        os.environ["CHAMELEON_DISABLE"] = "1"

        reason = is_chameleon_suppressed(repo, repo_id)
        if reason != "repo_skip":
            return Result(
                status="FAIL",
                notes=(
                    f"expected .skip to win (repo_skip) over CHAMELEON_DISABLE, "
                    f"got {reason!r}"
                ),
            )
    finally:
        _restore_env(old)

    return Result(
        status="PASS",
        notes="layered: .skip + CHAMELEON_DISABLE=1 -> repo_skip (.skip wins per hierarchy)",
    )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="8.1",
        name="/chameleon-disable toggle",
        family="suppression",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_chameleon_disable_toggle,
    ),
    Scenario(
        id="8.2",
        name="/chameleon-pause-15m + .pause_until",
        family="suppression",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_pause_and_expiry,
    ),
    Scenario(
        id="8.3",
        name="CHAMELEON_DISABLE=1",
        family="suppression",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_env_var_disable,
    ),
    Scenario(
        id="8.4",
        name=".chameleon/.skip",
        family="suppression",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_skip_file,
    ),
    Scenario(
        id="8.5",
        name="layered suppression (.skip wins)",
        family="suppression",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_layered_suppression,
    ),
]
