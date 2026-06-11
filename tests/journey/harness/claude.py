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
    # Final result text from the terminal "result" event ("" when absent).
    # Lets a caller read a one-turn judge's answer without re-parsing raw lines.
    result_text: str = ""
    # Commands of every Bash tool_use block, in order. Used to detect test
    # invocations on transcripts where hooks (and so the exec log) were disabled.
    bash_commands: list[str] = dataclasses.field(default_factory=list)


def parse_stream_json(stream: str) -> ParsedSession:
    """Parse a stream-json transcript. Malformed lines are skipped."""
    cost = 0.0
    hook_events: list[HookEvent] = []
    raw_lines: list[str] = []
    tool_uses: list[str] = []
    session_id = ""
    result_text = ""
    bash_commands: list[str] = []

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
            cost = float(obj.get("total_cost_usd", 0.0))
            text = obj.get("result")
            if isinstance(text, str):
                result_text = text
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

    return ParsedSession(
        cost_usd=cost,
        hook_events=hook_events,
        raw_lines=raw_lines,
        tool_uses=tool_uses,
        session_id=session_id,
        result_text=result_text,
        bash_commands=bash_commands,
    )


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


def spawn_claude(
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    transcript_path: Path,
    max_turns: int = 25,
    allowed_tools: list[str] | None = None,
    permission_mode: str = "bypassPermissions",
    timeout_s: int = 900,
    model: str = "sonnet",
    plugin_root: Path | None = None,
    add_dirs: list[Path] | None = None,
) -> ClaudeSession:
    """Spawn `claude -p` and capture its stream-json output."""
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
    ]
    if plugin_root is not None:
        args += ["--plugin-dir", str(plugin_root)]
    if allowed_tools:
        args += ["--allowedTools", ",".join(allowed_tools)]
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
    )
