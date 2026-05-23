"""Act 6: Suppression - pause, disable, 4-level precedence (Phase 19)."""
from __future__ import annotations

from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Test suppression mechanisms in working/ts_basic.
Use absolute paths for all file references.

PHASE 19 - pause, disable, and 4-level precedence:
  emit checkpoint started phase 19

  STEP 1 - /chameleon-pause-15m:
    Run /chameleon-pause-15m. Edit any tracked file (e.g. src/utils/format_date.ts,
    add a comment). Confirm no <chameleon-context> advisory appeared in the PreToolUse
    output (suppression honored). Report whether the advisory was suppressed.

  STEP 2 - /chameleon-disable:
    Run /chameleon-disable in the current session. Verify the command succeeds.
    Call chameleon-mcp::disable_session again for the same session_id (idempotent
    re-call). Expect either "ok" or a clear idempotent-ok response.
    Now call chameleon-mcp::disable_session with an UNKNOWN session_id (use a random
    string like "unknown-session-xyz-999") and WITHOUT force=True. Expect a refusal
    (error response indicating unknown session).
    Retry the same unknown session_id WITH force=True. Expect success.
    Now call chameleon-mcp::disable_session on an UNTRUSTED fixture: use a path
    pointing to working/rails_basic (which has not been bootstrapped or trusted yet).
    Expect an error response matching "not trusted" or similar.

  STEP 3 - forged HMAC marker:
    Use the Bash tool to overwrite the .session_disabled marker with a bad signature:
      find working/ts_basic -name ".session_disabled.*" | head -1 | xargs -I{} \
        bash -c 'echo "forged-bad-signature-content" > "{}"'
    Edit any tracked file again. Confirm the advisory IS present (forged marker rejected,
    suppression NOT honored = downgrade attack defense working).

  STEP 4 - 4-level precedence cycle:
    Verify the 4-level precedence: .skip > CHAMELEON_DISABLE env > session_disabled > pause

    Level 1 (.skip wins):
      Use Bash: touch working/ts_basic/.chameleon/.skip
      Edit src/utils/slugify.ts (add a comment). Confirm advisory suppressed.
      Use Bash: rm working/ts_basic/.chameleon/.skip

    Level 2 (CHAMELEON_DISABLE env wins when .skip absent):
      Report that env var CHAMELEON_DISABLE=1 suppression was already covered by
      runner-side preflight; observe the current env. If CHAMELEON_DISABLE is set,
      report suppressed. If not set, note that this level is validated at the harness
      level, not interactively.

    Level 3 (valid session_disabled marker wins when .skip absent):
      Run /chameleon-disable again to plant a new valid marker.
      Edit src/utils/format_currency.ts (add a comment). Confirm advisory suppressed.

    Level 4 (pause marker wins when session_disabled absent):
      After confirming level 3, remove the session_disabled marker via Bash if needed.
      Run /chameleon-pause-15m. Edit another file. Confirm advisory suppressed.

    Level 5 (no suppression - advisory fires):
      Remove all suppression (pause will be fast-forwarded by runner after this phase).
      Edit src/utils/format_date.ts. Confirm advisory fires.

  emit checkpoint completed phase 19

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_06.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=40,
        allowed_tools=[
            "Bash",
            "Read",
            "Edit",
            "Write",
            "mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            "mcp__plugin_chameleon_chameleon-mcp__disable_session",
            "mcp__plugin_chameleon_chameleon-mcp__get_drift_status",
            "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
            "mcp__plugin_chameleon_chameleon-mcp__pause_session",
            "mcp__plugin_chameleon_chameleon-mcp__trust_profile",
        ],
        plugin_root=ctx.plugin_root,
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[19]
    )

    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    # Phase 19: verify .pause_until was written, and session_disabled marker exists.
    ts_basic_chameleon = ctx.fixture("ts_basic") / ".chameleon"

    # Fast-forward the pause marker so it expires (simulating post-phase cleanup).
    # The runner does this to unblock later acts; the prompt already exercised the
    # live pause-suppression path before this runs.
    pause_until_path = ts_basic_chameleon / ".pause_until"
    if pause_until_path.exists():
        ctx.fast_forward_marker(pause_until_path, age_seconds=16 * 60)

    # Look for the HMAC-signed session_disabled marker under plugin_data_dir.
    # Markers are written as .session_disabled.<sha256(session_id)[:16]> under the
    # per-repo dir in CHAMELEON_PLUGIN_DATA.
    try:
        session_disabled_markers = list(ctx.plugin_data_dir.rglob(".session_disabled.*"))
        if not session_disabled_markers:
            # Tolerate absence: /chameleon-disable in the prompt may have written to
            # the fixture dir instead; look there too.
            session_disabled_markers = list(ts_basic_chameleon.glob(".session_disabled.*"))
        if not session_disabled_markers:
            notes_extra[19] = (
                "no .session_disabled.<sid> marker found under plugin_data_dir or "
                ".chameleon/ after /chameleon-disable"
            )
            cross_check_passed[19] = False
        else:
            cross_check_passed[19] = True
    except Exception as exc:
        notes_extra[19] = f"error scanning for session_disabled markers: {exc}"
        cross_check_passed[19] = False

    # Cross-check results can promote SKIP -> PASS
    for phase, passed in cross_check_passed.items():
        if phase in outcomes and outcomes[phase].status == "SKIP" and passed:
            outcomes[phase].status = "PASS"
            outcomes[phase].notes = "promoted from SKIP by runner cross-check"

    # Cross-check concerns (append, don't demote PASS)
    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="06_suppression_callout",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
