"""The review skills' numeric tunables must be registered DEFAULTS so the
CHAMELEON_<KEY> override mechanism applies. Model + boolean kill switches are
deliberately NOT here (DEFAULTS is int|float only); they are os.environ reads."""

from __future__ import annotations

from chameleon_mcp._thresholds import DEFAULTS, threshold_int


def test_review_thresholds_registered_with_defaults():
    assert DEFAULTS["REVIEW_FANOUT_FILES"] == 8
    assert DEFAULTS["REVIEW_FANOUT_LINES"] == 400
    assert DEFAULTS["REFUTER_MAX_SPAWNS_PER_INVOCATION"] == 8
    assert DEFAULTS["REFUTER_TIMEOUT_SECONDS"] == 45


def test_review_thresholds_are_int():
    for k in (
        "REVIEW_FANOUT_FILES",
        "REVIEW_FANOUT_LINES",
        "REFUTER_MAX_SPAWNS_PER_INVOCATION",
        "REFUTER_TIMEOUT_SECONDS",
    ):
        assert isinstance(DEFAULTS[k], int), k
        assert threshold_int(k) == DEFAULTS[k]


def test_env_override(monkeypatch):
    monkeypatch.setenv("CHAMELEON_REVIEW_FANOUT_FILES", "20")
    assert threshold_int("REVIEW_FANOUT_FILES") == 20
