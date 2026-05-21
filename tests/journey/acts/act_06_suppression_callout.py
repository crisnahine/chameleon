"""Act 6: Suppression + callout-detector (Phases 19, 20, 23)."""
from __future__ import annotations

import re
from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Test suppression mechanisms and the callout-detector in working/ts_basic.
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

PHASE 20 - callout-detector (7 frustration patterns):
  emit checkpoint started phase 20
  Send 7 user prompts (one per turn) that each contain a distinct frustration marker.
  For each, note whether the UserPromptSubmit hook suggested a chameleon command
  (the callout-detector should fire, embedding a /chameleon-disable or /chameleon-pause-15m
  or /chameleon-teach hint in the additionalContext).

  Prompt 1: "ugh stop doing this"
  Prompt 2: "I hate this constant injection"
  Prompt 3: "damn it why does chameleon keep doing this"
  Prompt 4: "this isn't right, please stop"
  Prompt 5: "don't do that again"
  Prompt 6: "chameleon is so slow"
  Prompt 7: "stop injecting all this context"

  After each prompt, check whether your context included a chameleon callout hint.
  Report: how many of the 7 prompts triggered a callout hint.
  emit checkpoint completed phase 20

PHASE 23 - HMAC tampering + disable_session security (already partially covered in Phase 19):
  emit checkpoint started phase 23
  Summarize what was verified in Phase 19 STEP 2 and STEP 3:
    - forged HMAC marker was rejected (downgrade defense confirmed)
    - force= flag behavior confirmed
    - unknown session refusal confirmed
    - trust gate on untrusted fixture confirmed
  Run chameleon-mcp::disable_session one more time with force=True on the ts_basic path
  to confirm the force= path still works after the precedence cycle.
  emit checkpoint completed phase 23

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
        ctx.current_checkpoint_file, expected_phases=[19, 20, 23]
    )

    notes_extra: dict[int, str] = {}

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
    except Exception as exc:
        notes_extra[19] = f"error scanning for session_disabled markers: {exc}"

    # Phase 20: count distinct frustration patterns matched via UserPromptSubmit
    # additionalContext. Each should contain a /chameleon-disable or similar hint.
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        # Look for chameleon callout hints in the transcript (injected via additionalContext).
        callout_patterns = [
            r"/chameleon-disable",
            r"/chameleon-pause",
            r"/chameleon-teach",
            r"callout",
            r"chameleon-disable",
            r"chameleon-pause",
        ]
        found_any_callout = any(
            re.search(p, transcript_text, re.IGNORECASE)
            for p in callout_patterns
        )
        if not found_any_callout:
            notes_extra[20] = (
                "no callout-detector hints (/chameleon-disable / /chameleon-pause / "
                "/chameleon-teach) found in transcript; frustration prompts may not "
                "have triggered UserPromptSubmit injection"
            )
    except Exception as exc:
        notes_extra[20] = f"error scanning transcript for callout hints: {exc}"

    # Phase 23: forged-HMAC defense - verify no forged marker is present
    # (the prompt instructs Claude to plant and then verify rejection; the runner
    # confirms the marker is gone or that suppression did not fire during the
    # forged-marker edit, which would be visible as an advisory in the transcript).
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        # After planting a forged marker, the advisory should have fired (suppression NOT
        # honored). Look for advisory tokens near the forged-marker edit in the transcript.
        # This is a heuristic: if "forged" and "advisory" appear in the same region,
        # the defense was exercised. Absence of this is acceptable since checkpoint
        # status is the primary signal.
        if "forged" not in transcript_text.lower():
            notes_extra[23] = (
                "transcript does not mention forged marker test; "
                "HMAC tampering defense may not have been exercised"
            )
    except Exception as exc:
        notes_extra[23] = f"error scanning transcript for forged marker evidence: {exc}"

    # Apply cross-check findings to outcomes
    for phase, extra in notes_extra.items():
        if phase in outcomes and outcomes[phase].status == "PASS":
            outcomes[phase].status = "FAIL"
            outcomes[phase].notes = (outcomes[phase].notes + "; " + extra).strip("; ")

    return ActResult(
        act_id="06_suppression_callout",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
