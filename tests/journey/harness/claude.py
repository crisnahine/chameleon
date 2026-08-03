"""Spawn `claude -p` subprocess and parse stream-json output.

The parser is split from spawn_claude() so we can unit-test it without
spawning real Claude.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path


@dataclasses.dataclass
class HookEvent:
    hook_name: str
    stdout: str


@dataclasses.dataclass
class ParsedSession:
    cost_usd: float
    hook_events: list[HookEvent]
    raw_lines: list[str]
    # Names of every tool_use block emitted by the assistant, in order. A real
    # tool invocation cannot be faked by prose in the transcript text, so acts
    # use this to assert a command actually called a given MCP tool. Backward
    # compatible: defaults to an empty list for callers that ignore it.
    tool_uses: list[str] = dataclasses.field(default_factory=list)
    # First session_id seen on any event object. Hooks key per-session state
    # (exec log, enforcement markers) on this id; scorers need it to read those
    # records back. Empty when no event carried one.
    session_id: str = ""
    # Result text of the spawned run's own result frame ("" when absent). Lets a
    # caller read a one-turn judge's answer without re-parsing raw lines.
    result_text: str = ""
    # Commands of every Bash tool_use block, in order. Used to detect test
    # invocations on transcripts where hooks (and so the exec log) were disabled.
    bash_commands: list[str] = dataclasses.field(default_factory=list)
    # End-state the spawned run reports for itself (see _own_run_frame for which
    # of several result frames that is). Without these a session that ended on a
    # deferred tool or the turn cap parses as an ordinary zero-cost success: such
    # a frame carries subtype "success" and is_error false, and only these fields
    # say the work never finished. `deferred_tool` names the tool the CLI handed
    # back un-executed in ANY segment of the session, not only the spawned run.
    terminal_reason: str = ""
    stop_reason: str = ""
    deferred_tool: str = ""


# Marks a result frame the CLI emitted for a background-task wakeup rather than
# for the run the harness asked for.
_WAKEUP_ORIGIN_KIND = "task-notification"


def _is_wakeup_frame(frame: dict) -> bool:
    origin = frame.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == _WAKEUP_ORIGIN_KIND


def _own_run_frame(frames: list[dict]) -> dict | None:
    """The result frame belonging to the run the harness spawned.

    One `claude -p` process can emit SEVERAL result frames. When a background
    subagent task finishes after the main run has already ended its turn, the
    CLI wakes the same session -- same session_id, a fresh system/init -- and
    runs another turn segment, which emits its own result frame carrying
    `origin: {"kind": "task-notification"}`. Every frame is flushed at the end of
    stdout in creation order, so a last-frame-wins rule reads the LAST WAKEUP's
    end state instead of the spawned run's. That is biased toward silence: a
    wakeup normally just acknowledges a background result and ends completed, so
    it overwrites a max_turns or a tool_deferred the run itself reported.

    Every recorded transcript emits its un-origined frame first, so the first
    non-wakeup frame is the answer. Falling back to the last frame covers a CLI
    that starts tagging every frame: an end state read from the wrong segment
    still beats reporting none at all.
    """
    for frame in frames:
        if not _is_wakeup_frame(frame):
            return frame
    return frames[-1] if frames else None


def parse_stream_json(stream: str) -> ParsedSession:
    """Parse a stream-json transcript. Malformed lines are skipped."""
    cost = 0.0
    hook_events: list[HookEvent] = []
    raw_lines: list[str] = []
    tool_uses: list[str] = []
    session_id = ""
    result_text = ""
    bash_commands: list[str] = []
    terminal_reason = ""
    stop_reason = ""
    deferred_tool = ""
    result_frames: list[dict] = []

    for line in stream.splitlines():
        line = line.strip()
        if not line:
            continue
        raw_lines.append(line)
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not session_id:
            sid = obj.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid

        if obj.get("type") == "result":
            result_frames.append(obj)
        elif obj.get("type") == "system" and obj.get("subtype") == "hook_response":
            hook_events.append(
                HookEvent(
                    hook_name=obj.get("hook_name", ""),
                    stdout=obj.get("stdout", ""),
                )
            )
        elif obj.get("type") == "assistant":
            message = obj.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name")
                        if isinstance(name, str) and name:
                            tool_uses.append(name)
                        if name == "Bash":
                            cmd = (block.get("input") or {}).get("command")
                            if isinstance(cmd, str):
                                bash_commands.append(cmd)

    for frame in result_frames:
        # total_cost_usd is cumulative over every segment of the session, so the
        # LAST frame carries the whole run's spend -- attributing cost to the
        # spawned run's frame alone would undercount a fan-out act by roughly
        # half. A deferral stays sticky across every frame for the opposite
        # reason: a tool handed back un-executed in any segment is still a write
        # that never happened.
        cost = float(frame.get("total_cost_usd", 0.0))
        deferred = frame.get("deferred_tool_use")
        if isinstance(deferred, dict):
            deferred_name = deferred.get("name")
            if isinstance(deferred_name, str):
                deferred_tool = deferred_name

    own_frame = _own_run_frame(result_frames)
    if own_frame is not None:
        reason = own_frame.get("terminal_reason")
        if isinstance(reason, str):
            terminal_reason = reason
        stop = own_frame.get("stop_reason")
        if isinstance(stop, str):
            stop_reason = stop
        text = own_frame.get("result")
        if isinstance(text, str):
            result_text = text

    return ParsedSession(
        cost_usd=cost,
        hook_events=hook_events,
        raw_lines=raw_lines,
        tool_uses=tool_uses,
        session_id=session_id,
        result_text=result_text,
        bash_commands=bash_commands,
        terminal_reason=terminal_reason,
        stop_reason=stop_reason,
        deferred_tool=deferred_tool,
    )


def abnormal_termination(session, *, work_complete: bool = False) -> str:
    """Why a session did not end cleanly, or "" when it did.

    A session that ended on a deferred tool or on the turn cap still exits 0
    and still reports subtype "success", so a return code alone cannot tell a
    worker that finished its task from one that stopped mid-way and handed an
    un-executed Edit/Write back to the caller. Callers use the returned reason
    to mark such a run failed instead of scoring whatever partial state it left
    behind. Reads through getattr because some callers pass their own session
    stub rather than a ClaudeSession.

    `work_complete` says the caller already holds the whole result it expected
    of the session -- every checkpoint an act declared, a scoreable diff for an
    eval cell. Hitting the turn cap after that is a budget fact, not a lost
    result, and failing on it would discard work the session demonstrably
    finished. A deferred tool is never survivable that way: the CLI handed an
    Edit or Write back un-executed, so the state the session saw is not the
    state it asked for, and that is the condition `--setting-sources` exists to
    prevent rather than to tolerate quietly.

    A deferred tool is therefore checked on its own terms, not under the
    end-state check: the two facts come from different result frames whenever a
    background-task wakeup ran, so gating the deferral behind an end state that
    already looks bad would swallow the exact failure this function exists to
    catch.
    """
    reasons: list[str] = []
    terminal_reason = getattr(session, "terminal_reason", "") or ""
    deferred_tool = getattr(session, "deferred_tool", "") or ""
    if deferred_tool:
        reasons.append(f"{terminal_reason or 'tool_deferred'} ({deferred_tool} never executed)")
    elif terminal_reason and terminal_reason != "completed":
        if terminal_reason == "tool_deferred" or not work_complete:
            reasons.append(terminal_reason)
    returncode = getattr(session, "returncode", 0) or 0
    if returncode != 0:
        reasons.append(f"exit code {returncode}")
    return "; ".join(reasons)


@dataclasses.dataclass
class ClaudeSession:
    cost_usd: float
    hook_events: list[HookEvent]
    transcript_path: Path
    returncode: int
    # Tool-use block names the assistant emitted, in order (see ParsedSession).
    tool_uses: list[str] = dataclasses.field(default_factory=list)
    # See ParsedSession for the semantics of these three; the timeout path
    # returns the empty defaults because no parse happened.
    session_id: str = ""
    result_text: str = ""
    bash_commands: list[str] = dataclasses.field(default_factory=list)
    # See ParsedSession. The timeout path fills terminal_reason itself, since a
    # killed process leaves no result frame to parse an end-state out of.
    terminal_reason: str = ""
    stop_reason: str = ""
    deferred_tool: str = ""


def spawn_claude(
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    transcript_path: Path,
    max_turns: int = 25,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    permission_mode: str = "bypassPermissions",
    timeout_s: int = 900,
    model: str | None = None,
    plugin_root: Path | None = None,
    add_dirs: list[Path] | None = None,
) -> ClaudeSession:
    """Spawn `claude -p` and capture its stream-json output."""
    # Every act calls this without a model, so the worker model is a run-wide
    # property rather than a per-act one. Resolving it here lets the runner's
    # --model reach all ~20 acts through one env var instead of threading a
    # parameter through each. The bare "sonnet" alias floats to whatever the
    # latest Sonnet is, which is wrong for a release gate whose results get
    # compared across runs -- pass an exact id to pin it.
    model = model or os.environ.get("CHAMELEON_JOURNEY_MODEL") or "sonnet"
    args = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--max-turns",
        str(max_turns),
        "--model",
        model,
        "--permission-mode",
        permission_mode,
        # Harness workers must be hermetic plain sessions. A user-level
        # settings.json can carry multi-agent orchestration directives
        # (ultracode / workflows), under which a worker delegates its small
        # task to background agents, idles on wakeups, and dies at the turn
        # cap — poisoning every arm's cells. Override them per-spawn; the
        # user's real sessions are unaffected.
        "--settings",
        '{"ultracode": false, "enableWorkflows": false}',
        # Load project and local settings only. A user-installed plugin can
        # register a PreToolUse hook that answers `permissionDecision: "defer"`;
        # in print mode the CLI honours that by ending the run and handing the
        # Edit/Write back to the caller un-executed, so the worker looks dead
        # and its file writes never happen. Dropping the user source drops
        # those plugins. The chameleon plugin still loads through --plugin-dir
        # and every one of its hooks still fires, and a fixture's own
        # .claude/settings*.json is still read.
        "--setting-sources",
        os.environ.get("CHAMELEON_JOURNEY_SETTING_SOURCES") or "project,local",
    ]
    if plugin_root is not None:
        args += ["--plugin-dir", str(plugin_root)]
    if allowed_tools:
        args += ["--allowedTools", ",".join(allowed_tools)]
    if disallowed_tools:
        # --allowedTools only PRE-AUTHORIZES under bypassPermissions; it does
        # not remove other tools. Orchestration tools (Agent/Task/Skill/
        # ScheduleWakeup) reached via user-level plugins must be blocked
        # explicitly or a worker delegates its task instead of doing it.
        args += ["--disallowedTools", ",".join(disallowed_tools)]
    if add_dirs:
        for d in add_dirs:
            args += ["--add-dir", str(d)]

    merged_env = os.environ.copy()
    merged_env.update(env)
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        transcript_path.write_text(raw, encoding="utf-8")
        return ClaudeSession(
            cost_usd=0.0,
            hook_events=[],
            transcript_path=transcript_path,
            returncode=-1,
            terminal_reason="timeout",
        )

    transcript_path.write_text(proc.stdout, encoding="utf-8")
    parsed = parse_stream_json(proc.stdout)
    return ClaudeSession(
        cost_usd=parsed.cost_usd,
        hook_events=parsed.hook_events,
        transcript_path=transcript_path,
        returncode=proc.returncode,
        tool_uses=parsed.tool_uses,
        session_id=parsed.session_id,
        result_text=parsed.result_text,
        bash_commands=parsed.bash_commands,
        terminal_reason=parsed.terminal_reason,
        stop_reason=parsed.stop_reason,
        deferred_tool=parsed.deferred_tool,
    )
