"""Reviewer model ladder (#6): route/severity-keyed reviewer models.

Pins the resolution helpers that escalate a high-risk / high-severity review to
a stronger model while keeping the ladder raise-only (a garbage model never
spawns, so the reviewer is never silently disabled) and kill-switchable.
"""

from __future__ import annotations

import pytest

from chameleon_mcp import judge
from chameleon_mcp.refuter import _refuter_model_for


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in (
        "CHAMELEON_JUDGE_MODEL",
        "CHAMELEON_JUDGE_MODEL_HIGH",
        "CHAMELEON_JUDGE_TIERING",
        "CHAMELEON_REFUTER_MODEL_HIGH",
    ):
        monkeypatch.delenv(k, raising=False)


# --- _valid_model ------------------------------------------------------------


@pytest.mark.parametrize("m", ["opus", "sonnet", "haiku", "fable", "claude-opus-4-8", "SONNET"])
def test_valid_model_accepts_known(m):
    assert judge._valid_model(m) is True


@pytest.mark.parametrize(
    "m",
    # Total garbage AND plausible token-containing typos of a real id: a
    # substring match ("opus" in "opus-latest") used to pass and fail-open the
    # spawn, so the exact-token guard must reject these.
    [None, "", "   ", "rm -rf", "gpt-4", "bogus", 5, "opus-latest", "sonnet-preview-bogus"],
)
def test_valid_model_rejects_unknown(m):
    assert judge._valid_model(m) is False


# --- judge_model_for_route ---------------------------------------------------


@pytest.fixture
def _detached(monkeypatch):
    # The escalation only fires on the detached async path (the sync 45s/55s
    # budget can't afford the slower model). Simulate the detached child.
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", True)


def test_high_routes_escalate_to_opus_when_detached(_detached):
    assert judge.judge_model_for_route("risk_high") == "opus"
    assert judge.judge_model_for_route("intent_forced") == "opus"


def test_sync_path_does_not_escalate_high_route():
    # NOT detached (the default sync Stop path): a high route keeps the base
    # model, because opus under the 45s sync budget would time out and lose
    # findings on exactly the high-risk turns -- a coverage regression.
    assert judge._RUNNING_DETACHED is False
    assert judge.judge_model_for_route("risk_high") == "sonnet"
    assert judge.judge_model_for_route("intent_forced") == "sonnet"


def test_low_routes_keep_sonnet(_detached):
    for reason in ("risk_elevated", "first_low_risk", None, "anything_else"):
        assert judge.judge_model_for_route(reason) == "sonnet"


def test_tiering_kill_switch_flattens_high_route(monkeypatch, _detached):
    monkeypatch.setenv("CHAMELEON_JUDGE_TIERING", "0")
    assert judge.judge_model_for_route("risk_high") == "sonnet"


def test_custom_high_model_used(monkeypatch, _detached):
    monkeypatch.setenv("CHAMELEON_JUDGE_MODEL_HIGH", "fable")
    assert judge.judge_model_for_route("risk_high") == "fable"


def test_garbage_high_model_falls_back_to_base_never_spawned(monkeypatch, _detached):
    # Raise-only: an unrecognized HIGH model must fall back to the valid base,
    # never be spawned (a garbage --model fail-opens the judge to zero findings).
    monkeypatch.setenv("CHAMELEON_JUDGE_MODEL_HIGH", "rm -rf /")
    assert judge.judge_model_for_route("risk_high") == "sonnet"


def test_garbage_base_model_falls_back_to_sonnet(monkeypatch):
    monkeypatch.setenv("CHAMELEON_JUDGE_MODEL", "bogus")
    assert judge.judge_model_for_route("first_low_risk") == "sonnet"


def test_custom_base_model_used_on_low_route(monkeypatch):
    monkeypatch.setenv("CHAMELEON_JUDGE_MODEL", "claude-opus-4-8")
    assert judge.judge_model_for_route("first_low_risk") == "claude-opus-4-8"


# --- refuter severity split --------------------------------------------------


def test_refuter_escalates_block_severity(monkeypatch):
    monkeypatch.setenv("CHAMELEON_REFUTER_MODEL_HIGH", "opus")
    assert _refuter_model_for({"severity": "BLOCK"}, "sonnet") == "opus"


def test_refuter_keeps_base_for_nits(monkeypatch):
    monkeypatch.setenv("CHAMELEON_REFUTER_MODEL_HIGH", "opus")
    assert _refuter_model_for({"severity": "NIT"}, "sonnet") == "sonnet"
    assert _refuter_model_for({"severity": "FIX"}, "sonnet") == "sonnet"


def test_refuter_high_defaults_to_opus_when_unset():
    # Default-ON: a BLOCK finding escalates to opus even without the env set.
    assert _refuter_model_for({"severity": "block"}, "sonnet") == "opus"


def test_refuter_missing_severity_keeps_base():
    assert _refuter_model_for({}, "sonnet") == "sonnet"
    assert _refuter_model_for({"severity": None}, "sonnet") == "sonnet"


def test_refuter_tiering_kill_switch(monkeypatch):
    monkeypatch.setenv("CHAMELEON_JUDGE_TIERING", "0")
    monkeypatch.setenv("CHAMELEON_REFUTER_MODEL_HIGH", "opus")
    assert _refuter_model_for({"severity": "BLOCK"}, "sonnet") == "sonnet"


def test_refuter_garbage_high_falls_back(monkeypatch):
    monkeypatch.setenv("CHAMELEON_REFUTER_MODEL_HIGH", "not-a-model")
    assert _refuter_model_for({"severity": "BLOCK"}, "sonnet") == "sonnet"


# --- regression fixes from adversarial review --------------------------------


def test_dup_model_preserves_judge_model_when_unset(monkeypatch):
    # An UNSET CHAMELEON_DUP_MODEL must ride CHAMELEON_JUDGE_MODEL (prior
    # behavior), NOT silently drop to sonnet. Regression: the dup spawn used to
    # pass no model and inherit the judge default.
    from chameleon_mcp.duplication_review import _dup_model

    monkeypatch.delenv("CHAMELEON_DUP_MODEL", raising=False)
    monkeypatch.setenv("CHAMELEON_JUDGE_MODEL", "opus")
    assert _dup_model() == "opus"


def test_dup_model_own_knob_wins(monkeypatch):
    from chameleon_mcp.duplication_review import _dup_model

    monkeypatch.setenv("CHAMELEON_JUDGE_MODEL", "opus")
    monkeypatch.setenv("CHAMELEON_DUP_MODEL", "sonnet")
    assert _dup_model() == "sonnet"


def test_dup_model_defaults_sonnet_and_hardens_garbage(monkeypatch):
    from chameleon_mcp.duplication_review import _dup_model

    monkeypatch.delenv("CHAMELEON_DUP_MODEL", raising=False)
    monkeypatch.delenv("CHAMELEON_JUDGE_MODEL", raising=False)
    assert _dup_model() == "sonnet"
    monkeypatch.setenv("CHAMELEON_DUP_MODEL", "rm -rf")
    assert _dup_model() == "sonnet"


def test_refuter_model_for_non_dict_finding_never_raises():
    # Runs outside run_one's try/except at submit time, so a non-dict finding
    # must return the base model rather than raise and collapse the whole batch.
    for bad in ("a string", 5, ["list"], None):
        assert _refuter_model_for(bad, "sonnet") == "sonnet"
