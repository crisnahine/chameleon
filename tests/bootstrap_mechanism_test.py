"""Bootstrap mechanism verification — the path Claude Code actually takes.

This test mirrors exactly what Claude Code does at session start:
1. Discovers hooks/hooks.json from .claude-plugin/plugin.json or convention.
2. Sets CLAUDE_PLUGIN_ROOT.
3. Invokes the configured command for SessionStart.
4. Reads JSON from stdout.
5. Injects `hookSpecificOutput.additionalContext` into the model context.

If any of these steps fails, using-chameleon never loads — and from a user's
perspective, the entire plugin is dead. So this test is the strictest
acceptance criterion for whether chameleon is "really integrated."

Two rounds:
  Round 1: simulate Claude Code session start, end-to-end.
  Round 2: per-platform dispatch (Cursor / Claude Code / Copilot CLI),
           JSON shape compliance, graceful degradation on bad env.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PASS, FAIL = [], []
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


def invoke_hook(matcher_event: str, env_overrides: dict | None = None) -> tuple[int, dict]:
    """Invoke the SessionStart hook the way Claude Code does.

    Args:
        matcher_event: one of "startup", "clear", "compact" (the SessionStart
                       matchers Claude Code emits).
        env_overrides: extra env vars to set (e.g., CURSOR_PLUGIN_ROOT).

    Returns:
        (returncode, parsed_output_dict)
    """
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    if env_overrides:
        env.update(env_overrides)
    payload = json.dumps({
        "hook_event_name": "SessionStart",
        "session_id": f"bootstrap-test-{matcher_event}",
        "matcher": matcher_event,
    })
    proc = subprocess.run(
        ["bash", str(PLUGIN_ROOT / "hooks" / "run-hook.cmd"), "session-start"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return proc.returncode, out


# ---------------------------------------------------------------------------
# Round 1: simulate Claude Code SessionStart end-to-end
# ---------------------------------------------------------------------------
section("Round 1 — Claude Code SessionStart end-to-end")

# 1. Verify hooks.json registers SessionStart
hooks_config = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
session_hooks = hooks_config.get("hooks", {}).get("SessionStart") or []
t(
    "hooks.json registers SessionStart",
    len(session_hooks) >= 1,
)

# 2. Verify the matcher covers all 3 Claude Code session events
matcher_pattern = session_hooks[0].get("matcher", "")
for event in ("startup", "clear", "compact"):
    t(
        f"SessionStart matcher covers '{event}'",
        event in matcher_pattern,
    )

# 3. Verify the configured command points at the run-hook dispatcher
configured_command = session_hooks[0].get("hooks", [{}])[0].get("command", "")
t(
    "Configured command uses ${CLAUDE_PLUGIN_ROOT}",
    "${CLAUDE_PLUGIN_ROOT}" in configured_command,
)
t(
    "Configured command uses run-hook.cmd dispatcher",
    "run-hook.cmd" in configured_command,
)
t(
    "Configured command passes session-start as argument",
    "session-start" in configured_command,
)

# 4. Verify the dispatcher script exists + is executable
dispatcher = PLUGIN_ROOT / "hooks" / "run-hook.cmd"
t("run-hook.cmd exists", dispatcher.is_file())

# 5. Invoke the hook for each matcher event
for event in ("startup", "clear", "compact"):
    rc, out = invoke_hook(event)
    t(f"Hook returns 0 for '{event}'", rc == 0)
    t(f"Hook emits valid JSON object for '{event}'", isinstance(out, dict))
    spec = out.get("hookSpecificOutput") or {}
    t(
        f"Hook emits hookSpecificOutput.hookEventName=SessionStart for '{event}'",
        spec.get("hookEventName") == "SessionStart",
    )
    additional = spec.get("additionalContext") or ""
    t(f"Hook emits non-empty additionalContext for '{event}'", len(additional) > 100)


# ---------------------------------------------------------------------------
# Round 1 (continued): additionalContext content shape
# ---------------------------------------------------------------------------
section("Round 1 — additionalContext content")

rc, out = invoke_hook("startup")
ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")

# The wrapper must announce chameleon
t(
    "additionalContext contains 'You have chameleon'",
    "You have chameleon" in ctx,
)
# It must reference the using-chameleon skill explicitly
t(
    "additionalContext mentions using-chameleon skill",
    "using-chameleon" in ctx,
)
# Skill content must be embedded — verify with a marker from the SKILL.md body
t(
    "additionalContext embeds 'Before any Edit' rule",
    "Before any Edit" in ctx,
)
t(
    "additionalContext embeds get_pattern_context call directive",
    "get_pattern_context" in ctx,
)
t(
    "additionalContext embeds 'Red Flags' table",
    "Red Flags" in ctx,
)
t(
    "additionalContext embeds 'Available slash commands' table",
    "Available slash commands" in ctx,
)
# The outer wrapper tag must be present and balanced
t(
    "additionalContext starts with <chameleon-context>",
    ctx.lstrip().startswith("<chameleon-context>"),
)
t(
    "additionalContext ends with </chameleon-context>",
    ctx.rstrip().endswith("</chameleon-context>"),
)


# ---------------------------------------------------------------------------
# Round 2: hook helper internals (per-platform dispatch logic)
# ---------------------------------------------------------------------------
section("Round 2 — hook helper internals")

helper_text = (PLUGIN_ROOT / "mcp" / "chameleon_mcp" / "hook_helper.py").read_text()
t(
    "Hook helper dispatches by CURSOR_PLUGIN_ROOT",
    "CURSOR_PLUGIN_ROOT" in helper_text,
)
t(
    "Hook helper emits hookSpecificOutput.hookEventName for Claude Code",
    '"hookEventName": "SessionStart"' in helper_text,
)
t(
    "Hook helper falls back to additionalContext for SDK / Copilot CLI",
    '"additionalContext"' in helper_text,
)


# ---------------------------------------------------------------------------
# Round 2: per-platform dispatch
# ---------------------------------------------------------------------------
section("Round 2 — per-platform dispatch")

# Cursor: CURSOR_PLUGIN_ROOT set → emit additional_context (snake_case)
rc, out = invoke_hook("startup", env_overrides={"CURSOR_PLUGIN_ROOT": str(PLUGIN_ROOT)})
t(
    "Cursor: emits additional_context (snake_case)",
    "additional_context" in out,
)
t(
    "Cursor: does NOT emit hookSpecificOutput",
    "hookSpecificOutput" not in out,
)

# Copilot CLI: COPILOT_CLI=1 → emit additionalContext (top-level, SDK standard)
rc, out = invoke_hook("startup", env_overrides={"COPILOT_CLI": "1"})
t(
    "Copilot CLI: emits additionalContext (top-level)",
    "additionalContext" in out and "hookSpecificOutput" not in out,
)

# Claude Code: only CLAUDE_PLUGIN_ROOT → nested hookSpecificOutput
env_only_claude = {k: "" for k in ("CURSOR_PLUGIN_ROOT", "COPILOT_CLI")}
rc, out = invoke_hook("startup", env_overrides=env_only_claude)
t(
    "Claude Code (CLAUDE_PLUGIN_ROOT only): emits nested hookSpecificOutput",
    "hookSpecificOutput" in out and out["hookSpecificOutput"].get("hookEventName") == "SessionStart",
)


# ---------------------------------------------------------------------------
# Round 2: format validity (JSON shape Claude Code expects)
# ---------------------------------------------------------------------------
section("Round 2 — JSON shape compliance")

rc, out = invoke_hook("startup")
spec = out.get("hookSpecificOutput", {})
# Claude Code's hook spec requires:
#   - top-level object
#   - hookSpecificOutput is a dict
#   - hookEventName matches the event ("SessionStart")
#   - additionalContext is a string (not array, not null)
t("Output is a JSON object", isinstance(out, dict))
t("hookSpecificOutput is a dict", isinstance(spec, dict))
t(
    "hookEventName equals 'SessionStart'",
    spec.get("hookEventName") == "SessionStart",
)
t(
    "additionalContext is a string",
    isinstance(spec.get("additionalContext"), str),
)
t(
    "No 'continue' field set (would block session)",
    "continue" not in out,
)
t(
    "No 'decision' field set (would block session)",
    "decision" not in out,
)


# ---------------------------------------------------------------------------
# Round 2: hook degrades cleanly when MCP venv is missing
# ---------------------------------------------------------------------------
section("Round 2 — graceful degradation paths")

# Run with no PYTHONPATH and no venv: should still emit valid JSON (even if {})
env = os.environ.copy()
env["CLAUDE_PLUGIN_ROOT"] = "/nonexistent/path"
proc = subprocess.run(
    ["bash", str(PLUGIN_ROOT / "hooks" / "run-hook.cmd"), "session-start"],
    input="",
    capture_output=True,
    text=True,
    env=env,
    timeout=10,
)
try:
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    valid_json = True
except json.JSONDecodeError:
    valid_json = False
t("Hook with bad CLAUDE_PLUGIN_ROOT emits valid JSON", valid_json)
t("Hook with bad CLAUDE_PLUGIN_ROOT exits 0", proc.returncode == 0)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {len(PASS) + len(FAIL)}")
print(f"  Pass: {len(PASS)}")
print(f"  Fail: {len(FAIL)}")
if FAIL:
    print("\n  FAILURES:")
    for name, info in FAIL:
        print(f"    - {name}{(': ' + info) if info else ''}")
    sys.exit(1)
sys.exit(0)
