"""Act 5b: Status v0.6.0 config surface (Phase 17)."""
from __future__ import annotations

from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Check status output in working/ts_basic (profile bootstrapped and trusted from earlier acts).
Use absolute paths for all file references.

PHASE 17 - status output surface:
  FIRST: emit checkpoint started phase 17 NOW.
  Run /chameleon-status. Verify the output mentions:
    - canonical_ref, auto_refresh, auto_rename (v0.6.0 config keys)
    - trust state (trusted / stale / untrusted)
  emit checkpoint completed phase 17.

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_05b.txt"
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
            "mcp__plugin_chameleon_chameleon-mcp__get_drift_status",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
        ],
        plugin_root=ctx.plugin_root,
        permission_mode="bypassPermissions",
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[17]
    )

    notes_extra: dict[int, str] = {}

    # Phase 17: parse transcript for /chameleon-status output, look for key fields
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        required_status_keys = ["canonical_ref", "auto_refresh", "auto_rename", "trust"]
        found_keys = [k for k in required_status_keys if k in transcript_text]
        if len(found_keys) < 2:
            notes_extra[17] = (
                f"status output missing expected v0.6.0 config keys; "
                f"found {found_keys!r} out of {required_status_keys!r}"
            )
    except Exception as e:
        notes_extra[17] = str(e)

    # Apply cross-check findings to outcomes.
    # Cross-checks are advisory: they append CONCERN to notes without demoting PASS to FAIL.
    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="05b_status",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
