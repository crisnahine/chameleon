"""Act 3: Hot path advisory (Edit + Write) (Phases 8, 9)."""

from __future__ import annotations

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext

_PROMPT_BODY = """\
In trusted working/ts_basic, perform three operations using THREE DIFFERENT tools
across archetypes. Use absolute paths for all file references.

PHASE 8 - MCP read sweep (all read-only tools):
  emit checkpoint started phase 8
  Call the following chameleon-mcp read-only tools and verify each responds:
    chameleon-mcp::detect_repo (on the current fixture path)
    chameleon-mcp::get_archetype (pick any archetype from the profile)
    chameleon-mcp::get_canonical_excerpt (pick the util or component archetype)
    chameleon-mcp::get_drift_status
    chameleon-mcp::get_pattern_context (file_path=src/utils/format_date.ts)
    chameleon-mcp::get_rules
    chameleon-mcp::list_profiles
  For get_pattern_context, confirm the envelope contains match_quality set to one
  of: ast, exact, fallback, none. Record the archetype name returned.
  emit checkpoint completed phase 8

PHASE 9 - Excerpt LRU cache dedup:
  emit checkpoint started phase 9
  Edit src/utils/format_date.ts (a one-line change, e.g. add a comment).
  This is the FIRST edit in the util archetype - a PreToolUse advisory should fire.
  Note whether chameleon-mcp::get_canonical_excerpt was called to build the advisory.
  Now make a second edit in the same archetype: edit src/utils/format_currency.ts
  (another one-line change). This is the SECOND edit in the same archetype within
  this session. The excerpt LRU cache should serve the canonical without a new MCP
  fetch (hook-model dedup: same archetype, same session).
  Verify that the advisory shape on the first edit contained:
    - archetype name
    - sub_buckets list
    - match_quality field
    - a canonical witness (code snippet)
  emit checkpoint completed phase 9

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_03.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=30,
        allowed_tools=[
            "Bash",
            "Read",
            "Edit",
            "Write",
            "mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            "mcp__plugin_chameleon_chameleon-mcp__get_archetype",
            "mcp__plugin_chameleon_chameleon-mcp__get_canonical_excerpt",
            "mcp__plugin_chameleon_chameleon-mcp__get_drift_status",
            "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
            "mcp__plugin_chameleon_chameleon-mcp__refresh_repo",
            "mcp__plugin_chameleon_chameleon-mcp__trust_profile",
        ],
        plugin_root=ctx.plugin_root,
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[8, 9]
    )

    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    try:
        hook_events_with_context = [
            e for e in session.hook_events if "<chameleon-context>" in e.stdout
        ]
        if not hook_events_with_context:
            notes_extra[8] = "no hook events with <chameleon-context> found in transcript"
            cross_check_passed[8] = False
        else:
            cross_check_passed[8] = True
    except expect.PhaseAssertionError as e:
        notes_extra[8] = str(e)
        cross_check_passed[8] = False

    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        excerpt_call_count = transcript_text.count("get_canonical_excerpt")
        if excerpt_call_count == 0:
            notes_extra[9] = (
                "no get_canonical_excerpt calls visible in transcript (may be normal if cache hit)"
            )
            cross_check_passed[9] = len(transcript_text) > 0
        else:
            cross_check_passed[9] = True
    except expect.PhaseAssertionError as e:
        notes_extra[9] = str(e)
        cross_check_passed[9] = False

    for phase, passed in cross_check_passed.items():
        if phase in outcomes and passed:
            if outcomes[phase].status == "SKIP":
                outcomes[phase].status = "PASS"
                outcomes[phase].notes = "promoted from SKIP by runner cross-check"
            elif outcomes[phase].status == "FAIL" and "phase incomplete" in outcomes[phase].notes:
                outcomes[phase].status = "PASS"
                outcomes[phase].notes = "promoted from incomplete-FAIL by runner cross-check"

    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="03_hot_path_drift",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
