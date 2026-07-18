"""Class D: the Secret Keyword placeholder filter must judge the EXACT flagged
token, not require every value on the line to be a placeholder. A scan-time
boolean carries the verdict without ever storing the raw value.
"""

from __future__ import annotations

import pytest


def test_leaf_module_predicate_parity():
    # The extracted predicate matches the historical behavior.
    from chameleon_mcp.secret_placeholder import secret_value_is_placeholder as isp

    assert isp("test") is True
    assert isp("") is True
    assert isp("your-secret-key") is True  # placeholder-shaped, low entropy
    assert isp("changethis") is False  # weak-but-plausible: kept
    assert isp("password123") is False
    assert isp("hunter2xJ9zQ_real_LongToken") is False  # high entropy real-looking


def test_keyword_hit_uses_per_value_boolean():
    # The whole point: a hit whose OWN flagged value is a placeholder is dropped,
    # even when a co-located non-secret arg (username="eric") sits on the line.
    from chameleon_mcp.lint_engine import _keyword_hit_is_placeholder

    line = 'login(username="eric", password="test")'
    hit_ph = {"type": "Secret Keyword", "line_number": 1, "value_placeholder": True}
    assert _keyword_hit_is_placeholder(hit_ph, [line]) is True  # NEW: dropped

    # a real flagged value -> not a placeholder -> kept
    hit_real = {"type": "Secret Keyword", "line_number": 1, "value_placeholder": False}
    assert _keyword_hit_is_placeholder(hit_real, [line]) is False


def test_concat_fold_short_circuit_wins_over_boolean():
    # A reassembled ("te"+"st") value must NEVER be suppressed, even if the folded
    # token looks like a placeholder -- the concat guard runs before the boolean.
    from chameleon_mcp.lint_engine import _keyword_hit_is_placeholder

    hit = {
        "type": "Secret Keyword",
        "line_number": 1,
        "concat_folded": True,
        "value_placeholder": True,
    }
    assert _keyword_hit_is_placeholder(hit, ['secret = "te" + "st"']) is False


def test_boolean_absent_falls_back_to_all_line_values():
    # An older hit with no value_placeholder key uses the legacy all-values check.
    from chameleon_mcp.lint_engine import _keyword_hit_is_placeholder

    all_ph = {"type": "Secret Keyword", "line_number": 1}
    assert _keyword_hit_is_placeholder(all_ph, ['password = "test"']) is True
    mixed = {"type": "Secret Keyword", "line_number": 1}
    assert _keyword_hit_is_placeholder(mixed, ['login(username="eric", password="test")']) is False


def test_scan_sets_value_placeholder():
    pytest.importorskip("detect_secrets")
    from chameleon_mcp.profile.secret_scanner import _try_detect_secrets

    hits = _try_detect_secrets('api_login(username="eric", password="test")\n') or []
    kw = [h for h in hits if h.get("type") == "Secret Keyword"]
    assert kw, 'expected a Secret Keyword hit on password="test"'
    assert all(h.get("value_placeholder") is True for h in kw)

    real = _try_detect_secrets('password = "hunter2xJ9zQ_real_LongToken_88"\n') or []
    kwr = [h for h in real if h.get("type") == "Secret Keyword"]
    if kwr:  # detect-secrets flags it; the boolean must say NOT a placeholder
        assert all(h.get("value_placeholder") is False for h in kwr)


def test_co_located_real_secret_keeps_hit_despite_placeholder_flag():
    # SECURITY: detect-secrets folds a multi-assignment line into ONE hit (the
    # placeholder token). A real secret under another key on the same line must
    # keep the hit -- the co-located guard forces keep when anything looks secretish.
    from chameleon_mcp.lint_engine import _keyword_hit_is_placeholder

    tok = "hX9zQ2mVp8Lk3RnT7wYbC4dFgJ6sA1eZ" * 8  # 256 chars, high entropy
    hit = {"type": "Secret Keyword", "line_number": 1, "value_placeholder": True}
    assert _keyword_hit_is_placeholder(hit, [f'password = "test"; token = "{tok}"']) is False
    # but a username co-located with a placeholder is NOT secretish -> still drops
    assert _keyword_hit_is_placeholder(hit, ['login(username="eric", password="test")']) is True


def test_colocated_weak_secret_under_secret_key_is_kept():
    # SECURITY REGRESSION GUARD: a WEAK-but-real secret (short, low-entropy) under a
    # secret-ish key, co-located with a placeholder on the same folded line, must NOT
    # be dropped. `value_looks_secretish` alone misses it (s3cr3t is <20 chars and
    # low entropy), so the guard must also key off the ASSIGNMENT TARGET.
    from chameleon_mcp.lint_engine import _keyword_hit_is_placeholder

    hit = {"type": "Secret Keyword", "line_number": 1, "value_placeholder": True}
    # token = a secret key; s3cr3t is not in the placeholder set -> real -> keep
    assert _keyword_hit_is_placeholder(hit, ['password="test", token="s3cr3t"']) is False
    # dict-string-key form: db_password holds a weak real secret -> keep
    assert (
        _keyword_hit_is_placeholder(
            hit, ['config = {"api_key": "your-api-key", "db_password": "hunter2"}']
        )
        is False
    )
    # a non-secret key (username) with a non-placeholder value is NOT a secret -> drop
    assert _keyword_hit_is_placeholder(hit, ['login(username="eric", password="test")']) is True
    # a pure-placeholder line under a secret key still drops
    assert _keyword_hit_is_placeholder(hit, ['password = "test"']) is True


def test_line_has_colocated_real_secret_helper():
    from chameleon_mcp.secret_placeholder import line_has_colocated_real_secret

    # weak real secret under a secret key -> True
    assert line_has_colocated_real_secret('password="test", token="s3cr3t"') is True
    assert line_has_colocated_real_secret('db_password = "hunter2"') is True
    # high-entropy value under ANY key -> True
    assert line_has_colocated_real_secret('data = "hX9zQ2mVp8Lk3RnT7wYbC4dFgJ6"') is True
    # non-secret key + short low-entropy value -> False (username/email/name)
    assert line_has_colocated_real_secret('username = "eric"') is False
    assert line_has_colocated_real_secret('name = "bob", email = "b@x.io"') is False
    # only placeholders -> False
    assert line_has_colocated_real_secret('password = "test", api_key = "your-api-key"') is False
    assert line_has_colocated_real_secret("") is False
