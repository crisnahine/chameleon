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
import time
from pathlib import Path


def _emit(output: dict) -> None:
    """Write Claude Code hook output JSON to stdout. Single source of truth."""
    sys.stdout.write(json.dumps(output))
    sys.stdout.write("\n")


def _plugin_data_dir() -> Path:
    """Return the per-user chameleon plugin data dir (override-aware).

    Mirrors chameleon_mcp.plugin_paths.plugin_data_dir but keeps the import
    cost off the hook hot path.
    """
    override = os.environ.get("CHAMELEON_PLUGIN_DATA")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "chameleon"


_TRUST_PROMPT_FILENAME = ".trust_prompted.{session}"
_TRUST_MARKER_TTL_SECONDS = 24 * 3600  # re-prompt after 24h even on resumed session


def _marker_is_fresh(marker_path: Path) -> bool:
    """True if marker exists and was touched within TTL."""
    try:
        st = marker_path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    age = time.time() - st.st_mtime
    return age < _TRUST_MARKER_TTL_SECONDS


def _should_emit_untrusted_prompt(repo_id: str, session_id: str | None) -> bool:
    """BUG-024: emit the untrusted trust prompt at most once per session.

    Writes a marker file at ``${PLUGIN_DATA}/<repo_id>/.trust_prompted.<session>``
    the first time and returns False on every subsequent invocation in
    the same session. Sessions are per-Claude-Code-conversation so the
    user gets a fresh prompt in a new conversation.

    Best-effort: any filesystem error short-circuits to "yes, prompt"
    (the prompt is harmless if duplicated; suppressing it on error would
    hide the trust gate completely).
    """
    if not repo_id or not session_id:
        return True
    try:
        from chameleon_mcp.optouts import _safe_session_marker
        marker_dir = _plugin_data_dir() / repo_id
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker = marker_dir / _TRUST_PROMPT_FILENAME.format(session=_safe_session_marker(session_id))
        if _marker_is_fresh(marker):
            return False
        marker.touch(exist_ok=True)
        return True
    except (OSError, PermissionError):
        return True


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

    Reads tool_input.file_path, calls chameleon_mcp.tools.get_pattern_context,
    emits the result as additionalContext.

    Fast path (Phase 4.5): try the long-lived daemon at
    ``${PLUGIN_DATA}/.daemon.sock``. The daemon holds the python import
    cache + profile state hot between hook calls, so warm latency drops
    from 200-500 ms (subprocess) to sub-100 ms (socket roundtrip).

    Fallback: if the daemon is unreachable / slow / returned an error,
    fall through to the in-process get_pattern_context call. The two
    paths are wire-equivalent; the daemon is purely a latency
    optimization, not a correctness layer.

    On first call from a session, we kick off a background daemon spawn
    so the SECOND hook call sees the daemon ready. The current call
    proceeds via the in-process path either way.
    """
    _started = time.time()

    def _elapsed() -> int:
        return int((time.time() - _started) * 1000)

    def _metric(
        *,
        advisory_emitted: bool,
        repo_id: str | None = None,
        suppression_reason: str | None = None,
        fail_open: bool = False,
        trust_state: str | None = None,
        archetype: str | None = None,
        confidence: str | None = None,
    ) -> None:
        try:
            from chameleon_mcp.metrics import emit_hook_metric
            emit_hook_metric(
                "preflight-and-advise",
                elapsed_ms=_elapsed(),
                repo_id=repo_id,
                advisory_emitted=advisory_emitted,
                suppression_reason=suppression_reason,
                fail_open=fail_open,
                trust_state=trust_state,
                archetype=archetype,
                confidence=confidence,
            )
        except Exception:
            pass

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
    repo_id_hint: str | None = None
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
            _metric(advisory_emitted=False, repo_id=repo_id_hint, suppression_reason=suppressed)
            _emit({})
            return 0
    except Exception:
        # Suppression check should never block — fail open into normal flow
        pass

    # Fast path: try the daemon. On any failure (None response), kick off
    # a background spawn (no-op if it's already running or in flight) and
    # fall through to the in-process call. The hook's 2s ceiling means we
    # cannot afford to wait for a cold daemon — the warm hits show up on
    # the second invocation onwards.
    result: dict | None = None
    try:
        from chameleon_mcp import daemon_client

        result = daemon_client.call("get_pattern_context", {"file_path": file_path})
    except Exception:
        result = None

    if result is None:
        # Kick off the daemon for next time, then run in-process now.
        try:
            from chameleon_mcp.daemon import ensure_daemon_async

            ensure_daemon_async()
        except Exception:
            pass
        try:
            from chameleon_mcp.tools import get_pattern_context
            result = get_pattern_context(file_path)
        except Exception:
            # Fail-open per ARCHITECTURE.md — never block edits on advisor failure
            _metric(advisory_emitted=False, repo_id=repo_id_hint, fail_open=True)
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
        repo_id = repo_info.get("id") or repo_id_hint
        _metric(advisory_emitted=False, repo_id=repo_id, trust_state=trust_state)
        _emit({})
        return 0

    # Record a drift observation BEFORE the trust gate. Drift recording is
    # internal observability and stays useful regardless of whether we
    # inject canonical content for this edit. Failure must not block the
    # edit.
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

    # BUG-024: gate canonical injection on trust_state. Pre-v0.5.6 the hook
    # injected the full canonical witness even when the user had not
    # granted trust, contradicting the using-chameleon skill ("if
    # untrusted, surface trust prompt once, proceed without injection
    # until trusted"). For an untrusted profile we now:
    #   1. Emit a one-time trust prompt (per session) suggesting
    #      /chameleon-trust, and
    #   2. Suppress the canonical excerpt and rules until the user trusts.
    # The "once per session" tracking uses a marker file under the per-
    # repo plugin-data dir.
    session_id = payload.get("session_id")
    if trust_state == "untrusted" and repo_id:
        if _should_emit_untrusted_prompt(repo_id, session_id):
            block = (
                "<chameleon-context>\n"
                "[chameleon: profile present, untrusted]\n\n"
                "A `.chameleon/` profile exists in this repo but the user "
                "has not granted trust for it yet. Surface this to your "
                "human partner once and suggest:\n\n"
                "    /chameleon-trust\n\n"
                "Chameleon will not inject canonical excerpts or team "
                "idioms into your context until trust is granted. The "
                "edit may still proceed; this is an advisory gate, not a "
                "hard deny.\n"
                "</chameleon-context>"
            )
            _metric(
                advisory_emitted=True,
                repo_id=repo_id,
                trust_state="untrusted",
                archetype=archetype_name,
                confidence=confidence_band,
            )
            _emit({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": block,
                }
            })
            return 0
        # Already prompted this session — stay silent.
        _metric(
            advisory_emitted=False,
            repo_id=repo_id,
            suppression_reason="session_disable",
            trust_state="untrusted",
            archetype=archetype_name,
            confidence=confidence_band,
        )
        _emit({})
        return 0

    # Build a short context block; cap at 1500 tokens approx via char limit
    excerpt_content = canonical.get("content") or ""
    rules_count = len(data.get("rules") or [])
    idioms_text = data.get("idioms") or ""
    has_idioms = bool(idioms_text.strip())
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context
    safe_name = sanitize_for_chameleon_context(archetype_name or "")
    safe_band = sanitize_for_chameleon_context(confidence_band or "unknown")
    block = (
        "<chameleon-context>\n"
        f"[chameleon: archetype={safe_name}, "
        f"confidence={safe_band}]\n\n"
    )
    if trust_state == "stale":
        # BUG-NEW-011 (v0.5.7): explain the "stale" cause. Pre-fix the
        # message just said "Trust is stale" without saying why, which was
        # confusing right after a user trusted then refreshed. Refresh
        # invalidates the trust grant because the profile sha changes.
        block += (
            "**Trust is stale**: a recent /chameleon-refresh (or manual edit) "
            "changed `.chameleon/profile.json` after the trust grant. Trust is "
            "tied to the profile sha, so the grant no longer covers the current "
            "profile. Suggest /chameleon-trust to re-confirm. Do not block the "
            "edit; chameleon advisory is provided below for reference only.\n\n"
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

    _metric(
        advisory_emitted=True,
        repo_id=repo_id,
        trust_state=trust_state,
        archetype=archetype_name,
        confidence=confidence_band,
    )
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
#
# BUG-NEW-014 (v0.5.7): expanded coverage. Pre-fix the patterns missed
# the obvious frustration markers ("annoying", "hate", expletives) and
# over-triggered on a bare "stop" in any context ("don't stop now"). The
# expanded set:
#   - keeps interjection markers (ugh, argh, wtf, nope) but drops solo "stop"
#     to avoid false positives;
#   - adds plain-English unhappiness ("hate", "annoying", "frustrating", "useless");
#   - adds common expletives that almost always co-occur with frustration;
#   - keeps chameleon-specific patterns explicit.
_FRUSTRATION_PATTERNS = (
    re.compile(r"\b(ugh|argh|wtf|nope|sucks|useless|dumb)\b", re.IGNORECASE),
    re.compile(r"\b(annoying|annoyed|frustrating|frustrated|hate|hating)\b", re.IGNORECASE),
    re.compile(r"\b(damn|fuck|fucking|shit|crap|bullshit)\b", re.IGNORECASE),
    re.compile(r"this isn'?t right", re.IGNORECASE),
    re.compile(r"don'?t (do|use|inject) (that|this)", re.IGNORECASE),
    re.compile(r"chameleon\s+is\s+(slow|wrong|broken|annoying|useless)", re.IGNORECASE),
    re.compile(r"stop (injecting|using|doing|adding)", re.IGNORECASE),
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
