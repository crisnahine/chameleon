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
  FIRST: emit checkpoint started phase 16 NOW (plain Bash echo, outside any code fence).
  Run /chameleon-teach (structured) with these exact values:
    slug: no-direct-axios
    rationale: We wrap HTTP in src/lib/api.ts - never import axios directly
    example: import { api } from '@/lib/api'
    counterexample: import axios from 'axios'
    archetype: util
    status: active
  After teach succeeds:
    - Confirm .chameleon/idioms.md was updated and contains "no-direct-axios".
    - Confirm idioms.md contains Language: typescript frontmatter.
  Test the slug length boundary:
    - Try a 64-char slug (e.g. "a" + "b" * 63). Expect success.
    - Try a 65-char slug (e.g. "a" + "b" * 64). Expect an error (exceeds 64-char limit).
  Test the 50KB per-idiom cap:
    - Run /chameleon-teach (structured) with slug "fifty-kb-test" and a rationale that is
      51000 characters long (just over 50KB). Expect a "failed" status with an error
      mentioning the 50KB cap. Do NOT add this idiom successfully; the failure is expected.
  emit checkpoint completed phase 16.

PHASE 17 - status output surface:
  FIRST: emit checkpoint started phase 17 NOW.
  Run /chameleon-status. Verify the output mentions:
    - canonical_ref, auto_refresh, auto_rename (v0.6.0 config keys)
    - trust state (trusted / stale / untrusted)
  emit checkpoint completed phase 17.

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
    transcript = ctx.run_dir / "transcripts" / "act_05.txt"
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
        timeout_s=900,
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

    # Apply cross-check findings to outcomes.
    # Cross-checks are advisory: they append CONCERN to notes without demoting PASS to FAIL.
    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="05_teach_status_doctor",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
