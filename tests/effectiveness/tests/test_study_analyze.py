"""Tests for the dogfood-study two-sample bootstrap (tests/study_analyze.py).

The verdict of the real-world effectiveness study rests on this CI, so its
direction and null-handling are pinned here. Deterministic (fixed seed).
"""

from __future__ import annotations

from tests.study_analyze import _pooled_rate, _verdict, two_sample_boot


def test_pooled_rate_weights_by_denominator():
    # 1 violation over 1 file + 9 over 9 files => 10/10 => 100 per 100
    assert _pooled_rate([(1, 1), (9, 9)]) == 100.0
    assert _pooled_rate([]) is None
    assert _pooled_rate([(0, 5)]) == 0.0


def test_identical_arms_straddle_zero():
    units = [(2, 10)] * 40
    r = two_sample_boot(units, list(units), resamples=2000)
    assert r["diff"] == 0.0
    assert r["lo"] <= 0 <= r["hi"]
    assert r["supported"] is False
    assert _verdict(r).startswith("NULL")


def test_clear_separation_excludes_zero():
    # pre arm heavily violating, post arm clean => (pre - post) CI > 0 => supported
    pre = [(8, 10)] * 50
    post = [(1, 10)] * 50
    r = two_sample_boot(pre, post, resamples=2000)
    assert r["diff"] > 0
    assert r["lo"] > 0
    assert r["supported"] is True
    assert _verdict(r).startswith("SUPPORTED")


def test_reversed_direction_flags_reversed():
    pre = [(1, 10)] * 50
    post = [(8, 10)] * 50
    r = two_sample_boot(pre, post, resamples=2000)
    assert r["hi"] < 0
    assert _verdict(r).startswith("REVERSED")


def test_empty_arm_returns_none():
    r = two_sample_boot([], [(1, 10)])
    assert r["diff"] is None
    assert _verdict(r) == "NO DATA"


def test_seed_is_deterministic():
    a = [(3, 10)] * 30
    b = [(1, 10)] * 30
    r1 = two_sample_boot(a, b, resamples=1500)
    r2 = two_sample_boot(a, b, resamples=1500)
    assert (r1["lo"], r1["hi"]) == (r2["lo"], r2["hi"])
