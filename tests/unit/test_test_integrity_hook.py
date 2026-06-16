"""Tests for the Stop-hook test-integrity advisory wiring (_test_integrity_advisory_lines).

The deterministic assess/format logic is covered in test_test_integrity.py; here
the gating (config flag, mode), the diff sourcing, and the per-session digest
dedup are exercised against the hook helper.
"""

from __future__ import annotations

from types import SimpleNamespace

import chameleon_mcp.test_integrity as ti
from chameleon_mcp import hook_helper

_WEAKENING_DIFF = (
    "diff --git a/app/models/widget.rb b/app/models/widget.rb\n"
    "--- a/app/models/widget.rb\n"
    "+++ b/app/models/widget.rb\n"
    "-  def compute; old; end\n"
    "+  def compute; new; end\n"
    "diff --git a/spec/models/widget_spec.rb b/spec/models/widget_spec.rb\n"
    "--- a/spec/models/widget_spec.rb\n"
    "+++ b/spec/models/widget_spec.rb\n"
    '+  skip "flaky for now"\n'
)


def _cfg(*, flag=True, mode="shadow"):
    return SimpleNamespace(test_integrity_review=flag, mode=mode)


def _state(files):
    return SimpleNamespace(files=files)


def _call(tmp_path, monkeypatch, *, cfg, files, diff=_WEAKENING_DIFF):
    monkeypatch.setattr(ti, "build_turn_diff", lambda root, edited: diff)
    return hook_helper._test_integrity_advisory_lines(
        repo_root=tmp_path,
        repo_id="rid",
        session_id="sid",
        state=_state(files),
        cfg=cfg,
        repo_data=tmp_path,
    )


def test_emits_when_source_changed_and_tests_weakened(tmp_path, monkeypatch):
    lines = _call(
        tmp_path,
        monkeypatch,
        cfg=_cfg(),
        files=["app/models/widget.rb", "spec/models/widget_spec.rb"],
    )
    assert lines
    assert any("test integrity" in ln.lower() for ln in lines)


def test_flag_off_silent(tmp_path, monkeypatch):
    lines = _call(
        tmp_path,
        monkeypatch,
        cfg=_cfg(flag=False),
        files=["app/models/widget.rb", "spec/models/widget_spec.rb"],
    )
    assert lines == []


def test_mode_off_silent(tmp_path, monkeypatch):
    lines = _call(
        tmp_path,
        monkeypatch,
        cfg=_cfg(mode="off"),
        files=["app/models/widget.rb", "spec/models/widget_spec.rb"],
    )
    assert lines == []


def test_test_only_change_silent(tmp_path, monkeypatch):
    # No live source in the turn's files -> stays quiet even with a weakening diff.
    lines = _call(tmp_path, monkeypatch, cfg=_cfg(), files=["spec/models/widget_spec.rb"])
    assert lines == []


def test_dedup_same_digest_second_turn_silent(tmp_path, monkeypatch):
    files = ["app/models/widget.rb", "spec/models/widget_spec.rb"]
    first = _call(tmp_path, monkeypatch, cfg=_cfg(), files=files)
    assert first
    second = _call(tmp_path, monkeypatch, cfg=_cfg(), files=files)
    assert second == []
