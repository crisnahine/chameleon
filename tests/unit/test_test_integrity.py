"""Tests for the turn-end test-integrity advisory (deterministic, zero-LLM).

Surfaces the test-weakening signals the auto-pass router already computes
(autopass.scan_diff_signals) at turn end, when the turn ALSO changed live source
-- the "why did you skip/delete this test?" reviewer-comment class -- so the
author fixes it before the PR. No model spawn; fails open.
"""

from __future__ import annotations

from chameleon_mcp import test_integrity as ti

_SOURCE_BLOCK = (
    "diff --git a/app/models/widget.rb b/app/models/widget.rb\n"
    "--- a/app/models/widget.rb\n"
    "+++ b/app/models/widget.rb\n"
    "-  def compute; old; end\n"
    "+  def compute; new; end\n"
)
_SPEC_ADDS_SKIP = (
    "diff --git a/spec/models/widget_spec.rb b/spec/models/widget_spec.rb\n"
    "--- a/spec/models/widget_spec.rb\n"
    "+++ b/spec/models/widget_spec.rb\n"
    '+  skip "flaky for now"\n'
)
_SPEC_ADDS_ASSERTION = (
    "diff --git a/spec/models/widget_spec.rb b/spec/models/widget_spec.rb\n"
    "--- a/spec/models/widget_spec.rb\n"
    "+++ b/spec/models/widget_spec.rb\n"
    "+  expect(thing).to eq(1)\n"
)
_SRC = "app/models/widget.rb"
_SPEC = "spec/models/widget_spec.rb"


def test_assess_flags_skip_with_source_change():
    a = ti.assess_test_weakening(_SOURCE_BLOCK + _SPEC_ADDS_SKIP, [_SRC, _SPEC])
    assert a is not None
    assert any("skip" in r for r in a["reasons"])


def test_assess_ignores_test_only_change():
    # Weakening but NO live source change -> pure test edit, stays quiet.
    a = ti.assess_test_weakening(_SPEC_ADDS_SKIP, [_SPEC])
    assert a is None


def test_assess_none_when_no_weakening():
    # Source change + a spec that ADDS an assertion -> no weakening marker.
    a = ti.assess_test_weakening(_SOURCE_BLOCK + _SPEC_ADDS_ASSERTION, [_SRC, _SPEC])
    assert a is None


def test_assess_empty_diff_is_none():
    assert ti.assess_test_weakening("", [_SRC]) is None


def test_format_produces_advisory_lines():
    a = ti.assess_test_weakening(_SOURCE_BLOCK + _SPEC_ADDS_SKIP, [_SRC, _SPEC])
    lines = ti.format_test_integrity_advisory(a)
    assert lines
    assert any("test integrity" in ln.lower() for ln in lines)
    assert any("skip" in ln for ln in lines)


def test_format_empty_assessment_is_empty():
    assert ti.format_test_integrity_advisory(None) == []
