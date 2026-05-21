"""Act 2: Init flow (TS, both auto_rename modes + force=True) (Phases 5, 6, 7, 15)."""
from __future__ import annotations

import json
from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import PhaseOutcome, parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Bootstrap two TS fixtures.

PHASE 5 - cold-start init interactive:
  emit checkpoint started phase 5
  First fixture: working/ts_basic. Use Bash to create .chameleon/config.json
  with content {"auto_rename": false}. Then run /chameleon-init. Step through
  the rename interview (at most 3 prompts), accepting defaults for each.
  After bootstrap completes, verify all of the following exist:
    .chameleon/COMMITTED
    .chameleon/profile.json
    .chameleon/canonicals.json
    .chameleon/archetypes.json
    .chameleon/rules.json
    .chameleon/idioms.md
    .chameleon/summary.md
  Use Bash to read .chameleon/profile.json and confirm schema_version is 7.
  emit checkpoint completed phase 5

PHASE 6 - cold-start init auto_rename:
  emit checkpoint started phase 6
  Second fixture: use Bash to cd into working/ts_monorepo (the monorepo fixture
  with 2 workspace packages). Create .chameleon/config.json with
  {"auto_rename": true}. Run /chameleon-init. Verify that NO rename interview
  appears - with auto_rename true the init should complete without prompting.
  After bootstrap, read .chameleon/archetype_renames.json (if it exists).
  Verify that only fallback names (cluster-*, class-*, numeric disambiguators)
  were auto-renamed and user-provided names were preserved.
  emit checkpoint completed phase 6

PHASE 7 - trust security:
  emit checkpoint started phase 7
  Back in working/ts_basic: run /chameleon-trust and confirm the trust prompt,
  typing the repo name when asked. Verify trust is granted.
  Then test the force=True overwrite path:
    Call chameleon-mcp::bootstrap_repo with path set to the ts_basic fixture
    path and no force flag. Expect status "already_bootstrapped".
    Then call chameleon-mcp::bootstrap_repo again with force=True.
    Expect successful overwrite (status "ok" or "bootstrapped").
    After the force overwrite, verify trust state has flipped to stale because
    the profile SHA changed (a fresh bootstrap replaces the profile).
  emit checkpoint completed phase 7

PHASE 15 - auto_rename ledger:
  emit checkpoint started phase 15
  In working/ts_monorepo, read the .chameleon/archetype_renames.json file
  (if present after the auto_rename init from Phase 6). Verify:
    - The file is valid JSON.
    - The structure is an array or object with at most 256 entries (FIFO cap).
    - Each entry represents an auto-rename with the old and new name.
  If the file does not exist, report that no renames were needed for this
  small fixture (acceptable for Phase 15 - the cap constraint is structural).
  Use Bash to confirm the file size or entry count.
  emit checkpoint completed phase 15

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing the fixture directories.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_02.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=20,
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
            "mcp__plugin_chameleon_chameleon-mcp__propose_archetype_renames",
            "mcp__plugin_chameleon_chameleon-mcp__apply_archetype_renames",
            "mcp__plugin_chameleon_chameleon-mcp__refresh_repo",
            "mcp__plugin_chameleon_chameleon-mcp__trust_profile",
            "mcp__plugin_chameleon_chameleon-mcp__doctor",
        ],
        plugin_root=ctx.plugin_root,
        timeout_s=900,
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[5, 6, 7, 15]
    )

    # Runner-side cross-checks
    notes_extra: dict[int, str] = {}

    # Phase 5: profile.json schema_version == 7 in ts_basic/.chameleon/
    ts_basic_chameleon = ctx.fixture("ts_basic") / ".chameleon"
    profile_json = ts_basic_chameleon / "profile.json"
    try:
        expect.path_exists(5, profile_json)
        expect.json_field(5, profile_json, "schema_version", 7)
        expect.path_exists(5, ts_basic_chameleon / "COMMITTED")
        expect.path_exists(5, ts_basic_chameleon / "canonicals.json")
        expect.path_exists(5, ts_basic_chameleon / "archetypes.json")
        expect.path_exists(5, ts_basic_chameleon / "idioms.md")
        expect.path_exists(5, ts_basic_chameleon / "summary.md")
    except expect.PhaseAssertionError as e:
        notes_extra[5] = str(e)

    # Phase 6: ts_monorepo/.chameleon/profile.json exists after auto_rename init
    ts_monorepo_chameleon = ctx.fixture("ts_monorepo") / ".chameleon"
    try:
        expect.path_exists(6, ts_monorepo_chameleon / "profile.json")
    except expect.PhaseAssertionError as e:
        notes_extra[6] = str(e)

    # Phase 7: trust file exists under chameleon_data after trust was granted
    # Trust files live under <plugin_data_dir>/<repo_id>/trust.json or similar.
    # We verify at minimum that ts_basic has a .chameleon dir and COMMITTED still present
    # after force=True overwrite (profile was re-bootstrapped).
    try:
        expect.path_exists(7, ts_basic_chameleon / "COMMITTED")
        expect.path_exists(7, profile_json)
    except expect.PhaseAssertionError as e:
        notes_extra[7] = str(e)

    # Phase 15: archetype_renames.json in ts_monorepo has <= 256 entries
    renames_json = ts_monorepo_chameleon / "archetype_renames.json"
    if renames_json.exists():
        try:
            data = json.loads(renames_json.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else list(data.values()) if isinstance(data, dict) else []
            if len(entries) > 256:
                notes_extra[15] = f"archetype_renames.json has {len(entries)} entries, expected <= 256"
        except (json.JSONDecodeError, Exception) as e:
            notes_extra[15] = f"archetype_renames.json parse error: {e}"
    # If renames_json doesn't exist, that's acceptable for a small fixture

    # Apply cross-check findings to outcomes
    for phase, extra in notes_extra.items():
        if phase in outcomes and outcomes[phase].status == "PASS":
            outcomes[phase].status = "FAIL"
            outcomes[phase].notes = (outcomes[phase].notes + "; " + extra).strip("; ")
        elif phase not in outcomes:
            # Phase not seen at all but we have a cross-check failure
            pass

    return ActResult(
        act_id="02_init_flow",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
