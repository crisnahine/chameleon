"""Act 3b: PreToolUse advisory coverage + drift + refresh recovery (Phases 10, 11)."""
from __future__ import annotations

from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Continue in working/ts_basic (profile already bootstrapped and trusted from earlier acts).
Use absolute paths for all file references.

PHASE 10 - PreToolUse advisory fires on Edit + Write + NotebookEdit (or Write fallback):
  emit checkpoint started phase 10
  Perform the following three operations in order:
  1. EDIT an existing util file (e.g. src/utils/slugify.ts): change one line.
     The PreToolUse hook must inject a <chameleon-context> advisory before the edit lands.
  2. WRITE a new component file: create src/components/NewWidget.tsx from scratch
     with a simple React component. The PreToolUse hook must inject advisory before Write.
  3. WRITE a test file: create tests/NewWidget.test.tsx (since no notebook is present,
     this satisfies the NotebookEdit-or-fallback matcher coverage).
     The PreToolUse hook must inject advisory before this Write.
  After all three operations, confirm:
    - Each advisory contained archetype + sub_buckets + match_quality + canonical witness.
    - Total advisory token count stayed under 1500 tokens (estimate based on advisory length).
    - The hook-model dedup fired: repeat edits in the same archetype within this session
      skip injecting a new advisory (no new get_canonical_excerpt call needed).
  emit checkpoint completed phase 10

PHASE 11 - Drift injection + banner + refresh recovery:
  emit checkpoint started phase 11
  Use the Bash tool to inject drift: copy 50 files with unconventional naming into src/utils/:
    for i in $(seq 1 50); do cp src/utils/format_date.ts "src/utils/UNCONVENTIONAL-FILE-${i}.ts"; done
  Then start a fresh sub-session to observe the drift banner. Use Bash to run:
    claude -p "Run chameleon-mcp::get_drift_status and report the result" \\
      --output-format stream-json --max-turns 3 \\
      --permission-mode acceptEdits 2>&1 | head -50
  If the drift score is above threshold, a drift banner should appear in the fresh session.
  After observing the drift, run /chameleon-refresh in the current session.
  After refresh completes, verify:
    - working/ts_basic/.chameleon/profile.json still exists.
    - working/ts_basic/.chameleon/COMMITTED still exists.
    - The trust state is preserved (structural-equality path: no renames, no idiom
      changes, only cluster size shifts from the 50 new files).
    - chameleon-mcp::get_drift_status now returns a lower score or "ok" status.
  emit checkpoint completed phase 11

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_03b.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=60,
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
        permission_mode="bypassPermissions",
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[10, 11]
    )

    notes_extra: dict[int, str] = {}

    # Phase 10: assert at least 3 PreToolUse hook events fired (one each Edit/Write/Write)
    try:
        pre_tool_events = [
            e for e in session.hook_events
            if "PreToolUse" in e.hook_name
        ]
        if len(pre_tool_events) < 3:
            notes_extra[10] = (
                f"expected >= 3 PreToolUse hook events, got {len(pre_tool_events)}"
            )
    except expect.PhaseAssertionError as e:
        notes_extra[10] = str(e)

    # Phase 11: profile.json + COMMITTED still present after refresh
    ts_basic_chameleon = ctx.fixture("ts_basic") / ".chameleon"
    try:
        expect.path_exists(11, ts_basic_chameleon / "profile.json")
        expect.path_exists(11, ts_basic_chameleon / "COMMITTED")
    except expect.PhaseAssertionError as e:
        notes_extra[11] = str(e)

    # Apply cross-check findings to outcomes.
    # Cross-checks are advisory: they append CONCERN to notes without demoting PASS to FAIL.
    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="03b_drift_refresh",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
