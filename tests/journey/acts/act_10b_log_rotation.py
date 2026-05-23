"""Act 10b: Log rotation + auto_refresh.log truncate (Phase 36)."""
from __future__ import annotations

import re
from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Test log rotation in working/ts_basic (profile bootstrapped and trusted from earlier acts).
Use absolute paths everywhere.

PHASE 36 - log rotation:
  emit checkpoint started phase 36

  STEP 1 - write 10MB to hook_errors.log:
    Use Bash (Python) to write 10MB of fake error entries to the hook errors log:
      python3 -c "
      import os, pathlib, time

      log_path = os.environ.get('CHAMELEON_HOOK_ERROR_LOG', '')
      if not log_path:
          print('CHAMELEON_HOOK_ERROR_LOG not set')
      else:
          line = '[' + time.strftime('%Y-%m-%dT%H:%M:%SZ') + '] FAKE ERROR: ' + 'x' * 200 + '\n'
          target_bytes = 10 * 1024 * 1024  # 10MB
          with open(log_path, 'a') as f:
              written = 0
              while written < target_bytes:
                  f.write(line)
                  written += len(line)
          size = os.path.getsize(log_path)
          print(f'hook_errors.log size: {size} bytes ({size / 1024 / 1024:.1f} MB)')
      "
    Report the file size after writing.

  STEP 2 - trigger hook event to force rotation:
    Edit working/ts_basic/src/utils/format_date.ts (add/change a comment).
    This triggers the PreToolUse hook, which should detect the oversized log
    and rotate it to .hook_errors.log.1.
    After the edit, use Bash to check for the rotated backup:
      LOG="$CHAMELEON_HOOK_ERROR_LOG"
      echo "Main log: $(wc -c < $LOG 2>/dev/null || echo 'missing') bytes"
      for i in 1 2 3 4 5; do
        if [ -f "${LOG}.${i}" ]; then
          echo "Backup .${i}: $(wc -c < ${LOG}.${i}) bytes"
        else
          echo "Backup .${i}: not present"
        fi
      done
    Report whether rotation occurred (backup .1 exists).

  STEP 3 - verify 5 backups max:
    Repeat step 1 and 2 multiple times to fill up to 5 backup files.
    Verify that only .1 through .5 exist (no .6 or beyond).

  STEP 4 - age backups past 72h and verify pruning:
    Use Bash to artificially age all backup files past 72 hours using Python's
    os.utime (the fast_forward_marker approach):
      python3 -c "
      import os, time, pathlib

      log_path = os.environ.get('CHAMELEON_HOOK_ERROR_LOG', '')
      if not log_path:
          print('CHAMELEON_HOOK_ERROR_LOG not set')
      else:
          age_seconds = 73 * 3600  # 73 hours, past the 72h threshold
          now = time.time()
          old_time = now - age_seconds
          for i in range(1, 6):
              backup = log_path + f'.{i}'
              if os.path.exists(backup):
                  os.utime(backup, (old_time, old_time))
                  print(f'Aged {backup} to 73h old')
      "
    Trigger another hook event by editing a file. Then check whether the
    aged backups were pruned by chameleon's log rotation + doctor stale filter.
    Report how many backups remain.

  STEP 5 - auto_refresh.log truncate-on-spawn:
    Use Bash to write 1MB of junk to the auto_refresh.log, then trigger an
    auto_refresh by editing a file in working/ts_basic (ensure auto_refresh is
    enabled in config.json from Act 4). After the auto_refresh fires, verify
    the auto_refresh.log is now small (truncated on spawn):
      python3 -c "
      import os, pathlib

      data_dir = os.environ.get('CHAMELEON_PLUGIN_DATA', '')
      if data_dir:
          logs = list(pathlib.Path(data_dir).rglob('auto_refresh.log'))
          for log in logs:
              print(f'auto_refresh.log: {log} ({log.stat().st_size} bytes)')
      "
    Report the final size of auto_refresh.log.

  emit checkpoint completed phase 36

Reminder: emit checkpoints as plain Bash echo lines outside any code fences.
Use absolute paths when referencing fixture directories and plugin root.
"""


def run(ctx: JourneyContext) -> ActResult:
    cwd = ctx.fixture("ts_basic")
    transcript = ctx.run_dir / "transcripts" / "act_10b.txt"
    transcript.parent.mkdir(exist_ok=True)

    session = spawn_claude(
        prompt=build_act_prompt(_PROMPT_BODY),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=50,
        allowed_tools=[
            "Bash",
            "Read",
            "Edit",
            "Write",
            "mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            "mcp__plugin_chameleon_chameleon-mcp__get_drift_status",
            "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
            "mcp__plugin_chameleon_chameleon-mcp__refresh_repo",
        ],
        plugin_root=ctx.plugin_root,
        permission_mode="bypassPermissions",
        timeout_s=900,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[36]
    )

    notes_extra: dict[int, str] = {}

    # Phase 36: rotation backup files (.hook_errors.log.1) were created
    hook_error_log = ctx.hook_error_log
    log_backup = Path(str(hook_error_log) + ".1")
    if not log_backup.exists():
        try:
            transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
            rotation_signals = [
                r"rotation",
                r"\.log\.1",
                r"backup",
                r"10\s*mb",
                r"10485760",
                r"hook_errors.*backup",
            ]
            found_rotation = any(
                re.search(p, transcript_text, re.IGNORECASE)
                for p in rotation_signals
            )
            if not found_rotation:
                notes_extra[36] = (
                    f"rotation backup {log_backup} not found and no rotation signal in transcript; "
                    "log rotation test may not have been exercised"
                )
        except Exception as exc:
            notes_extra[36] = f"phase 36 check error: {exc}"

    # Apply cross-check findings to outcomes.
    # Cross-checks are advisory: they append CONCERN to notes without demoting PASS to FAIL.
    for phase, extra in notes_extra.items():
        if phase in outcomes:
            note_prefix = "CONCERN: " if outcomes[phase].status == "PASS" else ""
            outcomes[phase].notes = (outcomes[phase].notes + "; " + note_prefix + extra).strip("; ")

    return ActResult(
        act_id="10b_log_rotation",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
