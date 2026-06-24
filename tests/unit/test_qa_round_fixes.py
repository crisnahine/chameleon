"""Fixes for the three defects left open by the real-use QA round.

1. The enforce-promotion docs must spell out the two-step flow (edit
   config.json, then re-trust): config.json is trust-hashed, so the edit alone
   flips the profile to stale and silently disables enforcement.
2. The correctness-judge spawn must stay authenticated: it inherits the real
   config dir (an empty throwaway CLAUDE_CONFIG_DIR strips OAuth/subscription
   auth, so the judge silently never fired) and sets CHAMELEON_DISABLE=1 so
   chameleon's own hooks don't recurse into another judge spawn.
3. get_crossfile_context must cap low-confidence (open-set/barrel) rows
   separately so they cannot crowd genuine high-confidence existence breaks out
   of the response.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_trust_skill_documents_single_step_promotion_by_default():
    # Trust persists across changes, so promoting enforcement is a single config
    # edit by default; the two-step (re-trust) path is documented as kill-switch-only.
    text = _read("skills/chameleon-trust/SKILL.md")
    assert "single edit" in text
    assert "CHAMELEON_TRUST_REVALIDATE=1" in text


def test_status_skill_documents_single_step_promotion_by_default():
    text = _read("skills/chameleon-status/SKILL.md")
    assert "single edit" in text
    assert "CHAMELEON_TRUST_REVALIDATE=1" in text


@pytest.mark.real_judge_spawn
def test_judge_spawn_preserves_auth_and_disables_chameleon(monkeypatch, tmp_path):
    # Corrected contract: the judge must NOT spawn into an empty throwaway
    # CLAUDE_CONFIG_DIR (that stripped OAuth/subscription auth -> "Not logged in"
    # -> the judge silently never fired). It inherits the real config dir for
    # auth and sets CHAMELEON_DISABLE=1 so chameleon's own hooks don't recurse.
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
    assert env.get("CHAMELEON_DISABLE") == "1"
    # Must not point at the empty throwaway dir that broke auth.
    assert "chameleon-judge-" not in (env.get("CLAUDE_CONFIG_DIR") or "")


@pytest.mark.real_judge_spawn
def test_judge_spawn_fails_open_on_subprocess_error(monkeypatch, tmp_path):
    from chameleon_mcp import judge

    def boom(*a, **k):
        raise OSError("spawn failed")

    monkeypatch.setattr(judge.subprocess, "run", boom)
    # A spawn error must fail open to no findings, never raise.
    assert judge._spawn_reviewer("prompt", tmp_path) is None


def test_crossfile_low_confidence_has_separate_cap():
    # The scan must budget low-confidence transparency rows separately from the
    # high-confidence findings the consumer actually relays.
    from chameleon_mcp._thresholds import DEFAULTS

    assert "CROSSFILE_MAX_LOW_CONFIDENCE" in DEFAULTS
    assert DEFAULTS["CROSSFILE_MAX_LOW_CONFIDENCE"] < DEFAULTS["CROSSFILE_MAX_FINDINGS"]


def test_stop_backstop_wrapper_timeout_exceeds_judge_budget():
    # The judge spawn runs inside the stop-backstop python process; a wrapper
    # cap shorter than the judge wall-clock budget SIGKILLs the review mid-run
    # and leaks its throwaway config dir.
    from chameleon_mcp._thresholds import DEFAULTS

    text = _read("hooks/stop-backstop")
    import re

    m = re.search(r'\$\{TIMEOUT_BIN:\+"\$\{TIMEOUT_BIN\}" (\d+)\}', text)
    assert m, "stop-backstop must keep its timeout(1) cap"
    assert int(m.group(1)) > DEFAULTS["CORRECTNESS_JUDGE_TIMEOUT_SECONDS"]


def test_stale_judge_dirs_swept_on_next_spawn(monkeypatch, tmp_path):
    import os as _os
    import time as _time

    from chameleon_mcp import judge

    monkeypatch.setattr(judge.tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / "chameleon-judge-stale"
    stale.mkdir()
    _os.utime(stale, (_time.time() - 7200, _time.time() - 7200))
    fresh = tmp_path / "chameleon-judge-fresh"
    fresh.mkdir()
    judge._sweep_stale_judge_dirs()
    assert not stale.exists(), "an hour-old leaked judge dir must be swept"
    assert fresh.exists(), "a recent dir may belong to a live spawn; keep it"
