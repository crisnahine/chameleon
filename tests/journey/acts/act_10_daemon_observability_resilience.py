"""Act 10: Daemon + observability + resilience (Phases 33, 34, 35, 36)."""
from __future__ import annotations

import re
from pathlib import Path

from tests.journey.acts.act_base import ActResult, build_act_prompt
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import parse_checkpoint_file
from tests.journey.harness.claude import spawn_claude
from tests.journey.harness.context import JourneyContext


_PROMPT_BODY = """\
Daemon lifecycle, metrics observability, log rotation, and hook fail-open
resilience. Use absolute paths everywhere.

PHASE 33 - daemon lifecycle + serial queue + idle shutdown:
  emit checkpoint started phase 33

  STEP 1 - start the daemon with a high idle timeout:
    Use Bash to start the chameleon daemon with a 600-second idle timeout:
      export CHAMELEON_DAEMON_IDLE_TIMEOUT=600
      cd PLUGIN_ROOT
      mcp/.venv/bin/python -m chameleon_mcp.daemon &
      DAEMON_PID=$!
      sleep 2
      echo "daemon started, PID=$DAEMON_PID"
    Replace PLUGIN_ROOT with the absolute path to the chameleon plugin root.
    Report the daemon PID.

  STEP 2 - verify socket and pidfile:
    Use Bash to check:
      SOCK="$CHAMELEON_PLUGIN_DATA/.daemon.sock"
      if [ -S "$SOCK" ]; then
        echo "socket exists: $SOCK"
        stat -c "%a" "$SOCK" 2>/dev/null || stat -f "%Lp" "$SOCK"
      else
        echo "socket NOT found at $SOCK"
      fi
    Report whether the socket exists and its mode (should be 0600).
    Also check for a pidfile:
      ls "$CHAMELEON_PLUGIN_DATA"/.daemon.pid 2>/dev/null && \
        cat "$CHAMELEON_PLUGIN_DATA"/.daemon.pid || echo "no pidfile found"

  STEP 3 - call daemon_status:
    Call chameleon-mcp::daemon_status.
    Verify the response contains the expected fields:
      - alive (boolean true)
      - pid (integer, matches the running daemon PID)
      - uptime_s (positive number)
      - socket_path (path matching CHAMELEON_PLUGIN_DATA)
    Report what fields are present in the response.

  STEP 4 - serial calls and latency:
    Use Bash to make 3 serial calls to chameleon-mcp::list_profiles (or another
    fast read tool) and measure the total time. Since the v0.5 daemon is
    single-threaded, serial calls should queue. The 3 calls should take roughly
    3x the time of a single call:
      python3 -c "
      import time
      # Time a single call baseline
      t0 = time.monotonic()
      # (placeholder - the actual calls happen via MCP tool use above)
      print('Use MCP tool calls above to measure latency')
      "
    Call chameleon-mcp::list_profiles three times in sequence (not concurrently).
    Report the approximate elapsed time for all three calls combined.

  STEP 5 - listen backlog flood test (50 connections):
    Use Bash to attempt 50 rapid connections to the daemon socket and verify
    no ECONNREFUSED errors:
      python3 -c "
      import socket, os, time
      sock_path = os.environ.get('CHAMELEON_PLUGIN_DATA', '') + '/.daemon.sock'
      if not os.path.exists(sock_path):
          print('socket not found, skipping flood test')
      else:
          errors = 0
          connected = 0
          for i in range(50):
              s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
              try:
                  s.settimeout(1.0)
                  s.connect(sock_path)
                  connected += 1
                  s.close()
              except ConnectionRefusedError:
                  errors += 1
              except Exception:
                  pass  # timeout/other is ok
          print(f'50-conn flood: {connected} connected, {errors} ECONNREFUSED')
          if errors > 0:
              print(f'FAIL: {errors} ECONNREFUSED errors')
          else:
              print('PASS: no ECONNREFUSED')
      "
    Report the flood test result.

  STEP 6 - idle shutdown:
    Use Bash to restart the daemon with a 2-second idle timeout and verify it
    exits automatically after 4 seconds:
      # Kill the previous daemon first
      kill $DAEMON_PID 2>/dev/null || true
      sleep 1
      export CHAMELEON_DAEMON_IDLE_TIMEOUT=2
      cd PLUGIN_ROOT
      mcp/.venv/bin/python -m chameleon_mcp.daemon &
      NEW_PID=$!
      echo "new daemon PID=$NEW_PID"
      sleep 4
      if kill -0 $NEW_PID 2>/dev/null; then
        echo "FAIL: daemon still alive after 4s idle with 2s timeout"
      else
        echo "PASS: daemon exited after idle timeout"
      fi
      # Verify pidfile removed
      ls "$CHAMELEON_PLUGIN_DATA"/.daemon.pid 2>/dev/null && \
        echo "FAIL: pidfile still present" || echo "PASS: pidfile removed"
    Replace PLUGIN_ROOT with the absolute path to the chameleon plugin root.
    Report whether the daemon exited and the pidfile was removed.

  emit checkpoint completed phase 33

PHASE 34 - hook fail-open + Python fallback chain:
  emit checkpoint started phase 34

  STEP 1 - mask Python and trigger SessionStart:
    Use Bash to temporarily mask Python interpreters by restricting PATH, then
    spawn a new claude -p session to trigger the SessionStart hook:
      python3 -c "
      import subprocess, os

      # Build a restricted PATH with no python
      restricted_path = '/usr/bin:/bin'

      # Spawn a minimal claude session that just exits immediately
      # The SessionStart hook should fail-open (emit {} instead of crashing)
      result = subprocess.run(
          ['claude', '-p', 'echo hello; exit 0'],
          env={**os.environ, 'PATH': restricted_path},
          capture_output=True, text=True, timeout=30
      )
      print('exit code:', result.returncode)
      print('stdout:', result.stdout[:500])
      print('stderr:', result.stderr[:500])
      "
    Report whether the session exited cleanly (not a crash), and whether
    the hook_errors.log captured a failure entry.

  STEP 2 - verify hook_errors.log:
    Use Bash to check the hook errors log:
      LOG="$CHAMELEON_HOOK_ERROR_LOG"
      if [ -f "$LOG" ]; then
        echo "hook_errors.log exists, size: $(wc -c < $LOG) bytes"
        tail -5 "$LOG"
      else
        echo "hook_errors.log not found at $LOG"
      fi
    Report whether the log has content (captured hook failures).

  STEP 3 - verify {} emission (fail-open):
    The preflight-and-advise hook should have emitted {} (empty JSON object)
    when all Python interpreters were unavailable. Report whether the session
    received an empty advisory or proceeded without advisory injection.

  emit checkpoint completed phase 34

PHASE 35 - metrics emission:
  emit checkpoint started phase 35

  Use Bash to inspect the metrics.jsonl file from prior acts:
    python3 -c "
    import os, pathlib, json

    data_dir = os.environ.get('CHAMELEON_PLUGIN_DATA', '')
    if not data_dir:
        print('CHAMELEON_PLUGIN_DATA not set')
    else:
        metrics_files = list(pathlib.Path(data_dir).rglob('metrics.jsonl'))
        if not metrics_files:
            print('no metrics.jsonl found under', data_dir)
        else:
            for mf in metrics_files:
                print(f'Found metrics.jsonl: {mf} ({mf.stat().st_size} bytes)')
                lines = mf.read_text(errors='replace').splitlines()
                print(f'  {len(lines)} entries')
                if lines:
                    # Parse first entry
                    try:
                        entry = json.loads(lines[0])
                        print('  First entry keys:', sorted(entry.keys()))
                    except json.JSONDecodeError as e:
                        print(f'  First entry parse error: {e}')
    "
  Verify the metrics.jsonl file exists and has at least one entry.
  For each entry found, verify the presence of the expected per-call fields:
    ts, hook, repo_id, elapsed_ms, advisory_emitted, suppression_reason,
    fail_open, trust_state, archetype, confidence
  Report which fields are present and which are missing (if any).

  emit checkpoint completed phase 35

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
    transcript = ctx.run_dir / "transcripts" / "act_10.txt"
    transcript.parent.mkdir(exist_ok=True)

    # Inject plugin_root into the prompt so Claude can reference it
    prompt_body = _PROMPT_BODY.replace("PLUGIN_ROOT", str(ctx.plugin_root))

    session = spawn_claude(
        prompt=build_act_prompt(prompt_body),
        cwd=cwd,
        env={**ctx.env, "CHAMELEON_JOURNEY_CHECKPOINT": str(ctx.current_checkpoint_file)},
        transcript_path=transcript,
        max_turns=40,
        allowed_tools=[
            "Bash",
            "Read",
            "Edit",
            "Write",
            "mcp__plugin_chameleon_chameleon-mcp__daemon_status",
            "mcp__plugin_chameleon_chameleon-mcp__detect_repo",
            "mcp__plugin_chameleon_chameleon-mcp__get_archetype",
            "mcp__plugin_chameleon_chameleon-mcp__get_drift_status",
            "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
            "mcp__plugin_chameleon_chameleon-mcp__get_rules",
            "mcp__plugin_chameleon_chameleon-mcp__list_profiles",
            "mcp__plugin_chameleon_chameleon-mcp__refresh_repo",
        ],
        plugin_root=ctx.plugin_root,
        timeout_s=1200,
        add_dirs=[ctx.run_dir],
    )

    outcomes, parse_errors = parse_checkpoint_file(
        ctx.current_checkpoint_file, expected_phases=[33, 34, 35, 36]
    )

    notes_extra: dict[int, str] = {}

    # Phase 33: daemon socket was created at some point (may be gone by end of act)
    # Primary signal comes from transcript + checkpoint. Check transcript for daemon evidence.
    try:
        transcript_text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        daemon_signals = [
            r"daemon.*start",
            r"\.daemon\.sock",
            r"daemon_status",
            r"uptime_s",
            r"idle.*timeout",
            r"pidfile",
        ]
        found_daemon = any(
            re.search(p, transcript_text, re.IGNORECASE)
            for p in daemon_signals
        )
        if not found_daemon:
            notes_extra[33] = (
                "no daemon lifecycle signal found in transcript; "
                "daemon start/stop/status tests may not have been exercised"
            )
    except Exception as exc:
        notes_extra[33] = f"transcript scan error for phase 33: {exc}"

    # Phase 34: hook_error_log was written to during the fail-open test
    hook_error_log = ctx.hook_error_log
    if not hook_error_log.exists() or hook_error_log.stat().st_size == 0:
        notes_extra[34] = (
            f"hook_errors.log at {hook_error_log} is absent or empty; "
            "fail-open hook error capture may not have fired"
        )

    # Phase 35: metrics.jsonl exists and has at least one entry
    metrics_found = list(ctx.plugin_data_dir.rglob("metrics.jsonl"))
    if not metrics_found:
        notes_extra[35] = (
            f"no metrics.jsonl found under {ctx.plugin_data_dir}; "
            "metrics emission may not be active"
        )
    else:
        sample = metrics_found[0]
        if sample.stat().st_size == 0:
            notes_extra[35] = (
                f"metrics.jsonl at {sample} is empty; "
                "no metrics entries were written"
            )

    # Phase 36: rotation backup files (.hook_errors.log.1) were created
    log_backup = Path(str(hook_error_log) + ".1")
    if not log_backup.exists():
        # Rotation may not have completed or log didn't reach 10MB threshold.
        # This is a soft/heuristic check; transcript is the primary signal.
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

    # Apply cross-check findings to outcomes
    for phase, extra in notes_extra.items():
        if phase in outcomes and outcomes[phase].status == "PASS":
            outcomes[phase].status = "FAIL"
            outcomes[phase].notes = (outcomes[phase].notes + "; " + extra).strip("; ")

    return ActResult(
        act_id="10_daemon_observability_resilience",
        cost_usd=session.cost_usd,
        phase_outcomes=list(outcomes.values()),
        checkpoint_parse_errors=parse_errors,
    )
