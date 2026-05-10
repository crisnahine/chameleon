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

import hashlib
import json
import os
import re
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

    Single-format-per-platform: never emit both formats — Claude Code reads
    both `additional_context` and `hookSpecificOutput` without dedup.
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

    # Opt-out check BEFORE any expensive work. If suppressed, emit empty
    # context so the edit proceeds without injection. Mirrors the docs'
    # "hook stack still fires (safety hard-deny preserved) but no
    # <chameleon-context> content is injected" promise.
    try:
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root
        from chameleon_mcp.tools import _compute_repo_id

        repo_root_path = find_repo_root(Path(file_path).expanduser())
        repo_id_hint = _compute_repo_id(repo_root_path) if repo_root_path else None
        session_id = payload.get("session_id")
        suppressed = is_chameleon_suppressed(
            repo_root=repo_root_path,
            repo_id=repo_id_hint,
            session_id=session_id,
        )
        if suppressed is not None:
            _emit({})
            return 0
    except Exception:
        # Suppression check should never block — fail open into normal flow
        pass

    try:
        from chameleon_mcp.tools import get_pattern_context
        result = get_pattern_context(file_path)
    except Exception:
        # Fail-open per ARCHITECTURE.md — never block edits on advisor failure
        _emit({})
        return 0

    data = result.get("data", {})
    archetype_obj = data.get("archetype", {}) or {}
    canonical = data.get("canonical_excerpt", {}) or {}
    repo_info = data.get("repo", {}) or {}
    trust_state = repo_info.get("trust_state")
    # Note: get_archetype returns {archetype: <name>, alternatives, content_signal_match,
    # confidence_band}. The cluster name lives under the "archetype" key (yes, nested).
    archetype_name = archetype_obj.get("archetype")

    if not archetype_name:
        _emit({})
        return 0

    # Record a drift observation. Best-effort — failure must not block the edit.
    repo_info = data.get("repo") or {}
    repo_id = repo_info.get("id")
    confidence_band = archetype_obj.get("confidence_band")
    if repo_id:
        try:
            from chameleon_mcp.drift.observations import record_edit_observation

            record_edit_observation(
                repo_id=repo_id,
                rel_path=str(file_path),
                archetype=archetype_name,
                confidence_band=confidence_band,
                matched_canonical=bool(canonical.get("witness_path")),
            )
        except Exception:
            pass

    # Build a short context block; cap at 1500 tokens approx via char limit
    excerpt_content = canonical.get("content") or ""
    rules_count = len(data.get("rules") or [])
    idioms_text = data.get("idioms") or ""
    has_idioms = bool(idioms_text.strip())
    block = (
        "<chameleon-context>\n"
        f"[chameleon: archetype={archetype_name}, "
        f"confidence={archetype_obj.get('confidence_band', 'unknown')}]\n\n"
    )
    if trust_state == "stale":
        block += (
            "**Trust is stale**: the .chameleon/ profile has changed since the user trusted it. "
            "Surface this once to your human partner and suggest /chameleon-trust to re-confirm. "
            "Do not block the edit; chameleon advisory is provided below for reference only.\n\n"
        )
    if excerpt_content:
        block += "Canonical witness:\n```\n"
        block += excerpt_content[:6000]  # ~1500 tokens
        if len(excerpt_content) > 6000:
            block += "\n... [truncated]"
        block += "\n```\n\n"
    if rules_count:
        block += f"Rules: {rules_count} entries available via get_rules({archetype_name!r}).\n"
    if has_idioms:
        block += "Team idioms captured via /chameleon-teach are available via get_pattern_context.\n"
    block += "</chameleon-context>"

    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": block,
        }
    })
    return 0


def posttool_recorder() -> int:
    """PostToolUse Bash: HMAC-signed exec log."""
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _emit({})
        return 0

    # Extract the bits we care about
    tool_input = payload.get("tool_input", {})
    tool_response = payload.get("tool_response", {})
    command = tool_input.get("command", "")
    session_id = payload.get("session_id", "unknown")
    exit_code = tool_response.get("returnCode") if isinstance(tool_response, dict) else None

    # Compute repo_id from cwd if available; else use session_id as the bucket
    cwd = Path(os.environ.get("CLAUDE_CWD") or os.getcwd()).resolve()
    repo_id = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()

    try:
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(
            repo_id=repo_id,
            session_id=session_id,
            command=command,
            exit_code=int(exit_code) if exit_code is not None else -1,
        )
    except Exception:
        # Fail-open per Round 4 — never break the hook chain on logging errors
        pass

    _emit({})
    return 0


# Frustration phrases that suggest the user is unhappy with chameleon's
# latency or pattern advice. Surfaced as a one-line reminder via
# additionalContext.
_FRUSTRATION_PATTERNS = (
    re.compile(r"\b(ugh|argh|wtf|stop|nope|wait)\b", re.IGNORECASE),
    re.compile(r"this isn'?t right", re.IGNORECASE),
    re.compile(r"don'?t (do|use|inject) (that|this)", re.IGNORECASE),
    re.compile(r"chameleon\s+is\s+(slow|wrong|broken)", re.IGNORECASE),
)


def callout_detector() -> int:
    """UserPromptSubmit: frustration phrase reminder.

    On detected frustration during a chameleon-active session, surface a
    one-line hint about /chameleon-disable, /chameleon-pause-15m, and
    /chameleon-teach as actionable next steps.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _emit({})
        return 0

    user_prompt = payload.get("user_prompt", "") or payload.get("prompt", "")
    if not user_prompt:
        _emit({})
        return 0

    if not any(pattern.search(user_prompt) for pattern in _FRUSTRATION_PATTERNS):
        _emit({})
        return 0

    # Frustration detected. Emit a brief hint as additionalContext.
    hint = (
        "<chameleon-context>\n"
        "[chameleon: detected frustration phrase]\n"
        "If chameleon is the issue, options:\n"
        "  /chameleon-disable      — suppress for the rest of this session\n"
        "  /chameleon-pause-15m    — pause for 15 minutes (auto-resume)\n"
        "  /chameleon-teach <pattern>  — capture the missed pattern as an idiom\n"
        "If chameleon is unrelated, ignore this note.\n"
        "</chameleon-context>"
    )
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": hint,
        }
    })
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
