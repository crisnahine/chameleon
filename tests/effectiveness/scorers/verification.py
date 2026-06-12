"""Verification scorer.

Four deterministic signals:
- test_run_seen: HMAC-verified, exit-0-only exec-log read
  (exec_log.session_test_run_seen). Structurally ALWAYS False on the off arm:
  CHAMELEON_DISABLE silences the recorder hook, so no log exists. Reported
  as-is; cross-arm comparison uses the transcript signal below.
- test_cmd_in_transcript: any Bash tool_use whose command classifies as a
  test runner (exec_log.classify_test_command — pure function). Works on
  every arm; cannot confirm exit 0, which is exactly why both are reported.
- stop_gate_seen: a Stop hook_response appeared in the stream.
- test_nudge_seen: the stop-backstop's "No passing test run was recorded"
  strengthening line appeared in any hook stdout.

Unscored when the transcript yielded no session_id (the exec-log read would
silently report False against the wrong key).
"""

from __future__ import annotations

from tests.effectiveness.scorers.base import ScoreContext, unscored

_NUDGE_MARKER = "No passing test run was recorded"


def _session_test_run_seen(repo_id: str, session_id: str) -> bool:
    """Seam: tests monkeypatch this."""
    from chameleon_mcp.exec_log import session_test_run_seen

    return session_test_run_seen(repo_id, session_id)


def score(ctx: ScoreContext) -> dict:
    if not ctx.session_id:
        return unscored("no session_id extracted from transcript")

    from chameleon_mcp.exec_log import classify_test_command

    test_cmd = any(classify_test_command(cmd) for cmd in ctx.bash_commands)
    stop_gate = any("stop" in (e.hook_name or "").lower() for e in ctx.hook_events)
    nudge = any(_NUDGE_MARKER in (e.stdout or "") for e in ctx.hook_events)

    return {
        "test_run_seen": bool(_session_test_run_seen(ctx.repo_id, ctx.session_id)),
        "test_cmd_in_transcript": test_cmd,
        "stop_gate_seen": stop_gate,
        "test_nudge_seen": nudge,
    }
