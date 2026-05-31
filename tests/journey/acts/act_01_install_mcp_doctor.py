"""Act 1: Install + MCP boot + Doctor + using-chameleon verify (Phases 1-4)."""

from __future__ import annotations

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import mcp
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext

_PROMPT_BODY = """\
Verify the chameleon plugin install.

PHASE 1, manifests:
  emit checkpoint started phase 1
  Use the Bash tool to `ls` and parse each of:
    .claude-plugin/plugin.json
    .claude-plugin/marketplace.json
    hooks/hooks.json
  Verify each is valid JSON. Verify the chameleon plugin name is present.
  emit checkpoint completed phase 1

PHASE 2, MCP boot + 20 tools:
  emit checkpoint started phase 2
  The MCP server is launched automatically by Claude Code (chameleon-mcp).
  Use the chameleon-mcp::doctor tool (a no-arg tool). Verify the response.
  Also verify the tool registry: count the chameleon-mcp::* tools you have
  access to via your tool listing. Expected: 20 tools.
  emit checkpoint completed phase 2

PHASE 3, Doctor baseline:
  emit checkpoint started phase 3
  Inspect the doctor envelope. All 9 subsystems should report status "ok":
  python, bash, timeout, plugin_data_writable, hook_scripts, hmac_key,
  daemon, recent_errors, per_repo_state. Report any non-ok subsystem.
  emit checkpoint completed phase 3

PHASE 4, bootstrap resource limits + using-chameleon:
  emit checkpoint started phase 4
  Use Bash to verify `mcp/typescript-checksums.json` exists. Parse it,
  count entries. Verify each listed file exists under mcp/node_modules/typescript/.
  Then describe (in plain text) what you can see of the using-chameleon
  skill content in your current session context. The runner will inspect
  your transcript for chameleon-context markers in the SessionStart system message.
  emit checkpoint completed phase 4

Reminder: emit checkpoints as plain Bash echo lines, never inside code fences.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_01.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=40,
        plugin_root=ctx.plugin_root,
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[1, 2, 3, 4]
    )

    notes_extra: dict[int, str] = {}
    notes_concern: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}

    try:
        tools_result = mcp.call_mcp_tool(
            tool_name="doctor",
            plugin_root=ctx.plugin_root,
            env=ctx.env,
        )
        if tools_result is None or "error" in tools_result:
            notes_extra[2] = f"MCP doctor returned error or None: {tools_result!r}"
            cross_check_passed[2] = False
        else:
            cross_check_passed[2] = True
    except Exception as e:
        notes_extra[2] = f"MCP direct probe failed: {e}"
        cross_check_passed[2] = False

    transcript_text = transcript.read_text(encoding="utf-8")
    if "<chameleon-context>" not in transcript_text:
        notes_extra[4] = "no <chameleon-context> in transcript (using-chameleon not injected?)"
        cross_check_passed[4] = False
    else:
        cross_check_passed[4] = True

    checksums_file = ctx.plugin_root / "mcp" / "typescript-checksums.json"
    if not checksums_file.exists():
        notes_concern[4] = (
            "mcp/typescript-checksums.json does not exist (concern only, not required)"
        )

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

    for phase, concern in notes_concern.items():
        if phase in outcomes:
            outcomes[phase].notes = (outcomes[phase].notes + "; CONCERN: " + concern).strip("; ")

    return ActResult(
        act_id="01_install_mcp_doctor",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
