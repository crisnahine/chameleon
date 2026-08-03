"""Unit tests for stream-json parsing (no actual claude spawn)."""

from __future__ import annotations

from pathlib import Path

from tests.journey.harness import claude
from tests.journey.harness.claude import (
    ClaudeSession,
    abnormal_termination,
    parse_stream_json,
)

SAMPLE_STREAM = """
{"type": "system", "subtype": "init", "session_id": "abc"}
{"type": "system", "subtype": "hook_response", "hook_name": "PreToolUse:Edit", "stdout": "{\\"hookSpecificOutput\\":{\\"additionalContext\\":\\"<chameleon-context>archetype=util</chameleon-context>\\"}}"}
{"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "name": "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context", "input": {}}]}}
{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__plugin_chameleon_chameleon-mcp__lint_file", "input": {}}]}}
{"type": "result", "subtype": "success", "total_cost_usd": 0.12, "duration_ms": 4200}
""".strip()


def test_parse_cost() -> None:
    parsed = parse_stream_json(SAMPLE_STREAM)
    assert parsed.cost_usd == 0.12


def test_parse_hook_events() -> None:
    parsed = parse_stream_json(SAMPLE_STREAM)
    pre_tool_events = [e for e in parsed.hook_events if e.hook_name == "PreToolUse:Edit"]
    assert len(pre_tool_events) == 1
    assert "<chameleon-context>" in pre_tool_events[0].stdout


def test_parse_malformed_lines_skipped() -> None:
    """Malformed JSON lines are skipped, not raised."""
    stream = '{"type": "system", "subtype": "init"}\nthis is junk\n{"type": "result", "total_cost_usd": 0.05}'
    parsed = parse_stream_json(stream)
    assert parsed.cost_usd == 0.05


def test_parse_tool_uses() -> None:
    """tool_use block names are collected from assistant messages, in order."""
    parsed = parse_stream_json(SAMPLE_STREAM)
    assert parsed.tool_uses == [
        "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
        "mcp__plugin_chameleon_chameleon-mcp__lint_file",
    ]
    assert sum(1 for n in parsed.tool_uses if "get_pattern_context" in n) == 1
    assert sum(1 for n in parsed.tool_uses if "lint_file" in n) == 1


def test_parse_tool_uses_empty_when_no_tool_use() -> None:
    """A transcript with only text blocks yields an empty tool_uses list."""
    stream = (
        '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}\n'
        '{"type": "result", "total_cost_usd": 0.01}'
    )
    parsed = parse_stream_json(stream)
    assert parsed.tool_uses == []


def test_parser_extracts_session_id_and_result_text() -> None:
    stream = "\n".join(
        [
            '{"type": "system", "subtype": "init", "session_id": "sess-abc123"}',
            '{"type": "result", "total_cost_usd": 0.42, "result": "WINNER: A\\nbecause it reuses the helper."}',
        ]
    )
    parsed = parse_stream_json(stream)
    assert parsed.session_id == "sess-abc123"
    assert parsed.result_text.startswith("WINNER: A")
    assert parsed.cost_usd == 0.42


def test_parser_extracts_bash_commands() -> None:
    stream = "\n".join(
        [
            '{"type": "assistant", "message": {"content": ['
            '{"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}},'
            '{"type": "tool_use", "name": "Edit", "input": {"file_path": "/x.ts"}}]}}',
        ]
    )
    parsed = parse_stream_json(stream)
    assert parsed.bash_commands == ["npm test"]
    assert parsed.tool_uses == ["Bash", "Edit"]


def test_parser_defaults_for_streams_without_new_fields() -> None:
    parsed = parse_stream_json('{"type": "result", "total_cost_usd": 0.1}')
    assert parsed.session_id == ""
    assert parsed.result_text == ""
    assert parsed.bash_commands == []


# A run that ends on a deferred tool. Note subtype "success", is_error false and
# an empty result: nothing outside terminal_reason / deferred_tool_use says the
# Edit was handed back instead of executed.
DEFERRED_RESULT_FRAME = (
    '{"type": "result", "subtype": "success", "is_error": false, "num_turns": 7,'
    ' "total_cost_usd": 0.21, "result": "", "session_id": "sess-deferred",'
    ' "stop_reason": "tool_deferred", "terminal_reason": "tool_deferred",'
    ' "deferred_tool_use": {"id": "toolu_01", "name": "Edit",'
    ' "input": {"file_path": "/x.ts"}}}'
)


def test_parser_extracts_the_deferred_end_state() -> None:
    parsed = parse_stream_json(DEFERRED_RESULT_FRAME)
    assert parsed.terminal_reason == "tool_deferred"
    assert parsed.stop_reason == "tool_deferred"
    assert parsed.deferred_tool == "Edit"


def test_parser_extracts_a_completed_end_state() -> None:
    stream = (
        '{"type": "result", "subtype": "success", "total_cost_usd": 0.12,'
        ' "stop_reason": "end_turn", "terminal_reason": "completed", "result": "done"}'
    )
    parsed = parse_stream_json(stream)
    assert parsed.terminal_reason == "completed"
    assert parsed.stop_reason == "end_turn"
    assert parsed.deferred_tool == ""


def test_parser_leaves_the_end_state_empty_without_a_result_frame() -> None:
    parsed = parse_stream_json('{"type": "system", "subtype": "init", "session_id": "s"}')
    assert parsed.terminal_reason == ""
    assert parsed.stop_reason == ""
    assert parsed.deferred_tool == ""


# A background-task wakeup segment of the SAME session. When a subagent task
# finishes after the spawned run has ended its turn, the CLI wakes the session
# and runs another turn segment, which flushes its own result frame after the
# spawned run's. Copied from the shape real transcripts carry: same session_id,
# an `origin` naming the wakeup, num_turns per-segment and total_cost_usd
# cumulative.
WAKEUP_RESULT_FRAME = (
    '{"type": "result", "subtype": "success", "is_error": false, "num_turns": 45,'
    ' "total_cost_usd": 8.47, "result": "Acknowledged the background result.",'
    ' "session_id": "sess-1", "stop_reason": "end_turn", "terminal_reason": "completed",'
    ' "origin": {"kind": "task-notification"}}'
)


def test_parser_reads_the_end_state_of_the_spawned_run_not_a_wakeup() -> None:
    """The spawned run hit the turn cap; the wakeup that followed it did not.

    Last-frame-wins would report the wakeup's clean "completed" and the act
    would go green on a worker that stopped mid-tool.
    """
    stream = "\n".join(
        [
            '{"type": "result", "subtype": "error_max_turns", "is_error": true,'
            ' "num_turns": 56, "total_cost_usd": 3.90, "result": "", "session_id": "sess-1",'
            ' "stop_reason": "tool_use", "terminal_reason": "max_turns"}',
            WAKEUP_RESULT_FRAME,
        ]
    )
    parsed = parse_stream_json(stream)
    assert parsed.terminal_reason == "max_turns"
    assert parsed.stop_reason == "tool_use"
    assert parsed.result_text == ""
    # Cost stays last-wins: total_cost_usd is cumulative over the whole session,
    # so the spawned run's own frame undercounts what the run actually spent.
    assert parsed.cost_usd == 8.47
    assert "max_turns" in abnormal_termination(parsed)


def test_a_wakeup_cannot_swallow_a_deferred_tool() -> None:
    """The failure --setting-sources exists to catch, re-entered through the
    wakeup door: the Edit was handed back un-executed, and the wakeup frame that
    follows says the session completed."""
    stream = "\n".join([DEFERRED_RESULT_FRAME, WAKEUP_RESULT_FRAME])
    parsed = parse_stream_json(stream)
    assert parsed.terminal_reason == "tool_deferred"
    assert parsed.deferred_tool == "Edit"
    reason = abnormal_termination(parsed)
    assert "tool_deferred" in reason
    assert "Edit" in reason


def test_parser_falls_back_to_the_last_frame_when_every_frame_is_a_wakeup() -> None:
    """No recorded transcript looks like this, but a CLI that starts tagging
    every frame must still yield an end state rather than none."""
    parsed = parse_stream_json(WAKEUP_RESULT_FRAME)
    assert parsed.terminal_reason == "completed"
    assert parsed.cost_usd == 8.47


def _session(**overrides) -> ClaudeSession:
    fields = {
        "cost_usd": 0.1,
        "hook_events": [],
        "transcript_path": Path("/dev/null"),
        "returncode": 0,
    }
    fields.update(overrides)
    return ClaudeSession(**fields)


def test_abnormal_termination_passes_a_clean_session() -> None:
    assert abnormal_termination(_session(terminal_reason="completed")) == ""


def test_abnormal_termination_names_the_deferred_tool() -> None:
    """The tool name is what makes the reason actionable: it says which write
    the worker never made."""
    reason = abnormal_termination(_session(terminal_reason="tool_deferred", deferred_tool="Write"))
    assert "tool_deferred" in reason
    assert "Write" in reason


def test_abnormal_termination_reports_a_deferral_under_a_clean_end_state() -> None:
    """A deferred tool is fatal on its own terms. The two facts come from
    different result frames once a wakeup ran, so a gate that only looks at the
    deferral when the end state already looks bad can be talked out of the one
    failure it exists to catch."""
    reason = abnormal_termination(
        _session(terminal_reason="completed", deferred_tool="Edit"), work_complete=True
    )
    assert "Edit" in reason


def test_abnormal_termination_flags_the_turn_cap() -> None:
    assert "max_turns" in abnormal_termination(_session(terminal_reason="max_turns"))


def test_abnormal_termination_flags_the_timeout_path() -> None:
    """The timeout path parses nothing, so it states its own end-state."""
    reason = abnormal_termination(_session(returncode=-1, terminal_reason="timeout"))
    assert "timeout" in reason
    assert "-1" in reason


def test_abnormal_termination_flags_a_nonzero_exit_alone() -> None:
    assert "exit code 2" in abnormal_termination(_session(returncode=2))


def test_abnormal_termination_tolerates_a_session_stub() -> None:
    """The effectiveness runner passes its own stub, which may carry neither
    field; a missing attribute must read as clean, not raise."""

    class _Stub:
        returncode = 0

    assert abnormal_termination(_Stub()) == ""


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_spawn_surfaces_a_deferred_session_without_spawning_claude(monkeypatch, tmp_path) -> None:
    """A deferred run exits 0, so spawn_claude's own return value is the only
    place the harness can learn the session stopped mid-task."""
    stream = "\n".join(
        [
            '{"type": "system", "subtype": "init", "session_id": "sess-deferred"}',
            '{"type": "assistant", "message": {"content": ['
            '{"type": "tool_use", "name": "Edit", "input": {"file_path": "/x.ts"}}]}}',
            DEFERRED_RESULT_FRAME,
        ]
    )
    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess(stream)

    monkeypatch.setattr(claude.subprocess, "run", fake_run)
    transcript = tmp_path / "act.txt"
    session = claude.spawn_claude(
        prompt="p",
        cwd=tmp_path,
        env={},
        transcript_path=transcript,
    )

    assert session.returncode == 0
    assert session.terminal_reason == "tool_deferred"
    assert session.deferred_tool == "Edit"
    assert session.cost_usd == 0.21
    assert abnormal_termination(session)
    # The transcript still holds every line the CLI emitted.
    assert transcript.read_text(encoding="utf-8") == stream
    assert captured["args"].count("--setting-sources") == 1
    assert captured["args"][captured["args"].index("--setting-sources") + 1] == "project,local"


def test_spawn_setting_sources_is_operator_overridable(monkeypatch, tmp_path) -> None:
    """Restoring the user source is how a local debug session reproduces a
    deferral, so the default must be an override and not a hardcode."""
    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _FakeCompletedProcess('{"type": "result", "total_cost_usd": 0.0}')

    monkeypatch.setattr(claude.subprocess, "run", fake_run)
    monkeypatch.setenv("CHAMELEON_JOURNEY_SETTING_SOURCES", "user,project,local")
    claude.spawn_claude(
        prompt="p",
        cwd=tmp_path,
        env={},
        transcript_path=tmp_path / "act.txt",
    )
    assert captured["args"][captured["args"].index("--setting-sources") + 1] == "user,project,local"
