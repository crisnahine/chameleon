"""TurnBudget: entry-anchored deadline + token ceiling with spend accounting."""

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
    assert not b.expired()
    now[0] = 1011.0
    assert b.remaining_seconds() == 0.0
    assert b.expired()


def test_token_charging_and_refusal():
    b = TurnBudget.for_hook(total_seconds=60.0, token_ceiling=10)
    assert b.would_fit("a" * 36)  # 9 tokens
    assert b.charge_tokens("a" * 36)  # spent 9
    assert b.tokens_remaining() == 1
    assert not b.would_fit("a" * 8)  # 2 tokens > 1 left
    assert not b.charge_tokens("a" * 8)  # refused, nothing spent
    assert b.tokens_remaining() == 1
    assert b.charge_tokens("abcd")  # exactly 1 token fits
    assert b.tokens_remaining() == 0
