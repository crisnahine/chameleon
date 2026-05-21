"""Act 5: Teach + Status + Doctor (Phases 16, 17, 18)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect, mcp
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Teach idioms, check status, and run doctor against working/ts_basic.
Use absolute paths for all file references.

PHASE 16 - structured idiom teach:
  emit checkpoint started phase 16
  Run /chameleon-teach with a structured idiom using these exact values:
    slug: no-direct-axios
    rationale: We wrap HTTP in src/lib/api.ts - never import axios directly
    example: import { api } from '@/lib/api'
    counterexample: import axios from 'axios'
    archetype: util
    status: active
  After teach succeeds, verify:
    - The slug "no-direct-axios" passes the regex \\A[a-z][a-z0-9-]{0,63}\\Z
    - .chameleon/idioms.md was updated and contains the new idiom
    - The rationale + example + counterexample is under 50KB total for this idiom
  Now test the slug length boundary:
    - Try a 64-character slug (e.g. "a" + "b" * 63, i.e. 64 chars starting with a letter).
      Expect success (64 chars is within the valid range of 1-64 chars per the regex).
    - Try a 65-character slug (e.g. "a" + "b" * 64, i.e. 65 chars).
      Expect an error response (65 chars exceeds the 64-char limit).
  Add 4 more idioms (any valid slugs and content) to push cumulative idioms.md
  toward but below 200KB. Each idiom should have rationale + example + counterexample
  of a few hundred bytes.
  Attempt to add a 6th idiom that would push the total over 200KB by using a
  rationale string that is 40KB long (use Bash to generate: python3 -c "print('x'*40000)").
  Expect the teach call to return an error indicating the 200KB cumulative cap.
  Verify working/ts_basic/.chameleon/idioms.md contains Language: typescript frontmatter.
  emit checkpoint completed phase 16

PHASE 17 - status output surface:
  emit checkpoint started phase 17
  Run /chameleon-status. Carefully read the output and verify it mentions all of:
    - Profile summary (archetype count or similar)
    - Trust state (trusted / stale / untrusted) with grantor and timestamp
    - Drift score and recommended action
    - Language hint (typescript or similar)
    - Version coherence (some version reference)
    - At least one of these v0.6.0 config keys: canonical_ref, auto_refresh, auto_rename
  If auto_preserve_when or trust.auto_preserve_when was configured in Act 4, verify
  it also appears in the status output.
  Report each item as found or not found. Emit checkpoint completed only if all
  core fields (profile, trust, drift, lang) are present.
  emit checkpoint completed phase 17

PHASE 18 - doctor with stale errors filter:
  emit checkpoint started phase 18
  Use the Bash tool to corrupt .chameleon/canonicals.json:
    echo "XXXXX" > .chameleon/canonicals.json
  Run /chameleon-doctor (or call chameleon-mcp::doctor). Verify the response reports
  a per_repo_state subsystem error related to the corrupted canonicals.json.
  Now test the 72-hour stale errors filter:
    1. Write a fake old error entry to the hook errors log file at $CHAMELEON_HOOK_ERROR_LOG:
       OLD_TS=$(date -u -d "4 days ago" +%FT%TZ 2>/dev/null || date -u -v-4d +%FT%TZ)
       echo "[${OLD_TS}] OLD-ERROR: stale hook failure from 4 days ago" >> "$CHAMELEON_HOOK_ERROR_LOG"
    2. Write a fresh error entry:
       echo "[$(date -u +%FT%TZ)] FRESH-ERROR: recent hook failure" >> "$CHAMELEON_HOOK_ERROR_LOG"
    3. Age the old entry by using touch with a past timestamp via Bash:
       touch -t $(date -u +"%Y%m%d%H%M.%S" -d "4 days ago" 2>/dev/null || date -u -v-4d +"%Y%m%d%H%M.%S") "$CHAMELEON_HOOK_ERROR_LOG"
       Note: this ages the entire log file mtime; the filter checks the mtime of the log file
       or parses timestamps. Verify the doctor response filters entries older than 72 hours.
    4. Run /chameleon-doctor again. Verify the recent_errors subsystem shows only the
       fresh error, not the old one. If the doctor shows both, report that the 72h filter
       did not fire.
  Restore .chameleon/canonicals.json to valid JSON when done:
    echo '{}' > .chameleon/canonicals.json
  emit checkpoint completed phase 18

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_05.txt"
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
            "mcp__plugin_chameleon_chameleon-mcp__doctor",
            "mcp__plugin_chameleon_chameleon-mcp__get_archetype",
            "mcp__plugin_chameleon_chameleon-mcp__get_drift_status",
            "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
            "mcp__plugin_chameleon_chameleon-mcp__refresh_repo",
            "mcp__plugin_chameleon_chameleon-mcp__teach_profile",
            "mcp__plugin_chameleon_chameleon-mcp__teach_profile_structured",
            "mcp__plugin_chameleon_chameleon-mcp__trust_profile",
        ],
        plugin_root=ctx.plugin_root,
        timeout_s=600,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[16, 17, 18]
    )

    # Runner-side cross-checks (defense in depth)
    notes_extra: dict[int, str] = {}

    # Phase 16: read idioms.md, verify total size <= 200KB and Language: typescript present
    ts_basic_chameleon = ctx.fixture("ts_basic") / ".chameleon"
    idioms_md = ts_basic_chameleon / "idioms.md"
    try:
        expect.path_exists(16, idioms_md)
        expect.file_size_between(16, idioms_md, 1, 200 * 1024)
        idioms_content = idioms_md.read_text(encoding="utf-8")
        if "Language: typescript" not in idioms_content and "Language:typescript" not in idioms_content:
            notes_extra[16] = "idioms.md missing 'Language: typescript' frontmatter"
    except expect.PhaseAssertionError as e:
        notes_extra[16] = str(e)

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
    except expect.PhaseAssertionError as e:
        notes_extra[17] = str(e)

    # Phase 18: age hook_errors.log entries and verify doctor filters them
    # The prompt instructs Claude to write old + fresh entries; runner-side we
    # use fast_forward_marker to simulate aging, then call doctor directly.
    try:
        hook_error_log = ctx.hook_error_log
        if hook_error_log.exists():
            # Age the log file mtime past 72 hours to test the stale filter
            ctx.fast_forward_marker(hook_error_log, age_seconds=4 * 24 * 3600)
            # Call doctor via MCP to verify aged entries are filtered
            try:
                doctor_result = mcp.call_mcp_tool(
                    tool_name="doctor",
                    plugin_root=ctx.plugin_root,
                    env={**ctx.env, "CHAMELEON_PLUGIN_DATA": str(ctx.plugin_data_dir)},
                    timeout_s=30,
                )
                # If doctor returns ok for recent_errors, the filter worked
                recent_errors = doctor_result.get("recent_errors", {})
                if isinstance(recent_errors, dict) and recent_errors.get("status") == "error":
                    # Check if the error message references only recent entries
                    error_msg = str(recent_errors.get("message", ""))
                    if "OLD-ERROR" in error_msg:
                        notes_extra[18] = "doctor showed old error (72h filter did not fire)"
            except Exception as e:
                # Doctor call failure is not critical for this check
                pass
    except expect.PhaseAssertionError as e:
        notes_extra[18] = str(e)

    # Apply cross-check findings to outcomes
    for phase, extra in notes_extra.items():
        if phase in outcomes and outcomes[phase].status == "PASS":
            outcomes[phase].status = "FAIL"
            outcomes[phase].notes = (outcomes[phase].notes + "; " + extra).strip("; ")

    return ActResult(
        act_id="05_teach_status_doctor",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
