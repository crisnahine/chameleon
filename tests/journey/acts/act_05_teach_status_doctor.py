"""Act 5: Teach idiom (structured + cap tests) (Phase 16)."""
from __future__ import annotations

from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Teach idioms against working/ts_basic (trusted from Act 2).
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
    - Use Bash to generate a 51000-character string and save it to a temp file:
        python3 -c "print('x' * 51000)" > /tmp/big_rationale.txt
    - Read the file contents and pass them as the rationale to /chameleon-teach (structured)
      with slug "fifty-kb-test". Expect a "failed" status with an error mentioning the 50KB cap.
      Do NOT add this idiom successfully; the failure is expected.
  emit checkpoint completed phase 16.

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
        timeout_s=1200,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[16]
    )

    # Runner-side cross-checks (defense in depth)
    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    # Phase 16: read idioms.md, verify it exists and has at least one ### header
    ts_basic_chameleon = ctx.fixture("ts_basic") / ".chameleon"
    idioms_md = ts_basic_chameleon / "idioms.md"
    try:
        expect.path_exists(16, idioms_md)
        expect.file_size_between(16, idioms_md, 1, 200 * 1024)
        idioms_content = idioms_md.read_text(encoding="utf-8")
        _phase16_fail = False
        if "Language: typescript" not in idioms_content and "Language:typescript" not in idioms_content:
            notes_extra[16] = "idioms.md missing 'Language: typescript' frontmatter"
            _phase16_fail = True
        if "###" not in idioms_content:
            notes_extra[16] = (notes_extra.get(16, "") + "; idioms.md has no ### headers").strip("; ")
            _phase16_fail = True
        cross_check_passed[16] = not _phase16_fail
    except expect.PhaseAssertionError as e:
        notes_extra[16] = str(e)
        cross_check_passed[16] = False

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
        act_id="05_teach_status_doctor",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
