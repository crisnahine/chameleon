"""Act 4: v0.6.0 UX bundle - auto_refresh subprocess discipline (Phase 12)."""

from __future__ import annotations

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
  Auto-refresh is a SessionStart feature (the per-edit PreToolUse hook never
  triggers it - the hot path stays refresh-free by design). Trigger it the way
  a new session would: invoke the session-start hook directly via Bash:
    echo '{"session_id":"act04-ar"}' | CLAUDE_PLUGIN_ROOT=<plugin root> <plugin root>/hooks/session-start
  (find <plugin root> from $CLAUDE_PLUGIN_ROOT, or the chameleon repo's plugin/ dir).
  The auto_refresh mechanism should spawn a detached background subprocess
  (Popen with start_new_session=True).
  After that invocation, use Bash to verify:
    - An auto_refresh.log file was written under the chameleon plugin data directory.
      The path is: $CHAMELEON_PLUGIN_DATA/<repo_id>/auto_refresh.log
      where <repo_id> is the repo identifier. Check by listing $CHAMELEON_PLUGIN_DATA.
    - A .auto_refresh_cooldown file exists in the same repo_id directory.
  Then invoke the session-start hook again immediately. Verify the cooldown blocks
  re-triggering (auto_refresh.log and the cooldown file both unchanged - compare
  mtimes or contents before/after).
  Also confirm the negative: a PreToolUse edit (edit src/utils/format_date.ts via
  the Edit tool) does NOT touch auto_refresh.log - per-edit hooks never spawn a
  refresh.
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
            "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            # get_drift_status routes via the telemetry dispatcher;
            # list_profiles / refresh_repo / trust_profile via lifecycle.
            "mcp__plugin_chameleon_chameleon-mcp__chameleon_lifecycle",
            "mcp__plugin_chameleon_chameleon-mcp__chameleon_telemetry",
        ],
        plugin_root=ctx.plugin_root,
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[12]
    )

    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    try:
        auto_refresh_logs = list(ctx.plugin_data_dir.rglob("auto_refresh.log"))
        if not auto_refresh_logs:
            notes_extra[12] = "auto_refresh.log not found under plugin_data_dir"
            cross_check_passed[12] = False
        else:
            log_path = auto_refresh_logs[0]
            _phase12_fail = False
            try:
                expect.file_mode(12, log_path, 0o600)
            except expect.PhaseAssertionError as e:
                notes_extra[12] = str(e)
                _phase12_fail = True
            try:
                expect.file_size_between(12, log_path, 0, 64 * 1024)
            except expect.PhaseAssertionError as e:
                notes_extra[12] = (notes_extra.get(12, "") + "; " + str(e)).strip("; ")
                _phase12_fail = True
            cross_check_passed[12] = not _phase12_fail
    except expect.PhaseAssertionError as e:
        notes_extra[12] = str(e)
        cross_check_passed[12] = False

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
        act_id="04_v060_ux_bundle",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
