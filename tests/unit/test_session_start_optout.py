"""SessionStart honors the opt-out hierarchy (.skip / disable / pause).

Regression guard: session_start() must gate on is_chameleon_suppressed like
PreToolUse/PostToolUse do, so a .skip / paused / session-disabled repo gets no
skill injection, no statusLine write, and no auto-refresh at session start.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugin"


def _run_session_start(suppressed_reason, tmp_path) -> tuple[int, str]:
    captured: list[str] = []
    payload = {"session_id": "s1"}
    env = {
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT),
        "CHAMELEON_PLUGIN_DATA": str(tmp_path / "data"),
    }
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, env, clear=False),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=suppressed_reason),
        patch("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None),
    ):
        mock_stdout.write = lambda s: captured.append(s)
        from chameleon_mcp.hook_helper import session_start

        rc = session_start()
    return rc, "".join(captured).strip()


def test_session_start_suppressed_emits_nothing_and_skips_statusline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc, out = _run_session_start("repo_skip", tmp_path)
    assert rc == 0
    assert out in ("{}", "")  # no skill primer injected
    assert "using-chameleon" not in out
    # the statusLine settings.local.json write was skipped
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_session_start_not_suppressed_still_injects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc, out = _run_session_start(None, tmp_path)
    assert rc == 0
    # control: the gate must not fire when not suppressed — skill is injected
    assert out not in ("{}", "")
    assert "chameleon" in out.lower()
    # Phase 4: the injection is the curated operational digest, not the full
    # ~13.6k-char using-chameleon SKILL.md body.
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "operational digest" in ctx.lower()
    # A line that lives ONLY in the full SKILL.md's ASCII flow diagram (dropped
    # from the digest per the curation) must be absent.
    assert "untrusted prompt (once) --> edit proceeds without canonical" not in ctx
    # And the slash-command reference table (also dropped) must be absent.
    assert "| Command | Purpose |" not in ctx
    # Budget bound: nowhere near the old ~13,588-char full-skill dump.
    assert len(ctx) < 6000
