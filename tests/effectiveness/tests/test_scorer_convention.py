"""Convention scorer against canned chameleon envelopes."""

from __future__ import annotations

from pathlib import Path

from tests.effectiveness.scorers import convention
from tests.effectiveness.tests.test_scorer_base import _ctx

PATTERN_OK = {
    "api_version": "1",
    "data": {
        "repo": {"id": "0" * 64, "profile_status": "ok", "trust_state": "trusted"},
        "archetype": {"archetype": "react-component", "match_quality": "exact"},
        "rules": [],
        "idioms": "",
    },
}

PATTERN_NONE = {
    "api_version": "1",
    "data": {
        "repo": {"id": "0" * 64, "profile_status": "ok", "trust_state": "trusted"},
        "archetype": {"archetype": None, "match_quality": "none"},
        "rules": [],
        "idioms": "",
    },
}

LINT_TWO_VIOLATIONS = {
    "api_version": "1",
    "data": {
        "stub": False,
        "violations": [
            {"rule": "export-shape", "severity": "warn", "message": "default export"},
            {"rule": "naming", "severity": "warn", "message": "snake_case component"},
        ],
        "canonical_confidence": 0.9,
        "unparseable_regions": [],
        "content_size": 100,
    },
}

LINT_STUB = {
    "api_version": "1",
    "data": {"stub": True, "stub_reason": "no profile", "violations": []},
}


def _prep(tmp_path: Path, monkeypatch, pattern_resp, lint_resp):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Widget.tsx").write_text("export default 1;\n")
    monkeypatch.setattr(convention, "_pattern_context", lambda path: pattern_resp)
    monkeypatch.setattr(convention, "_lint", lambda **kw: lint_resp)
    ctx = _ctx(tmp_path)
    ctx.changed_files = ["src/Widget.tsx"]
    return ctx


def test_counts_violations_per_changed_file(tmp_path, monkeypatch):
    ctx = _prep(tmp_path, monkeypatch, PATTERN_OK, LINT_TWO_VIOLATIONS)
    out = convention.score(ctx)
    assert out["violations"] == 2
    assert out["files_scored"] == 1
    assert out["files_unresolved"] == 0


def test_unresolved_archetype_counted_not_fatal(tmp_path, monkeypatch):
    ctx = _prep(tmp_path, monkeypatch, PATTERN_NONE, LINT_TWO_VIOLATIONS)
    out = convention.score(ctx)
    assert set(out) == {"unscored"}  # the ONLY changed file resolved to nothing


def test_stub_lint_counts_as_unresolved(tmp_path, monkeypatch):
    ctx = _prep(tmp_path, monkeypatch, PATTERN_OK, LINT_STUB)
    out = convention.score(ctx)
    assert set(out) == {"unscored"}


def test_no_lintable_changes_scores_zero(tmp_path, monkeypatch):
    ctx = _prep(tmp_path, monkeypatch, PATTERN_OK, LINT_TWO_VIOLATIONS)
    ctx.changed_files = ["README.md"]
    out = convention.score(ctx)
    assert out["violations"] == 0
    assert out["files_scored"] == 0


def test_rubric_metrics_merged_with_prefix(tmp_path, monkeypatch):
    ctx = _prep(tmp_path, monkeypatch, PATTERN_OK, LINT_TWO_VIOLATIONS)
    ctx.pack.rubrics[ctx.task.task_id] = lambda worktree: {"placed_in_components": True}
    out = convention.score(ctx)
    assert out["rubric_placed_in_components"] is True
