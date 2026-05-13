"""Phase 14: multi-harness dispatch scenario."""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

from tests.dogfood.scenario import Result, Scenario


def _ensure_mcp_on_path(ctx) -> None:
    d = str(ctx.plugin_root / "mcp")
    if d not in sys.path:
        sys.path.insert(0, d)


def _capture_session_start(env_overrides: dict[str, str | None]) -> str:
    """Call hook_helper.session_start() with the given env overrides.

    Temporarily sets/clears env vars, redirects stdout to a StringIO buffer,
    calls session_start(), then restores everything.

    Returns the captured stdout string.
    """
    saved_env: dict[str, str | None] = {}
    for k, v in env_overrides.items():
        saved_env[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    # Redirect stdout
    old_stdout = sys.stdout
    capture = io.StringIO()
    sys.stdout = capture

    try:
        # Import fresh each time so module-level env reads are re-evaluated.
        # Use importlib.reload to ensure _emit_session_context re-reads env.
        import importlib
        import chameleon_mcp.hook_helper as hh  # type: ignore[import]
        importlib.reload(hh)
        hh.session_start()
    except SystemExit:
        pass
    except Exception:
        pass
    finally:
        sys.stdout = old_stdout
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return capture.getvalue()


# ---------------------------------------------------------------------------
# 14.1  Multi-harness dispatch
# ---------------------------------------------------------------------------

def _run_multi_harness_dispatch(ctx) -> Result:
    """Verify the three JSON-shape dispatch branches in hook_helper.session_start.

    Cursor:       CURSOR_PLUGIN_ROOT set  -> {"additional_context": ...}
    Copilot CLI:  COPILOT_CLI=1 (no CURSOR) -> {"additionalContext": ...}
    Claude Code:  CLAUDE_PLUGIN_ROOT set, no others -> hookSpecificOutput shape
    """
    _ensure_mcp_on_path(ctx)

    # Need a real skill file for session_start to produce content (non-empty body).
    skill_path = ctx.plugin_root / "skills" / "using-chameleon" / "SKILL.md"
    if not skill_path.is_file():
        return Result(status="SKIP", notes="skills/using-chameleon/SKILL.md not found")

    failures: list[str] = []

    # --- Cursor branch ---
    cursor_out = _capture_session_start({
        "CURSOR_PLUGIN_ROOT": "/whatever",
        "CLAUDE_PLUGIN_ROOT": str(ctx.plugin_root),
        "COPILOT_CLI": None,
    })
    import json
    try:
        cursor_parsed = json.loads(cursor_out.strip())
    except json.JSONDecodeError:
        failures.append(f"cursor: stdout not valid JSON: {cursor_out[:80]!r}")
        cursor_parsed = {}

    if "additional_context" not in cursor_parsed:
        failures.append(
            f"cursor: expected 'additional_context' key, got keys={list(cursor_parsed.keys())}"
        )

    # --- Copilot CLI branch ---
    copilot_out = _capture_session_start({
        "COPILOT_CLI": "1",
        "CURSOR_PLUGIN_ROOT": None,
        "CLAUDE_PLUGIN_ROOT": str(ctx.plugin_root),
    })
    try:
        copilot_parsed = json.loads(copilot_out.strip())
    except json.JSONDecodeError:
        failures.append(f"copilot: stdout not valid JSON: {copilot_out[:80]!r}")
        copilot_parsed = {}

    if "additionalContext" not in copilot_parsed:
        failures.append(
            f"copilot: expected top-level 'additionalContext' key, got keys={list(copilot_parsed.keys())}"
        )
    # Copilot branch must NOT have hookSpecificOutput
    if "hookSpecificOutput" in copilot_parsed:
        failures.append("copilot: unexpected 'hookSpecificOutput' in copilot branch output")

    # --- Claude Code branch ---
    claude_out = _capture_session_start({
        "CLAUDE_PLUGIN_ROOT": str(ctx.plugin_root),
        "CURSOR_PLUGIN_ROOT": None,
        "COPILOT_CLI": None,
    })
    try:
        claude_parsed = json.loads(claude_out.strip())
    except json.JSONDecodeError:
        failures.append(f"claude: stdout not valid JSON: {claude_out[:80]!r}")
        claude_parsed = {}

    hook_output = claude_parsed.get("hookSpecificOutput", {})
    if "hookSpecificOutput" not in claude_parsed:
        failures.append(
            f"claude: expected 'hookSpecificOutput' key, got keys={list(claude_parsed.keys())}"
        )
    elif "additionalContext" not in hook_output:
        failures.append(
            f"claude: hookSpecificOutput missing 'additionalContext', got keys={list(hook_output.keys())}"
        )

    if failures:
        return Result(status="FAIL", notes="; ".join(failures))

    return Result(
        status="PASS",
        notes=(
            "cursor->additional_context, copilot->additionalContext (top-level), "
            "claude->hookSpecificOutput.additionalContext: all 3 dispatch shapes correct"
        ),
    )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="14.1",
        name="multi-harness dispatch",
        family="harness",
        needs_claude=False,
        cost="cheap",
        requires=[],
        run=_run_multi_harness_dispatch,
    ),
]
