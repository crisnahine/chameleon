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
    # No baseline version resolvable (tmp_path is not a git repo) -> all current
    # violations are counted as introduced.
    assert out["violations"] == 2
    assert out["files_scored"] == 1
    assert out["files_unresolved"] == 0


_V_EXPORT = {"rule": "export-shape", "severity": "warn", "message": "default export"}
_V_NAMING = {"rule": "naming", "severity": "warn", "message": "snake_case component"}
_V_NEW = {"rule": "style-rule-violation", "severity": "info", "message": "line 9 too long"}


def _lint_with(rows):
    return {"api_version": "1", "data": {"stub": False, "violations": rows}}


def test_preexisting_violations_not_counted(tmp_path, monkeypatch):
    # The baseline file already had both violations; the change added none -> the
    # net introduced count is 0, even though the absolute count is 2.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Widget.tsx").write_text("CURRENT\n")
    monkeypatch.setattr(convention, "_pattern_context", lambda path: PATTERN_OK)
    monkeypatch.setattr(convention, "_baseline_content", lambda wt, sha, rel: "BASELINE")

    def fake_lint(**kw):
        # Same two violations in both the baseline and current versions.
        return _lint_with([_V_EXPORT, _V_NAMING])

    monkeypatch.setattr(convention, "_lint", fake_lint)
    ctx = _ctx(tmp_path)
    ctx.changed_files = ["src/Widget.tsx"]
    out = convention.score(ctx)
    assert out["violations"] == 0
    assert out["violations_baseline"] == 2


def test_only_net_new_violations_counted(tmp_path, monkeypatch):
    # Baseline had 2; current has those 2 plus 1 new -> net introduced is 1.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Widget.tsx").write_text("CURRENT\n")
    monkeypatch.setattr(convention, "_pattern_context", lambda path: PATTERN_OK)
    monkeypatch.setattr(convention, "_baseline_content", lambda wt, sha, rel: "BASELINE")

    def fake_lint(**kw):
        if kw.get("content") == "BASELINE":
            return _lint_with([_V_EXPORT, _V_NAMING])
        return _lint_with([_V_EXPORT, _V_NAMING, _V_NEW])

    monkeypatch.setattr(convention, "_lint", fake_lint)
    ctx = _ctx(tmp_path)
    ctx.changed_files = ["src/Widget.tsx"]
    out = convention.score(ctx)
    assert out["violations"] == 1
    assert out["violations_baseline"] == 2


def test_change_that_fixes_violations_counts_zero_introduced(tmp_path, monkeypatch):
    # Baseline had 2; current fixed one -> introduced 0 (a removed violation is not
    # negative; the metric is net NEW violations, floored at 0).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Widget.tsx").write_text("CURRENT\n")
    monkeypatch.setattr(convention, "_pattern_context", lambda path: PATTERN_OK)
    monkeypatch.setattr(convention, "_baseline_content", lambda wt, sha, rel: "BASELINE")

    def fake_lint(**kw):
        if kw.get("content") == "BASELINE":
            return _lint_with([_V_EXPORT, _V_NAMING])
        return _lint_with([_V_EXPORT])

    monkeypatch.setattr(convention, "_lint", fake_lint)
    ctx = _ctx(tmp_path)
    ctx.changed_files = ["src/Widget.tsx"]
    out = convention.score(ctx)
    assert out["violations"] == 0


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
