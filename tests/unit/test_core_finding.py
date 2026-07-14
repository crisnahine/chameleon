"""Canonical Finding value type: vocabulary, normalization, match_key stability."""

from __future__ import annotations

import dataclasses

import pytest

from chameleon_mcp.core.finding import (
    KINDS,
    SEVERITIES,
    STATUSES,
    Finding,
    compute_match_key,
    normalize_severity,
)


def _mk(**over):
    base = dict(
        id="f-1",
        kind="correctness",
        severity="high",
        confidence=0.8,
        file="src/a.py",
        span=(10, 14),
        claim="retry count is 2 not 3",
        evidence="src/a.py:12 hardcodes 2",
        excerpt_sha="ab" * 32,
        excerpt="retries = 2",
        source_lens="correctness",
        intent_tokens=("fix retries",),
        status="pending",
        created_at="2026-07-14T00:00:00Z",
    )
    base.update(over)
    return Finding(**base)


def test_finding_is_frozen_and_replace_derives():
    f = _mk()
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.severity = "low"  # type: ignore[misc]
    g = dataclasses.replace(f, status="delivered")
    assert g.status == "delivered" and f.status == "pending"


def test_match_key_is_stable_and_normalized():
    a = compute_match_key("Retry count is 2  not 3.", "src/a.py", "correctness")
    b = compute_match_key("retry count is 2 not 3", "src/a.py", "correctness")
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
    assert a != compute_match_key("retry count is 2 not 3", "src/b.py", "correctness")
    assert a != compute_match_key("retry count is 2 not 3", "src/a.py", "idiom")


def test_finding_autofills_match_key():
    f = _mk()
    assert f.match_key == compute_match_key(f.claim, f.file, f.kind)


def test_normalize_severity_single_source():
    assert normalize_severity("HIGH") == "high"
    assert normalize_severity("blocker") == "blocker"
    assert normalize_severity("critical") == "blocker"
    assert normalize_severity("warning") == "medium"
    assert normalize_severity(None) == "medium"
    assert normalize_severity("nonsense") == "medium"


def test_vocab_constants():
    assert "idiom" in KINDS and "intent" in KINDS
    assert SEVERITIES == ("blocker", "high", "medium", "low")
    assert {"pending", "delivered", "addressed", "resurfaced", "shelved", "expired"} <= set(
        STATUSES
    )


def test_invalid_vocab_rejected():
    with pytest.raises(ValueError):
        _mk(kind="vibes")
    with pytest.raises(ValueError):
        _mk(status="done")
    with pytest.raises(ValueError):
        _mk(severity="urgent")


def test_round_trip_dict():
    f = _mk()
    d = f.to_dict()
    assert Finding.from_dict(d) == f
    # Unknown keys tolerated (forward compat), missing optionals defaulted.
    d2 = dict(d)
    d2["future_field"] = 1
    assert Finding.from_dict(d2) == f
