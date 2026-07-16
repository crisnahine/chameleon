"""TurnBudget: entry-anchored deadline + carried token ceiling."""

from __future__ import annotations

from chameleon_mcp.core.budget import TurnBudget, approx_tokens


def test_approx_tokens_chars_over_four():
    assert approx_tokens("") == 0
    assert approx_tokens("abcd") == 1
    assert approx_tokens("a" * 401) == 101  # ceil


def test_deadline_anchored_at_construction(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("chameleon_mcp.core.budget.time.monotonic", lambda: now[0])
    b = TurnBudget.for_hook(total_seconds=10.0, token_ceiling=100)
    assert b.remaining_seconds() == 10.0
    now[0] = 1004.0
    assert b.remaining_seconds() == 6.0
    now[0] = 1011.0
    assert b.remaining_seconds() == 0.0


def test_token_ceiling_is_carried_and_floored():
    assert TurnBudget.for_hook(total_seconds=60.0, token_ceiling=10).tokens_remaining() == 10
    assert TurnBudget.for_hook(total_seconds=60.0, token_ceiling=-5).tokens_remaining() == 0
