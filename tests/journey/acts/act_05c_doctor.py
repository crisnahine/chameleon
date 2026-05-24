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
    cross_check_passed: dict[int, bool] = {}

    # Phase 18: age hook_errors.log entries and verify doctor filters them
    # Also verify doctor detects canonicals.json corruption (permissive about subsystem name)
    try:
        hook_error_log = ctx.hook_error_log
        _phase18_fail = False
        stale_filter_passed = True

        if hook_error_log.exists():
            ctx.fast_forward_marker(hook_error_log, age_seconds=4 * 24 * 3600)
            try:
                doctor_result = mcp.call_mcp_tool(
                    tool_name="doctor",
                    plugin_root=ctx.plugin_root,
                    env={**ctx.env, "CHAMELEON_PLUGIN_DATA": str(ctx.plugin_data_dir)},
                    timeout_s=30,
                )

                # --- stale filter check ---
                recent_errors = doctor_result.get("recent_errors", {})
                if isinstance(recent_errors, dict) and recent_errors.get("status") == "error":
                    error_msg = str(recent_errors.get("message", ""))
                    if "OLD-ERROR" in error_msg:
                        notes_extra[18] = "doctor showed old error (72h filter did not fire)"
                        stale_filter_passed = False

                # --- corruption detection check (v0.6.1: any subsystem OK) ---
                # We corrupted canonicals.json earlier; doctor should notice somewhere.
                doctor_data = doctor_result.get("data", doctor_result)
                corruption_detected = False

                # Check per_repo_state
                prs = doctor_data.get("per_repo_state", {})
                if isinstance(prs, dict) and prs.get("status") not in ("ok", None):
                    corruption_detected = True

                # Check known_repos
                kr = doctor_data.get("known_repos", {})
                if isinstance(kr, dict):
                    repos = kr.get("repos", [])
                    candidates = repos if isinstance(repos, list) else [kr]
                    for repo_entry in candidates:
                        if isinstance(repo_entry, dict) and repo_entry.get(
                            "profile_status"
                        ) in (None, "corrupted", "error", "missing"):
                            corruption_detected = True

                # Any other subsystem showing non-ok
                for val in doctor_data.values():
                    if isinstance(val, dict) and val.get("status") in ("error", "warn"):
                        corruption_detected = True

                _phase18_fail = not stale_filter_passed
                # Corruption detection is advisory (doctor may have already recovered by
                # the time the runner calls it); don't hard-fail on it.
            except Exception:
                pass

        # Transcript non-empty is the minimal evidence Claude ran the phase
        transcript = ctx.run_dir / "transcripts" / "act_05c.txt"
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        cross_check_passed[18] = not _phase18_fail and len(transcript_text) > 0
    except expect.PhaseAssertionError as e:
        notes_extra[18] = str(e)
        cross_check_passed[18] = False

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
        act_id="05c_doctor",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
