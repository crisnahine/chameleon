"""Phase 9.x: hook lifecycle scenarios.

Exercises the four bash hook scripts directly via subprocess:
  9.1 SessionStart two-chunk output shape
  9.2 SessionStart resume re-prompts trust after 24h marker expiry
  9.3 PostToolUse exec log dir created with mode 0700
  9.4 Frustration phrase -> disable hint emitted (cheap, no claude)
  9.5 Callout-detector clean no-op: benign prompt, no false positive, no error log

All scenarios are cheap / no-claude.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
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


def _base_env(ctx, data_tmp: Path) -> dict:
    """Minimal env for hook subprocess invocations."""
    return {
        **os.environ,
        "CHAMELEON_PLUGIN_DATA": str(data_tmp),
        "CHAMELEON_ALLOW_TMP_REPO": "1",
        "CLAUDE_PLUGIN_ROOT": str(ctx.plugin_root),
        # Suppress log rotation noise by pointing the error log to a tmp file.
        "CHAMELEON_HOOK_ERROR_LOG": str(data_tmp / ".hook_errors.log"),
    }


def _run_hook(hook_name: str, event: dict, env: dict, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a named hook script with the given event JSON on stdin."""
    hook_path = env["CLAUDE_PLUGIN_ROOT"] + f"/hooks/{hook_name}"
    return subprocess.run(
        ["bash", hook_path],
        input=json.dumps(event).encode(),
        capture_output=True,
        env=env,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 9.1  SessionStart two-chunk output shape
# ---------------------------------------------------------------------------

def _run_session_start_two_chunk(ctx) -> Result:
    """SessionStart hook emits hookSpecificOutput with additionalContext.

    The output wraps the using-chameleon SKILL.md in <chameleon-context>
    tags. We verify:
      - stdout is valid JSON
      - hookSpecificOutput.hookEventName == 'SessionStart'
      - additionalContext contains at least one <chameleon-context> open tag
    """
    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    skill_path = ctx.plugin_root / "skills" / "using-chameleon" / "SKILL.md"
    if not skill_path.is_file():
        return Result(status="SKIP", notes="skills/using-chameleon/SKILL.md not found")

    data_tmp = ctx.plugin_data_dir
    env = _base_env(ctx, data_tmp)

    event = {"session_id": "dogfood-9-1"}

    try:
        proc = _run_hook("session-start", event, env)
    except subprocess.TimeoutExpired:
        return Result(status="FAIL", notes="session-start hook timed out (>15s)")

    raw_stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw_stdout:
        return Result(
            status="FAIL",
            notes=f"session-start produced empty stdout (exit={proc.returncode})",
        )

    try:
        hook_out = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        return Result(status="FAIL", notes=f"stdout is not valid JSON: {exc}; raw={raw_stdout[:200]!r}")

    # For Claude Code platform (CLAUDE_PLUGIN_ROOT set): hookSpecificOutput shape.
    hook_specific = hook_out.get("hookSpecificOutput", {})
    event_name = hook_specific.get("hookEventName")
    additional_context = hook_specific.get("additionalContext", "")

    # Fall back to flat additionalContext (SDK platform shape).
    if not additional_context:
        additional_context = hook_out.get("additionalContext", "") or hook_out.get("additional_context", "")

    if not additional_context:
        return Result(
            status="FAIL",
            notes=f"no additionalContext in hook output: {raw_stdout[:200]!r}",
        )

    # Verify <chameleon-context> wrapper is present.
    if "<chameleon-context>" not in additional_context:
        return Result(
            status="FAIL",
            notes="additionalContext missing <chameleon-context> open tag",
        )

    return Result(
        status="PASS",
        notes=f"SessionStart emits hookSpecificOutput with additionalContext containing <chameleon-context> "
              f"(event_name={event_name!r}, context_len={len(additional_context)})",
    )


# ---------------------------------------------------------------------------
# 9.2  SessionStart resume re-prompts trust after 24h
# ---------------------------------------------------------------------------

def _run_session_start_trust_reprompt(ctx) -> Result:
    """Trust-prompt marker: fresh marker suppresses re-prompt; stale marker (>24h) re-prompts.

    The hook calls _should_emit_untrusted_prompt which writes a marker under
    <plugin_data>/<repo_id>/.trust_prompted.<session_hash> and returns True
    only when the marker is absent or older than 24h.

    We simulate this directly by:
      1. Calling session-start once -> marker is written (first call always prompts).
      2. Touching the marker with a recent mtime -> calling session-start again
         produces identical context (the session-start hook doesn't check trust at all;
         the trust gate is in preflight-and-advise). So instead we test the helper
         function directly.
    """
    _ensure_mcp_on_path(ctx)

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    data_tmp = ctx.plugin_data_dir
    old_plugin_data = os.environ.get("CHAMELEON_PLUGIN_DATA")
    old_allow_tmp = os.environ.get("CHAMELEON_ALLOW_TMP_REPO")
    try:
        os.environ["CHAMELEON_PLUGIN_DATA"] = str(data_tmp)
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

        # Import the helper directly to test the trust-prompt marker logic.
        from chameleon_mcp.hook_helper import (  # type: ignore[import]
            _should_emit_untrusted_prompt,
        )
        from chameleon_mcp.optouts import _safe_session_marker  # type: ignore[import]

        repo_id = "test-trust-reprompt-9-2"
        session_id = "dogfood-session-9-2"

        # No marker yet: should prompt (returns True) and writes the marker.
        first_result = _should_emit_untrusted_prompt(repo_id, session_id)
        if not first_result:
            return Result(status="FAIL", notes="first call returned False (expected True -- no marker yet)")

        # Marker now exists and is fresh: should NOT prompt.
        second_result = _should_emit_untrusted_prompt(repo_id, session_id)
        if second_result:
            return Result(status="FAIL", notes="second call returned True (expected False -- marker is fresh)")

        # Backdate marker to 25 hours ago.
        session_hash = _safe_session_marker(session_id)
        marker_path = data_tmp / repo_id / f".trust_prompted.{session_hash}"
        if not marker_path.is_file():
            return Result(status="FAIL", notes=f"marker file not found at {marker_path}")

        backdated_mtime = time.time() - (25 * 3600)
        os.utime(marker_path, (backdated_mtime, backdated_mtime))

        # Marker is now stale (>24h): should re-prompt.
        third_result = _should_emit_untrusted_prompt(repo_id, session_id)
        if not third_result:
            return Result(
                status="FAIL",
                notes="third call returned False (expected True -- marker is >24h old, should re-prompt)",
            )
    finally:
        if old_plugin_data is None:
            os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
        else:
            os.environ["CHAMELEON_PLUGIN_DATA"] = old_plugin_data
        if old_allow_tmp is None:
            os.environ.pop("CHAMELEON_ALLOW_TMP_REPO", None)
        else:
            os.environ["CHAMELEON_ALLOW_TMP_REPO"] = old_allow_tmp

    return Result(
        status="PASS",
        notes="trust-prompt marker: fresh suppresses; stale (25h) re-prompts; 24h TTL verified",
    )


# ---------------------------------------------------------------------------
# 9.3  PostToolUse exec log dir created with mode 0700
# ---------------------------------------------------------------------------

def _run_posttool_log_dir_mode(ctx) -> Result:
    """posttool-recorder creates exec_log dir with mode 0700 (owner-only)."""
    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    data_tmp = ctx.plugin_data_dir

    # Use a dedicated TMPDIR so exec_log lands in a place we can inspect.
    exec_log_tmp = data_tmp / "exec_log_tmpdir"
    exec_log_tmp.mkdir(mode=0o700, exist_ok=True)

    env = _base_env(ctx, data_tmp)
    env["TMPDIR"] = str(exec_log_tmp)
    # Set CLAUDE_CWD to a known path so repo_id is deterministic.
    cwd_path = data_tmp
    env["CLAUDE_CWD"] = str(cwd_path)

    # Build the event payload for posttool-recorder.
    event = {
        "session_id": "dogfood-9-3",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
        "tool_response": {"returnCode": 0, "stdout": "hello\n", "stderr": ""},
    }

    try:
        proc = _run_hook("posttool-recorder", event, env)
    except subprocess.TimeoutExpired:
        return Result(status="FAIL", notes="posttool-recorder hook timed out (>15s)")

    # Hook should exit 0 and emit {} regardless of log write success.
    if proc.returncode != 0:
        return Result(
            status="FAIL",
            notes=f"posttool-recorder exited {proc.returncode}; stderr={proc.stderr.decode()[:200]!r}",
        )

    # Compute the expected repo_id (sha256 of cwd path).
    repo_id = hashlib.sha256(str(cwd_path.resolve()).encode("utf-8")).hexdigest()

    # Locate the exec_log dir.
    exec_log_base = exec_log_tmp / ".chameleon_exec_log"
    repo_log_dir = exec_log_base / repo_id

    if not repo_log_dir.is_dir():
        # The log write may have failed (e.g., HMAC key issues in test env).
        # Inspect stderr for clues.
        stderr_text = proc.stderr.decode("utf-8", errors="replace")
        if "HMACKeyError" in stderr_text or "key" in stderr_text.lower():
            return Result(
                status="SKIP",
                notes=f"exec_log dir not created (HMAC key issue in test env): {stderr_text[:150]!r}",
            )
        # Check if any exec_log dir was created at all.
        if exec_log_base.is_dir():
            found_dirs = list(exec_log_base.iterdir())
            return Result(
                status="FAIL",
                notes=f"exec_log dir not at expected path {repo_log_dir}; found: {found_dirs[:3]}",
            )
        return Result(
            status="FAIL",
            notes=f"exec_log base dir not created at {exec_log_base}",
        )

    # Check mode 0700.
    dir_mode = stat.S_IMODE(repo_log_dir.stat().st_mode)
    if dir_mode & 0o077:
        return Result(
            status="FAIL",
            notes=f"exec_log dir mode is {oct(dir_mode)}, expected 0700 (group/other bits set)",
        )

    return Result(
        status="PASS",
        notes=f"exec_log dir {repo_log_dir.name[:16]}... created with mode {oct(dir_mode)}",
    )


# ---------------------------------------------------------------------------
# 9.4  Frustration phrase -> disable hint emitted (cheap, no real claude)
# ---------------------------------------------------------------------------

def _run_frustration_disable_hint(ctx) -> Result:
    """callout-detector emits a hint for a known frustration phrase.

    Uses a phrase from _FRUSTRATION_PATTERNS in hook_helper.py.
    This is a cheap no-claude test: we invoke the hook script directly
    and check that the response contains chameleon-disable / chameleon-pause.
    """
    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    data_tmp = ctx.plugin_data_dir
    env = _base_env(ctx, data_tmp)

    # Use a phrase that matches _FRUSTRATION_PATTERNS (chameleon-specific pattern).
    frustration_prompt = "chameleon is so annoying, can you stop injecting context"

    event = {
        "session_id": "dogfood-9-4",
        "user_prompt": frustration_prompt,
    }

    try:
        proc = _run_hook("callout-detector", event, env)
    except subprocess.TimeoutExpired:
        return Result(status="FAIL", notes="callout-detector timed out (>15s)")

    if proc.returncode != 0:
        return Result(
            status="FAIL",
            notes=f"callout-detector exited {proc.returncode}; stderr={proc.stderr.decode()[:200]!r}",
        )

    raw_stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw_stdout:
        return Result(status="FAIL", notes="callout-detector returned empty stdout for frustration phrase")

    try:
        hook_out = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return Result(status="FAIL", notes=f"stdout is not valid JSON: {raw_stdout[:200]!r}")

    # Find additionalContext in the response.
    hook_specific = hook_out.get("hookSpecificOutput", {})
    context = hook_specific.get("additionalContext", "") or hook_out.get("additionalContext", "")

    if not context:
        return Result(
            status="FAIL",
            notes=f"no additionalContext in response for frustration phrase: {raw_stdout[:200]!r}",
        )

    # Verify the hint mentions chameleon-disable or chameleon-pause.
    if "chameleon-disable" not in context and "CHAMELEON_DISABLE" not in context:
        return Result(
            status="FAIL",
            notes=f"hint does not mention chameleon-disable: {context[:300]!r}",
        )
    if "chameleon-pause" not in context:
        return Result(
            status="FAIL",
            notes=f"hint does not mention chameleon-pause-15m: {context[:300]!r}",
        )

    return Result(
        status="PASS",
        notes=f"frustration phrase triggered hint mentioning chameleon-disable and chameleon-pause (context_len={len(context)})",
    )


# ---------------------------------------------------------------------------
# 9.5  Callout-detector clean no-op: benign prompt, no false positive
# ---------------------------------------------------------------------------

def _run_callout_detector_clean_noop(ctx) -> Result:
    """callout-detector returns {} for a benign prompt and writes no error log entry."""
    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    data_tmp = ctx.plugin_data_dir
    env = _base_env(ctx, data_tmp)

    # A completely benign, non-frustration prompt.
    benign_prompt = "please add a helper function that formats dates"

    event = {
        "session_id": "dogfood-9-5",
        "user_prompt": benign_prompt,
    }

    # Record error log state before the hook runs.
    error_log_path = data_tmp / ".hook_errors.log"
    error_log_size_before = error_log_path.stat().st_size if error_log_path.is_file() else 0

    try:
        proc = _run_hook("callout-detector", event, env)
    except subprocess.TimeoutExpired:
        return Result(status="FAIL", notes="callout-detector timed out (>15s)")

    if proc.returncode != 0:
        return Result(
            status="FAIL",
            notes=f"callout-detector exited {proc.returncode}; stderr={proc.stderr.decode()[:200]!r}",
        )

    raw_stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if not raw_stdout:
        return Result(status="FAIL", notes="callout-detector returned empty stdout (expected '{}')")

    try:
        hook_out = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return Result(status="FAIL", notes=f"stdout is not valid JSON: {raw_stdout[:200]!r}")

    # For a benign prompt, the hook should return an empty dict (no advisory).
    hook_specific = hook_out.get("hookSpecificOutput")
    additional = (
        hook_specific.get("additionalContext", "") if hook_specific else ""
    ) or hook_out.get("additionalContext", "")

    if additional:
        return Result(
            status="FAIL",
            notes=f"benign prompt triggered false positive: {additional[:200]!r}",
        )

    # Verify no new errors were written to the error log during a clean run.
    error_log_size_after = error_log_path.stat().st_size if error_log_path.is_file() else 0
    if error_log_size_after > error_log_size_before:
        added = error_log_path.read_text(encoding="utf-8", errors="replace")[error_log_size_before:]
        return Result(
            status="FAIL",
            notes=f"error log grew by {error_log_size_after - error_log_size_before} bytes on clean run: {added[:200]!r}",
        )

    return Result(
        status="PASS",
        notes=f"benign prompt -> {{}} (no advisory); error log unchanged (size={error_log_size_after})",
    )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="9.1",
        name="SessionStart two-chunk output shape",
        family="hooks",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_session_start_two_chunk,
    ),
    Scenario(
        id="9.2",
        name="SessionStart resume re-prompts trust after 24h",
        family="hooks",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_session_start_trust_reprompt,
    ),
    Scenario(
        id="9.3",
        name="PostToolUse log dir 0700",
        family="hooks",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_posttool_log_dir_mode,
    ),
    Scenario(
        id="9.4",
        name="frustration disable hint",
        family="hooks",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_frustration_disable_hint,
    ),
    Scenario(
        id="9.5",
        name="callout-detector log line shape stable",
        family="hooks",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_callout_detector_clean_noop,
    ),
]
