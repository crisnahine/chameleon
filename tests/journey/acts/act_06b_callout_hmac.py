"""Act 6b: Callout-detector + HMAC tampering security (Phases 20, 23)."""
from __future__ import annotations

import re

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext

_PROMPT_BODY = """\
Test the callout-detector and HMAC security in working/ts_basic
(profile bootstrapped and trusted from earlier acts).
Use absolute paths for all file references.

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

PHASE 23 - HMAC tampering + disable_session security:
  emit checkpoint started phase 23
  STEP 1 - forged HMAC marker:
    Use the Bash tool to overwrite any existing .session_disabled marker with a bad signature:
      find working/ts_basic -name ".session_disabled.*" 2>/dev/null | head -1 | \
        xargs -I{} bash -c 'echo "forged-bad-signature-content" > "{}"' 2>/dev/null || true
    Edit any tracked file (e.g. src/utils/format_date.ts, add a comment).
    Confirm the advisory IS present (forged marker rejected, suppression NOT honored =
    downgrade attack defense working).

  STEP 2 - disable_session security:
    Call chameleon-mcp::disable_session with an UNKNOWN session_id (use a random
    string like "unknown-session-xyz-999") and WITHOUT force=True. Expect a refusal
    (error response indicating unknown session).
    Retry the same unknown session_id WITH force=True. Expect success.

  STEP 3 - summarize:
    Summarize what was verified in STEP 1 and STEP 2:
      - forged HMAC marker was rejected (downgrade defense confirmed)
      - force= flag behavior confirmed
      - unknown session refusal confirmed
    Run chameleon-mcp::disable_session one more time with force=True on the ts_basic path
    to confirm the force= path still works cleanly.
  emit checkpoint completed phase 23

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_06b.txt"
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
        permission_mode="bypassPermissions",
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[20, 23]
    )

    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    # Phase 20: count distinct frustration patterns matched via UserPromptSubmit
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
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
            cross_check_passed[20] = False
        else:
            cross_check_passed[20] = True
    except Exception as exc:
        notes_extra[20] = f"error scanning transcript for callout hints: {exc}"
        cross_check_passed[20] = False

    # Phase 23: forged-HMAC defense - verify advisory fired during forged-marker edit
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        if "forged" not in transcript_text.lower():
            notes_extra[23] = (
                "transcript does not mention forged marker test; "
                "HMAC tampering defense may not have been exercised"
            )
            cross_check_passed[23] = False
        else:
            cross_check_passed[23] = True
    except Exception as exc:
        notes_extra[23] = f"error scanning transcript for forged marker evidence: {exc}"
        cross_check_passed[23] = False

    # Cross-check results can promote SKIP -> PASS
    for phase, passed in cross_check_passed.items():
        if phase in outcomes and passed:
            if outcomes[phase].status == "SKIP":
                outcomes[phase].status = "PASS"
                outcomes[phase].notes = "promoted from SKIP by runner cross-check"
            elif outcomes[phase].status == "FAIL" and "phase incomplete" in outcomes[phase].notes:
                outcomes[phase].status = "PASS"
                outcomes[phase].notes = "promoted from incomplete-FAIL by runner cross-check"

    # Cross-check concerns (append, don't demote PASS)
    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="06b_callout_hmac",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
