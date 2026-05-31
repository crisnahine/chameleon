"""Act 2: Init flow (TS, both auto_rename modes + force=True) (Phases 5, 6, 7, 15)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
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
    .chameleon/profile.summary.md
  Use Bash to read .chameleon/profile.json and confirm schema_version is 8.
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
  Back in working/ts_basic: grant trust by calling the MCP tool directly:
    chameleon-mcp::trust_profile(repo=<absolute path to ts_basic>, confirmation_token="ts_basic")
  Verify the response has status "success" and a trusted_at timestamp.
  Do NOT use the /chameleon-trust skill (it costs extra turns). Call the tool directly.
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


def _compute_fixture_repo_id(repo_path: Path) -> str:
    """Mirror _compute_repo_id from tools.py: hash the git remote URL or path."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        url = result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        url = ""

    if url:
        s = url.strip()
        scp_re = re.compile(r"^([a-zA-Z0-9._-]+):([^/].*)$")
        m = scp_re.match(s)
        if m and "://" not in s:
            host, path = m.group(1), m.group(2)
            s = f"ssh://git@{host}/{path}"
        s = re.sub(r"\.git/?$", "", s)
        s = s.rstrip("/")
        proto_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+\-.]*)://([^/]+)(/.*)?$", s)
        if proto_match:
            scheme = proto_match.group(1)
            host = proto_match.group(2)
            path = proto_match.group(3) or ""
            if "@" in host:
                host = host.split("@", 1)[1]
            case_insensitive = {
                "github.com",
                "gitlab.com",
                "bitbucket.org",
                "dev.azure.com",
                "ssh.dev.azure.com",
            }
            if host.lower() in case_insensitive:
                host = host.lower()
                scheme = "https"
            canonical = f"{scheme}://{host}{path}"
        else:
            canonical = s
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return hashlib.sha256(str(repo_path.resolve()).encode("utf-8")).hexdigest()


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_02.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=55,
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
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[5, 6, 7, 15]
    )

    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    ts_basic_chameleon = ctx.fixture("ts_basic") / ".chameleon"
    profile_json = ts_basic_chameleon / "profile.json"
    try:
        expect.path_exists(5, profile_json)
        # Tracks CURRENT_SCHEMA_VERSION in mcp/chameleon_mcp/profile/schema.py.
        # Bump this in lockstep when the schema version changes.
        expect.json_field(5, profile_json, "schema_version", 8)
        expect.path_exists(5, ts_basic_chameleon / "COMMITTED")
        expect.path_exists(5, ts_basic_chameleon / "canonicals.json")
        expect.path_exists(5, ts_basic_chameleon / "archetypes.json")
        expect.path_exists(5, ts_basic_chameleon / "idioms.md")
        expect.path_exists(5, ts_basic_chameleon / "profile.summary.md")
        cross_check_passed[5] = True
    except expect.PhaseAssertionError as e:
        notes_extra[5] = str(e)
        cross_check_passed[5] = False

    ts_monorepo_chameleon = ctx.fixture("ts_monorepo") / ".chameleon"
    try:
        expect.path_exists(6, ts_monorepo_chameleon / "profile.json")
        cross_check_passed[6] = True
    except expect.PhaseAssertionError as e:
        notes_extra[6] = str(e)
        cross_check_passed[6] = False

    try:
        expect.path_exists(7, ts_basic_chameleon / "COMMITTED")
        expect.path_exists(7, profile_json)

        ts_basic_path = ctx.fixture("ts_basic")
        repo_id = _compute_fixture_repo_id(ts_basic_path)
        trust_path = ctx.plugin_data_dir / repo_id / ".trust"
        expect.path_exists(7, trust_path)
        cross_check_passed[7] = True
    except expect.PhaseAssertionError as e:
        notes_extra[7] = str(e)
        cross_check_passed[7] = False

    renames_json = ts_monorepo_chameleon / "archetype_renames.json"
    if renames_json.exists():
        try:
            data = json.loads(renames_json.read_text(encoding="utf-8"))
            entries = (
                data
                if isinstance(data, list)
                else list(data.values())
                if isinstance(data, dict)
                else []
            )
            if len(entries) > 256:
                notes_extra[15] = (
                    f"archetype_renames.json has {len(entries)} entries, expected <= 256"
                )
                cross_check_passed[15] = False
            else:
                cross_check_passed[15] = True
        except (json.JSONDecodeError, Exception) as e:
            notes_extra[15] = f"archetype_renames.json parse error: {e}"
            cross_check_passed[15] = False
    else:
        cross_check_passed[15] = True

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
        act_id="02_init_flow",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
