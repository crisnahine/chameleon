"""Common types + helpers used by all act modules."""

from __future__ import annotations

import dataclasses
import json

from tests.journey.harness.checkpoints import PhaseOutcome

# Fully-qualified names of the three dispatcher tools (v3 MCP surface split).
# The folded lifecycle / review / telemetry operations are invoked as
# `action` arguments on these; acts allow-list the dispatcher, not the action.
MCP_LIFECYCLE = "mcp__plugin_chameleon_chameleon-mcp__chameleon_lifecycle"
MCP_REVIEW = "mcp__plugin_chameleon_chameleon-mcp__chameleon_review"
MCP_TELEMETRY = "mcp__plugin_chameleon_chameleon-mcp__chameleon_telemetry"


@dataclasses.dataclass
class ActResult:
    act_id: str
    cost_usd: float
    phase_outcomes: list[PhaseOutcome]
    checkpoint_parse_errors: int = 0
    notes: str = ""


def dispatcher_actions(session, dispatcher: str) -> list[str]:
    """Action strings of every tool_use of the given dispatcher tool, in order.

    session.tool_uses carries only tool-block NAMES, so a folded operation
    routed through a dispatcher (chameleon_lifecycle / chameleon_review /
    chameleon_telemetry) is invisible there. This re-parses the raw
    stream-json assistant events and returns each matching tool_use's input
    `action`. A real tool_use block cannot be faked by transcript prose, so
    acts can assert dispatcher-routed calls with the same strength as the
    name-based tool_use checks.

    `dispatcher` is substring-matched against the block name, so both the
    bare tool name ("chameleon_review") and the fully-qualified form work.
    """
    actions: list[str] = []
    # ClaudeSession carries no raw_lines (only ParsedSession does); the
    # stream-json events live in the transcript file it points at.
    try:
        raw_lines = session.transcript_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return actions
    for line in raw_lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            if dispatcher not in (block.get("name") or ""):
                continue
            action = (block.get("input") or {}).get("action")
            if isinstance(action, str):
                actions.append(action)
    return actions


_CHECKPOINT_PREAMBLE = """\
At the END of each phase (after running all its steps), emit a checkpoint by running this Bash command:

  echo '{"phase": <N>, "status": "passed"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

If an assertion fails inside the phase, emit:

  echo '{"phase": <N>, "status": "failed", "notes": "what failed"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

ONE checkpoint per phase, after the phase completes (or fails). Do NOT emit a "started" event. Emit each checkpoint as a SINGLE LINE outside any code fence.
"""


def checkpoint_preamble() -> str:
    return _CHECKPOINT_PREAMBLE


def build_act_prompt(body: str) -> str:
    return checkpoint_preamble() + "\n\n" + body
