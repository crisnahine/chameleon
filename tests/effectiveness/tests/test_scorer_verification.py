"""Verification scorer against canned transcripts and a stubbed exec log."""

from __future__ import annotations

from tests.effectiveness.scorers import verification
from tests.effectiveness.tests.test_scorer_base import _ctx
from tests.journey.harness.claude import HookEvent


def test_all_signals_positive(tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "_session_test_run_seen", lambda repo_id, sid: True)
    ctx = _ctx(tmp_path)
    ctx.bash_commands = ["ls", "npm test"]
    ctx.hook_events = [
        HookEvent(hook_name="Stop", stdout="No passing test run was recorded this turn"),
    ]
    out = verification.score(ctx)
    assert out == {
        "test_run_seen": True,
        "test_cmd_in_transcript": True,
        "stop_gate_seen": True,
        "test_nudge_seen": True,
    }


def test_no_test_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "_session_test_run_seen", lambda repo_id, sid: False)
    ctx = _ctx(tmp_path)
    ctx.bash_commands = ["ls", "cat foo.ts", "pip install pytest"]
    out = verification.score(ctx)
    assert out["test_run_seen"] is False
    assert out["test_cmd_in_transcript"] is False
    assert out["stop_gate_seen"] is False


def test_missing_session_id_is_unscored(tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "_session_test_run_seen", lambda repo_id, sid: False)
    ctx = _ctx(tmp_path)
    ctx.session_id = ""
    out = verification.score(ctx)
    assert set(out) == {"unscored"}
    assert "session_id" in out["unscored"]


def test_ruby_itest_command_classified(tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "_session_test_run_seen", lambda repo_id, sid: False)
    ctx = _ctx(tmp_path)
    ctx.bash_commands = ["ruby -Itest tests/run_tests.rb"]
    out = verification.score(ctx)
    assert out["test_cmd_in_transcript"] is True
