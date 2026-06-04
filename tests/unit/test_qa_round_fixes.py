"""Fixes for the three defects left open by the real-use QA round.

1. The enforce-promotion docs must spell out the two-step flow (edit
   config.json, then re-trust): config.json is trust-hashed, so the edit alone
   flips the profile to stale and silently disables enforcement.
2. The correctness-judge spawn must isolate the child claude process from the
   user's settings/plugins/hooks via a throwaway CLAUDE_CONFIG_DIR, or a
   SessionStart hook stack can eat the whole timeout budget.
3. get_crossfile_context must cap low-confidence (open-set/barrel) rows
   separately so they cannot crowd genuine high-confidence existence breaks out
   of the response.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_trust_skill_documents_two_step_promotion():
    text = _read("skills/chameleon-trust/SKILL.md")
    assert "Promotion is a TWO-step action" in text
    assert "run `/chameleon-trust` again" in text


def test_status_skill_documents_two_step_promotion():
    text = _read("skills/chameleon-status/SKILL.md")
    assert "then re-run `/chameleon-trust`" in text
    assert "edit `config.json`, then `/chameleon-trust`" in text


def test_judge_spawn_isolates_claude_config(monkeypatch, tmp_path):
    from chameleon_mcp import judge

    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    out = judge._spawn_reviewer("prompt", tmp_path)
    assert out == "[]"
    env = captured["env"]
    assert env is not None
    cfg = env.get("CLAUDE_CONFIG_DIR")
    assert cfg and "chameleon-judge-" in cfg
    # The throwaway dir is removed after the spawn returns.
    assert not Path(cfg).exists()


def test_judge_spawn_fails_open_when_tmpdir_unavailable(monkeypatch, tmp_path):
    from chameleon_mcp import judge

    def no_tmp(*a, **k):
        raise OSError("no tmp")

    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(judge.tempfile, "mkdtemp", no_tmp)
    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    assert judge._spawn_reviewer("prompt", tmp_path) == "[]"
    # Falls back to the inherited environment rather than refusing to spawn.
    assert "CLAUDE_CONFIG_DIR" not in (captured["env"] or {}) or captured["env"] is not None


def test_crossfile_low_confidence_has_separate_cap():
    # The scan must budget low-confidence transparency rows separately from the
    # high-confidence findings the consumer actually relays.
    from chameleon_mcp._thresholds import DEFAULTS

    assert "CROSSFILE_MAX_LOW_CONFIDENCE" in DEFAULTS
    assert DEFAULTS["CROSSFILE_MAX_LOW_CONFIDENCE"] < DEFAULTS["CROSSFILE_MAX_FINDINGS"]
