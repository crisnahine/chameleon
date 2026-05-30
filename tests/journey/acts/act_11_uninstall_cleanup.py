"""Act 11: Uninstall + cleanup + isolation verify (Phase 37)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext

_PROMPT_BODY = """\
Uninstall chameleon from the EPHEMERAL per-run plugin data directory and verify
clean isolation. Read this carefully before acting.

IMPORTANT SCOPE CLARIFICATION:
  The "uninstall" target in this act is ONLY the per-run ephemeral state stored
  under $CHAMELEON_PLUGIN_DATA (the journey harness's isolated data directory).
  Do NOT touch the chameleon source repository files:
    - .claude-plugin/plugin.json
    - .claude-plugin/marketplace.json
    - hooks/hooks.json
  These are the developer's actual manifest files in the chameleon repo. They
  MUST NOT be removed or modified. They are not part of the "install" being
  tested here - this act only tests wipe of the per-run plugin data state.

  Also do NOT touch:
    - ~/.local/share/chameleon/ (the developer's real chameleon data)
    - ~/.claude/hooks/.exec_hmac.key (the developer's real HMAC key)
  These must remain UNTOUCHED throughout the entire journey harness.

PHASE 37 - uninstall + cleanup + isolation verify:
  emit checkpoint started phase 37

  STEP 1 - wipe the ephemeral plugin data:
    Use Bash to wipe the per-run chameleon data directory:
      echo "Wiping ephemeral plugin data: $CHAMELEON_PLUGIN_DATA"
      rm -rf "$CHAMELEON_PLUGIN_DATA"
      echo "Wipe complete"
    Verify it is gone:
      if [ -d "$CHAMELEON_PLUGIN_DATA" ]; then
        echo "FAIL: $CHAMELEON_PLUGIN_DATA still exists"
      else
        echo "PASS: $CHAMELEON_PLUGIN_DATA removed"
      fi

  STEP 2 - verify daemon is dead:
    Use Bash to check no chameleon daemon process is running:
      ps aux | grep chameleon_mcp.daemon | grep -v grep
    If that returns nothing, the daemon is confirmed dead. Report the result.

  STEP 3 - verify no chameleon processes:
    Use Bash to verify no chameleon-related processes remain:
      ps aux | grep -E 'chameleon_mcp|chameleon-mcp' | grep -v grep
    Report whether any processes are found. If any are, kill them and report.

  STEP 4 - attempt list_profiles after wipe:
    Call chameleon-mcp::list_profiles. Since the plugin data was wiped, this
    may fail (MCP server unreachable or returns empty). Report whatever
    response you get - an empty list, an error, or a connection failure are
    all acceptable outcomes. The key verification is that no stale state
    appears from before the wipe.

  STEP 5 - verify developer's home dir was NOT touched:
    Use Bash to verify the developer's real chameleon data is untouched:
      # Check ~/.local/share/chameleon/ - should NOT contain our run_dir paths
      REAL_DATA="$HOME/.local/share/chameleon"
      if [ -d "$REAL_DATA" ]; then
        echo "Real chameleon data dir exists: $REAL_DATA"
        ls "$REAL_DATA" | head -10
      else
        echo "Real chameleon data dir does not exist (acceptable)"
      fi

      # Check ~/.claude/hooks/.exec_hmac.key - should exist if developer uses chameleon
      REAL_KEY="$HOME/.claude/hooks/.exec_hmac.key"
      if [ -f "$REAL_KEY" ]; then
        echo "Real HMAC key exists: $REAL_KEY (untouched by harness)"
      else
        echo "Real HMAC key not found (may not be set up on this machine)"
      fi
    Report the state of both paths.

  STEP 6 - verify chameleon repo manifest files are intact:
    Use Bash to confirm the chameleon source manifests were NOT removed:
      PLUGIN_ROOT="PLUGIN_ROOT_PATH"
      for f in \
        "$PLUGIN_ROOT/.claude-plugin/plugin.json" \
        "$PLUGIN_ROOT/.claude-plugin/marketplace.json" \
        "$PLUGIN_ROOT/hooks/hooks.json"; do
        if [ -f "$f" ]; then
          echo "OK: $f exists"
        else
          echo "MISSING: $f (should not have been removed)"
        fi
      done
    Replace PLUGIN_ROOT_PATH with the absolute path to the chameleon repo root.
    Report which files are present.

  emit checkpoint completed phase 37

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_11.txt"
    transcript.parent.mkdir(exist_ok=True)

    prompt_body = _PROMPT_BODY.replace("PLUGIN_ROOT_PATH", str(ctx.plugin_root))

    session = spawn_claude(
        prompt=build_act_prompt(prompt_body),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=25,
        allowed_tools=[
            "Bash",
            "Read",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
            "mcp__plugin_chameleon_chameleon-mcp__daemon_status",
        ],
        plugin_root=ctx.plugin_root,
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[37]
    )

    notes_extra: dict[int, str] = {}
    cross_check_passed: dict[int, bool] = {}


    plugin_data = ctx.plugin_data_dir
    if plugin_data.exists():
        contents = list(plugin_data.iterdir())
        non_lock = [
            p for p in contents
            if p.name not in (".lock", ".gitkeep")
            and not p.suffix == ".lock"
            # version-scoped daemon runtime files: .daemon-<version>.{sock,pid}
            and not p.name.startswith(".daemon-")
            and p.name != ".daemon.sock"
        ]
        if non_lock:
            notes_extra[37] = (
                f"plugin_data_dir {plugin_data} not fully wiped; "
                f"remaining entries: {[p.name for p in non_lock[:5]]}"
            )

    real_chameleon = Path.home() / ".local" / "share" / "chameleon"
    if real_chameleon.exists() and 37 not in notes_extra:
        try:
            for entry in real_chameleon.iterdir():
                try:
                    entry.resolve().relative_to(ctx.run_dir.resolve())
                    existing = notes_extra.get(37, "")
                    notes_extra[37] = (
                        (existing + "; " if existing else "") +
                        f"real chameleon data dir contains entry {entry.name} "
                        "that is inside the harness run_dir - isolation violation"
                    ).strip("; ")
                    break
                except ValueError:
                    pass
        except Exception as exc:
            existing = notes_extra.get(37, "")
            notes_extra[37] = (
                (existing + "; " if existing else "") +
                f"home dir isolation scan error: {exc}"
            ).strip("; ")

    plugin_json = ctx.plugin_root / ".claude-plugin" / "plugin.json"
    try:
        expect.path_exists(37, plugin_json)
    except expect.PhaseAssertionError as e:
        existing = notes_extra.get(37, "")
        notes_extra[37] = (
            (existing + "; " if existing else "") + str(e)
        ).strip("; ")

    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        daemon_lines = [
            line for line in result.stdout.splitlines()
            if "chameleon_mcp.daemon" in line and "grep" not in line
        ]
        if daemon_lines:
            existing = notes_extra.get(37, "")
            notes_extra[37] = (
                (existing + "; " if existing else "") +
                f"chameleon daemon process still running after uninstall: {daemon_lines[0][:120]}"
            ).strip("; ")
    except Exception as exc:
        existing = notes_extra.get(37, "")
        notes_extra[37] = (
            (existing + "; " if existing else "") +
            f"daemon process check failed: {exc}"
        ).strip("; ")

    cross_check_passed[37] = 37 not in notes_extra

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
        act_id="11_uninstall_cleanup",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
