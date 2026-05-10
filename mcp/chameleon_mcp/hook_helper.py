"""CLI helper for Claude Code hooks.

Hooks invoke this via:
    python -m chameleon_mcp.hook_helper <command>

Where <command> is one of: session-start | preflight-and-advise |
posttool-recorder | callout-detector.

Reads JSON from stdin, calls the appropriate MCP tool, emits a Claude Code
hook output JSON to stdout.

Phase 4: implements session-start (loads using-chameleon + profile primer)
and preflight-and-advise (calls get_pattern_context). posttool-recorder and
callout-detector remain Phase 4-end stubs.

Per ARCHITECTURE.md "Bootstrap mechanism" + "Hook stack".
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _emit(output: dict) -> None:
    """Write Claude Code hook output JSON to stdout. Single source of truth."""
    sys.stdout.write(json.dumps(output))
    sys.stdout.write("\n")


def _emit_session_context(content: str) -> None:
    """Emit SessionStart context per platform's expected JSON shape.

    Cursor: `{ "additional_context": ... }`
    Claude Code: `{ "hookSpecificOutput": { "hookEventName": "SessionStart", "additionalContext": ... } }`
    SDK / Copilot CLI: `{ "additionalContext": ... }`

    Mirrors superpowers/hooks/session-start dispatch logic. Single-format-per-platform
    (Round 5 BLOCKING fix: never emit both formats).
    """
    if os.environ.get("CURSOR_PLUGIN_ROOT"):
        _emit({"additional_context": content})
    elif os.environ.get("CLAUDE_PLUGIN_ROOT") and not os.environ.get("COPILOT_CLI"):
        _emit({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": content,
            }
        })
    else:
        _emit({"additionalContext": content})


def session_start() -> int:
    """SessionStart: inject using-chameleon SKILL.md + profile primer."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        # Without plugin root we can't locate the skill file. Emit empty context.
        _emit({})
        return 0

    skill_path = Path(plugin_root) / "skills" / "using-chameleon" / "SKILL.md"
    if not skill_path.is_file():
        _emit({})
        return 0

    skill_content = skill_path.read_text(encoding="utf-8", errors="replace")

    # Wrap in <chameleon-context> per ARCHITECTURE.md
    wrapped = (
        "<chameleon-context>\n"
        "You have chameleon, a profile-aware coding assistant.\n\n"
        "Below is the full content of your `using-chameleon` skill. Follow it.\n\n"
        f"{skill_content}\n"
        "</chameleon-context>"
    )

    _emit_session_context(wrapped)
    return 0


def preflight_and_advise() -> int:
    """PreToolUse Edit/Write/NotebookEdit: inject canonical context.

    Phase 4 simplified: reads tool_input.file_path, calls
    chameleon_mcp.tools.get_pattern_context, emits the result as
    additionalContext.

    Real Phase 4 production: connects to a long-lived daemon via UNIX socket
    for sub-100ms latency. This subprocess-per-call form is acceptable for
    initial dogfood but will be replaced with the daemon model.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _emit({})
        return 0

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        _emit({})
        return 0

    try:
        from chameleon_mcp.tools import get_pattern_context
        result = get_pattern_context(file_path)
    except Exception:
        # Fail-open per ARCHITECTURE.md — never block edits on advisor failure
        _emit({})
        return 0

    data = result.get("data", {})
    archetype = data.get("archetype", {}) or {}
    canonical = data.get("canonical_excerpt", {}) or {}
    archetype_name = archetype.get("name")

    if not archetype_name:
        _emit({})
        return 0

    # Build a short context block; cap at 1500 tokens approx via char limit
    excerpt_content = canonical.get("content") or ""
    rules_count = len(data.get("rules") or [])
    block = (
        "<chameleon-context>\n"
        f"[chameleon: archetype={archetype_name}, "
        f"confidence={archetype.get('confidence_band', 'unknown')}]\n\n"
    )
    if excerpt_content:
        block += "Canonical witness:\n```\n"
        block += excerpt_content[:6000]  # ~1500 tokens
        if len(excerpt_content) > 6000:
            block += "\n... [truncated]"
        block += "\n```\n\n"
    if rules_count:
        block += f"Rules: {rules_count} entries available via get_rules({archetype_name!r}).\n"
    block += "</chameleon-context>"

    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": block,
        }
    })
    return 0


def posttool_recorder() -> int:
    """PostToolUse Bash: HMAC-signed exec log. Phase 4-end implementation."""
    # Read input but don't act yet (avoid breaking the hook chain).
    try:
        sys.stdin.read()
    except Exception:
        pass
    _emit({})
    return 0


def callout_detector() -> int:
    """UserPromptSubmit: frustration phrase reminder. Phase 4-end implementation."""
    try:
        sys.stdin.read()
    except Exception:
        pass
    _emit({})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        sys.stderr.write("hook_helper.py: missing command argument\n")
        return 1
    command = args[0]
    if command == "session-start":
        return session_start()
    if command == "preflight-and-advise":
        return preflight_and_advise()
    if command == "posttool-recorder":
        return posttool_recorder()
    if command == "callout-detector":
        return callout_detector()
    sys.stderr.write(f"hook_helper.py: unknown command {command!r}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
