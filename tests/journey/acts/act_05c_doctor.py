"""Act 5c: Doctor stale errors filter (Phase 18)."""
from __future__ import annotations

from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect, mcp
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Run doctor checks in working/ts_basic (profile bootstrapped and trusted from earlier acts).
Use absolute paths for all file references.

PHASE 18 - doctor stale errors filter:
  FIRST: emit checkpoint started phase 18 NOW.
  Corrupt .chameleon/canonicals.json:
    echo "XXXXX" > .chameleon/canonicals.json
  Run /chameleon-doctor. Verify per_repo_state subsystem shows status: error.
  Test the 72h stale filter:
    OLD_TS=$(date -u -d "4 days ago" +%FT%TZ 2>/dev/null || date -u -v-4d +%FT%TZ)
    echo "[${OLD_TS}] OLD-ERROR: stale hook failure" >> "$CHAMELEON_HOOK_ERROR_LOG"
    echo "[$(date -u +%FT%TZ)] FRESH-ERROR: recent hook failure" >> "$CHAMELEON_HOOK_ERROR_LOG"
    touch -t $(date -u +"%Y%m%d%H%M.%S" -d "4 days ago" 2>/dev/null || date -u -v-4d +"%Y%m%d%H%M.%S") "$CHAMELEON_HOOK_ERROR_LOG"
  Run /chameleon-doctor again. Verify recent_errors shows only FRESH-ERROR, not OLD-ERROR.
  Restore canonicals.json:
    echo '{}' > .chameleon/canonicals.json
  emit checkpoint completed phase 18.

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_05c.txt"
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
            "mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            "mcp__plugin_chameleon_chameleon-mcp__doctor",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
        ],
        plugin_root=ctx.plugin_root,
        permission_mode="bypassPermissions",
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[18]
    )

    notes_extra: dict[int, str] = {}

    # Phase 18: age hook_errors.log entries and verify doctor filters them
    try:
        hook_error_log = ctx.hook_error_log
        if hook_error_log.exists():
            ctx.fast_forward_marker(hook_error_log, age_seconds=4 * 24 * 3600)
            try:
                doctor_result = mcp.call_mcp_tool(
                    tool_name="doctor",
                    plugin_root=ctx.plugin_root,
                    env={**ctx.env, "CHAMELEON_PLUGIN_DATA": str(ctx.plugin_data_dir)},
                    timeout_s=30,
                )
                recent_errors = doctor_result.get("recent_errors", {})
                if isinstance(recent_errors, dict) and recent_errors.get("status") == "error":
                    error_msg = str(recent_errors.get("message", ""))
                    if "OLD-ERROR" in error_msg:
                        notes_extra[18] = "doctor showed old error (72h filter did not fire)"
            except Exception:
                pass
    except expect.PhaseAssertionError as e:
        notes_extra[18] = str(e)

    # Apply cross-check findings to outcomes.
    # Cross-checks are advisory: they append CONCERN to notes without demoting PASS to FAIL.
    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="05c_doctor",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
