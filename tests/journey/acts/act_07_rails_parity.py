"""Act 7: Rails parity (Phase 21)."""

from __future__ import annotations

import json
import re

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext

_PROMPT_BODY = """\
Test Rails parity and hybrid language detection. Use absolute paths everywhere.

PHASE 21 - Rails parity (Prism extractor, rubocop rules, Rails priors, hybrid):
  emit checkpoint started phase 21

  STEP 1 - init Rails fixture:
    Switch to working/rails_basic (Gemfile, app/controllers, app/models,
    app/services, spec/). Run /chameleon-init.
    After bootstrap, verify:
      - working/rails_basic/.chameleon/COMMITTED exists
      - working/rails_basic/.chameleon/profile.json exists
      - profile.json has language "ruby" (read and confirm)
      - working/rails_basic/.chameleon/rules.json exists and has a "rubocop" key
        (read and confirm; the rubocop.yml rules should have been extracted)
      - working/rails_basic/.chameleon/archetypes.json exists and contains at least
        3 Rails-shaped archetype names (look for "controller", "model", or "service"
        in the archetype names)
    Report whether Prism extractor appears to have run: look for any mention of
    prism_dump.rb in your context or in the bootstrap output. Note that Prism is the
    Ruby AST extractor; evidence of its invocation confirms the Ruby path was taken
    (not the TS path).

  STEP 2 - trust and edit across 3 Rails archetypes:
    Run /chameleon-trust on the rails_basic fixture (type the repo name when prompted).
    Verify trust is granted.
    Make 3 edits across 3 different Rails archetypes:
      1. Controller edit: open or create app/controllers/home_controller.rb and add
         a comment or method stub. Verify PreToolUse advisory fires and references
         the controller archetype.
      2. Service edit: open or create app/services/user_service.rb and add a comment.
         Verify advisory fires and references the service archetype.
      3. Spec edit: open or create spec/models/user_spec.rb and add a comment.
         Verify advisory fires and references the spec archetype (or a test archetype).
    Note: the advisory should appear in your context before each edit lands.

  STEP 3 - refresh:
    Run /chameleon-refresh on the rails_basic fixture.
    Verify working/rails_basic/.chameleon/profile.json and COMMITTED still exist.

  STEP 4 - teach with Ruby idiom:
    Run /chameleon-teach with a Rails idiom:
      slug: no-direct-active-record-in-controllers
      rationale: Move AR queries to service objects or scopes
      example: UserService.find_active_users
      counterexample: User.where(active: true) inside a controller action
      archetype: controller
      status: active
    Verify the idiom was added to working/rails_basic/.chameleon/idioms.md.
    Verify the idioms.md file contains "Language: ruby" frontmatter.

  STEP 5 - hybrid language detection:
    Switch to working/ts_with_rails_sidecar (this fixture has both Gemfile and
    package.json). Run /chameleon-init.
    After bootstrap, run chameleon-mcp::get_pattern_context on any file in the
    fixture to verify chameleon initialized successfully.
    Report the language detected in profile.json (should match the dominant tree).
    Verify that a language_hint appeared in the SessionStart primer context during
    this init (it should note whether this is a TS or Rails primary repo with a
    sidecar of the other language).

  emit checkpoint completed phase 21

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("rails_basic")
    transcript = ctx.run_dir / "transcripts" / "act_07.txt"
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
            "mcp__plugin_chameleon_chameleon-mcp__bootstrap_repo",
            "mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            "mcp__plugin_chameleon_chameleon-mcp__get_archetype",
            "mcp__plugin_chameleon_chameleon-mcp__get_canonical_excerpt",
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
        ctx.current_checkpoint_file, expected_phases=[21]
    )

    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    rails_chameleon = ctx.fixture("rails_basic") / ".chameleon"

    try:
        expect.path_exists(21, rails_chameleon / "COMMITTED")
    except expect.PhaseAssertionError as e:
        notes_extra[21] = str(e)

    profile_json = rails_chameleon / "profile.json"
    if 21 not in notes_extra:
        try:
            expect.path_exists(21, profile_json)
            profile_data = json.loads(profile_json.read_text(encoding="utf-8"))
            lang = profile_data.get("language") or profile_data.get("language_hint", "")
            if str(lang).lower() not in ("ruby", "rails"):
                notes_extra[21] = f"profile.json language field is {lang!r}, expected 'ruby'"
        except expect.PhaseAssertionError as e:
            notes_extra[21] = str(e)
        except (json.JSONDecodeError, OSError) as e:
            notes_extra[21] = f"profile.json read/parse error: {e}"

    rules_json = rails_chameleon / "rules.json"
    if 21 not in notes_extra:
        try:
            expect.path_exists(21, rules_json)
            rules_data = json.loads(rules_json.read_text(encoding="utf-8"))
            if "rubocop" not in rules_data:
                notes_extra[21] = (
                    f"rules.json missing 'rubocop' key; keys found: {list(rules_data)!r}"
                )
        except expect.PhaseAssertionError as e:
            notes_extra[21] = str(e)
        except (json.JSONDecodeError, OSError) as e:
            notes_extra[21] = f"rules.json read/parse error: {e}"

    archetypes_json = rails_chameleon / "archetypes.json"
    if 21 not in notes_extra:
        try:
            expect.path_exists(21, archetypes_json)
            archetypes_data = json.loads(archetypes_json.read_text(encoding="utf-8"))
            if isinstance(archetypes_data, list):
                names = [
                    a.get("name", "") if isinstance(a, dict) else str(a) for a in archetypes_data
                ]
            elif isinstance(archetypes_data, dict):
                names = list(archetypes_data.keys())
            else:
                names = []
            rails_patterns = ("controller", "model", "service", "spec", "job", "mailer")
            matched = [n for n in names if any(p in n.lower() for p in rails_patterns)]
            if len(matched) < 3:
                notes_extra[21] = (
                    f"archetypes.json has {len(matched)} Rails-shaped names "
                    f"(need >= 3); names found: {names!r}"
                )
        except expect.PhaseAssertionError as e:
            notes_extra[21] = str(e)
        except (json.JSONDecodeError, OSError) as e:
            notes_extra[21] = f"archetypes.json read/parse error: {e}"

    idioms_md = rails_chameleon / "idioms.md"
    if 21 not in notes_extra:
        if idioms_md.exists():
            idioms_content = idioms_md.read_text(encoding="utf-8")
            if not re.search(r"Language:\s*ruby", idioms_content, re.IGNORECASE):
                notes_extra[21] = (
                    "idioms.md missing 'Language: ruby' frontmatter after /chameleon-teach"
                )

    cross_check_passed[21] = 21 not in notes_extra

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
        act_id="07_rails_parity",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
