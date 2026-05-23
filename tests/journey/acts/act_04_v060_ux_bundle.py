"""Act 4: v0.6.0 UX bundle - auto_refresh subprocess discipline (Phase 12)."""
from __future__ import annotations

from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Test auto_refresh subprocess discipline against working/ts_basic (trusted from Act 2).
Use absolute paths for all file references.

PHASE 12 - auto_refresh subprocess discipline:
  emit checkpoint started phase 12
  Use the Bash tool to write .chameleon/config.json with this content:
    {"auto_refresh": {"enabled": true, "drift_threshold": 0.2, "max_age_hours": 168}}
  Trigger drift past the threshold: copy 30 unconventional files into src/services/:
    mkdir -p src/services
    for i in $(seq 1 30); do cp src/utils/format_date.ts "src/services/DRIFT-FILE-${i}.ts"; done
  Now fire a PreToolUse event by editing any tracked file (e.g. src/utils/format_date.ts,
  add a comment). The auto_refresh mechanism should spawn a detached background subprocess
  (Popen with start_new_session=True).
  After the edit, use Bash to verify:
    - An auto_refresh.log file was written under the chameleon plugin data directory.
      The path is: $CHAMELEON_PLUGIN_DATA/<repo_id>/auto_refresh.log
      where <repo_id> is the repo identifier. Check by listing $CHAMELEON_PLUGIN_DATA.
    - A .auto_refresh_cooldown file exists in the same repo_id directory.
  Then fire another edit immediately on a second file. Verify the cooldown blocks
  re-triggering (no duplicate auto_refresh subprocess).
  emit checkpoint completed phase 12

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_04.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=50,
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
        ctx.current_checkpoint_file, expected_phases=[12]
    )

    # Runner-side cross-checks (defense in depth)
    notes_extra: dict[int, str] = {}

    # Phase 12: assert auto_refresh.log exists under plugin_data_dir/<repo_id>/
    # and check file mode 0o600 and size <= 64KB
    try:
        # Find auto_refresh.log files anywhere under plugin_data_dir
        auto_refresh_logs = list(ctx.plugin_data_dir.rglob("auto_refresh.log"))
        if not auto_refresh_logs:
            notes_extra[12] = "auto_refresh.log not found under plugin_data_dir"
        else:
            log_path = auto_refresh_logs[0]
            try:
                expect.file_mode(12, log_path, 0o600)
            except expect.PhaseAssertionError as e:
                notes_extra[12] = str(e)
            try:
                expect.file_size_between(12, log_path, 0, 64 * 1024)
            except expect.PhaseAssertionError as e:
                notes_extra[12] = (notes_extra.get(12, "") + "; " + str(e)).strip("; ")
    except expect.PhaseAssertionError as e:
        notes_extra[12] = str(e)

    # Apply cross-check findings to outcomes.
    # Cross-checks are advisory: they append CONCERN to notes without demoting PASS to FAIL.
    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="04_v060_ux_bundle",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
