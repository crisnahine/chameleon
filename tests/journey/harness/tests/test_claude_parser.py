"""Unit tests for stream-json parsing (no actual claude spawn)."""
from __future__ import annotations

from tests.journey.harness.claude import parse_stream_json

SAMPLE_STREAM = """
{"type": "system", "subtype": "init", "session_id": "abc"}
{"type": "system", "subtype": "hook_response", "hook_name": "PreToolUse:Edit", "stdout": "{\\"hookSpecificOutput\\":{\\"additionalContext\\":\\"<chameleon-context>archetype=util</chameleon-context>\\"}}"}
{"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
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
