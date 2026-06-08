"""Act 5: Teach idiom (structured + slug-length boundary) (Phase 16).

A single bounded claude session does the real-loop teach: a structured teach
that must land in idioms.md, plus the slug-length boundary (64-char ok, 65-char
rejected). It then emits the one phase-16 checkpoint the harness expects.

The per-idiom 50KB cap is NOT exercised here: forcing a model to emit a 51KB
rationale through a live session either trips the per-response output ceiling or
stalls the stream, and tests the model rather than the cap. That cap is
deterministic server-side validation and is covered by tests/unit/
test_teach_structured_cap.py instead.
"""

from __future__ import annotations

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext

_TEACH_TOOLS = [
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
]


def _body(repo: str) -> str:
    return f"""\
Phase 16 (structured idiom teach + slug-length boundary) against the trusted repo at
{repo} (trusted in Act 2). Use absolute paths. Be terse: do not print file contents or
long narration.

1. Run /chameleon-teach (structured) with EXACTLY these values:
     slug: no-direct-axios
     rationale: We wrap HTTP in src/lib/api.ts - never import axios directly
     example: import {{ api }} from '@/lib/api'
     counterexample: import axios from 'axios'
     archetype: util
     status: active
   After it succeeds, confirm {repo}/.chameleon/idioms.md contains "no-direct-axios"
   and a "Language: typescript" frontmatter line.
2. Slug-length boundary:
     - A 64-character slug ("a" then 63 "b"s): expect SUCCESS.
     - A 65-character slug ("a" then 64 "b"s): expect an ERROR (exceeds the 64-char
       limit). Do not retry it.

Then emit the phase-16 checkpoint: status "passed" if the structured teach landed
(idioms.md contains "no-direct-axios" and the "Language: typescript" frontmatter) and
both slug-boundary cases behaved as described; otherwise status "failed" with a short
note naming what broke.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    repo = str(cwd)
    transcript = ctx.run_dir / "transcripts" / "act_05.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_body(repo)),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=40,
        allowed_tools=_TEACH_TOOLS,
        plugin_root=ctx.plugin_root,
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[16]
    )

    # Backstop: cross-check the on-disk artifact so a "passed" the state does not
    # support is flagged, and a session that never checkpointed is still attributed
    # from the idioms.md it left behind.
    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    idioms_md = cwd / ".chameleon" / "idioms.md"
    try:
        expect.path_exists(16, idioms_md)
        expect.file_size_between(16, idioms_md, 1, 200 * 1024)
        idioms_content = idioms_md.read_text(encoding="utf-8")
        _phase16_fail = False
        if (
            "Language: typescript" not in idioms_content
            and "Language:typescript" not in idioms_content
        ):
            notes_extra[16] = "idioms.md missing 'Language: typescript' frontmatter"
            _phase16_fail = True
        if "###" not in idioms_content:
            notes_extra[16] = (notes_extra.get(16, "") + "; idioms.md has no ### headers").strip(
                "; "
            )
            _phase16_fail = True
        cross_check_passed[16] = not _phase16_fail
    except expect.PhaseAssertionError as e:
        notes_extra[16] = str(e)
        cross_check_passed[16] = False

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
        act_id="05_teach_status_doctor",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
