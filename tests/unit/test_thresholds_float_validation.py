"""Regression tests for threshold() float-override validation.

The float branch of ``threshold()`` historically did a bare ``float(raw)`` with
no finiteness or range check. An env override of ``nan`` returned NaN (every
drift comparison against NaN is False, so the banner silently never fired),
``inf`` returned infinity, and negatives passed straight through. These tests
pin that a non-finite or negative float override falls back to the documented
default while a valid finite, non-negative float still applies.
"""

from __future__ import annotations

import math

import pytest

from chameleon_mcp import _thresholds


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """monkeypatch.setenv auto-restores env on teardown; nothing else to clean."""
    yield


class TestFloatOverrideValidation:
    def test_nan_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "nan")
        got = _thresholds.threshold("DRIFT_BANNER_THRESHOLD")
        assert got == 0.4
        assert not math.isnan(got)

    def test_inf_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "inf")
        got = _thresholds.threshold("DRIFT_BANNER_THRESHOLD")
        assert got == 0.4

    def test_negative_inf_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "-inf")
        assert _thresholds.threshold("DRIFT_BANNER_THRESHOLD") == 0.4

    def test_negative_float_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "-0.2")
        assert _thresholds.threshold("DRIFT_BANNER_THRESHOLD") == 0.4

    def test_valid_float_still_passes_through(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "0.85")
        got = _thresholds.threshold("DRIFT_BANNER_THRESHOLD")
        assert got == 0.85
        assert type(got) is float

    def test_zero_float_is_accepted(self, monkeypatch):
        # Zero is finite and non-negative; it must pass through, not fall back.
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "0")
        assert _thresholds.threshold("DRIFT_BANNER_THRESHOLD") == 0.0
